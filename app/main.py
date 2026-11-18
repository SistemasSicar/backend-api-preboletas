"""
API de Preboletas (puerto 7230).

La ÚNICA pieza que habla con la base (esquema `preboletas`, y lee `core`) y con Dataverse.
El panel y la app móvil solo hablan con esta API.

Ejecutar en desarrollo (desde backend-api-preboletas/):
    .venv\\Scripts\\uvicorn app.main:crear_app --factory --reload --port 7230
Swagger: http://localhost:7230/docs (apagado en producción)

Al arrancar levanta cuatro hilos de fondo: la escucha de boletas (NOTIFY core_boletas → motor),
el barrido de respaldo, la escucha de eventos (NOTIFY preboletas_eventos → WebSocket) y el envío
de avisos por correo y push. Debe correr con UN solo worker (los tickets del WebSocket viven en memoria).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.bd import BaseDatos
from app.config import Configuracion, Entorno, obtener_configuracion
from app.dependencias import Servicios
from app.errores import registrar_manejadores
from app.esquemas.modelos import Salud
from app.integraciones.core_api import ClienteCoreApi
from app.integraciones.dataverse import ConsultorDataverse
from app.integraciones.expo_push import ExpoPush
from app.motor.escucha import CANAL_BOLETAS, Barrido, EscuchaPg, procesar_aviso_boleta
from app.rutas import catalogos, configuracion, consultas, incidencias, preboletas, sesion
from app.seguridad.token import LlavesMicrosoft, ValidadorToken
from app.servicios.comun import CANAL_EVENTOS
from app.trabajos.envio_notificaciones import EnvioNotificaciones

log = logging.getLogger(__name__)

DESCRIPCION = """
API de **Preboletas** de SICAR: negociaciones de fruta con proveedores, su paso por báscula y la
vinculación con las boletas de Dynamics 365 (la fuente de verdad).

* Personas (panel y app): `Authorization: Bearer <access_token del registro Preboletas>`.
  Al iniciar sesión se llama `GET /yo` (vincula la cuenta con su colaborador).
* Roles de Azure: **Admin** (todo), **Comprador** (sus preboletas), **Basculista** (su unidad).
  Lo que un rol no puede ver responde 404.
* Tiempo real: `POST /ws/ticket` y luego el WebSocket `/ws` con `{"ticket": "..."}` como primer mensaje.
* Errores: siempre `{ "codigo", "mensaje", "id_solicitud" }`.
"""

# Claves de tipos_movimiento que escribe esta API (FK a preboletas.tipos_movimiento).
TIPOS_MOVIMIENTO_USADOS = frozenset(
    {
        "preboleta_creada", "preboleta_cerrada", "preboleta_reabierta", "entrada_bascula", "carga_manual",
        "salida_bascula", "incidencia_sobre_entrega", "incidencia_faltante", "incidencia_autorizada",
        "incidencia_rechazada", "incidencia_cierre_manual", "boleta_creada", "boleta_vinculada",
        "boleta_desvinculada", "boleta_conciliada", "boleta_descartada", "incidencia_proveedor_distinto",
        "incidencia_sin_preboleta", "incidencia_cambio_proveedor", "precio_diario", "precio_preboleta",
        "precio_boleta", "tolerancia_global", "unidad_preboleta", "tipo_caja", "multiples_cargas",
        "comprador_preboleta", "propuesta_precio", "propuesta_autorizada", "propuesta_rechazada",
        "propuesta_cancelada", "permiso_colaborador", "entrada_cancelada", "recepcion_cancelada_d365",
        "cajas_corregidas", "implementacion_unidad",
    }
)  # fmt: skip


def crear_servicios(cfg: Configuracion) -> Servicios:
    bd = BaseDatos(cfg.bd_url.get_secret_value(), cfg.bd_pool_min, cfg.bd_pool_max)
    return Servicios(
        cfg=cfg,
        bd=bd,
        validador=ValidadorToken(cfg, LlavesMicrosoft(cfg.entra_tenant_id)),
        dataverse=ConsultorDataverse(cfg),
        core=ClienteCoreApi(cfg),
        push=ExpoPush(),
    )


def revisar_catalogo(bd: BaseDatos) -> None:
    """Avisa en el log si falta en la base algún tipo de movimiento que la API escribe (solo lectura)."""
    try:
        with bd.transaccion() as con:
            existentes = {str(f["clave"]) for f in con.execute("SELECT clave FROM preboletas.tipos_movimiento")}
    except Exception:
        log.warning("No se pudo revisar preboletas.tipos_movimiento (¿la base no responde?).")
        return
    faltan = sorted(TIPOS_MOVIMIENTO_USADOS - existentes)
    if faltan:
        log.error("Faltan tipos de movimiento en preboletas.tipos_movimiento: %s", ", ".join(faltan))


def crear_app(
    cfg: Configuracion | None = None, servicios: Servicios | None = None, *, trabajos_de_fondo: bool = True
) -> FastAPI:
    """Fábrica de la aplicación. Las pruebas inyectan su configuración y servicios, sin hilos de fondo."""
    cfg = cfg or obtener_configuracion()

    @asynccontextmanager
    async def ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
        s = servicios or crear_servicios(cfg)
        s.bd.abrir()
        s.hub.fijar_loop(asyncio.get_running_loop())
        app.state.servicios = s
        parar = threading.Event()
        if trabajos_de_fondo:
            await run_in_threadpool(revisar_catalogo, s.bd)
            s.hilos = [
                EscuchaPg(s.bd, CANAL_BOLETAS, procesar_aviso_boleta(s.bd), parar),
                EscuchaPg(s.bd, CANAL_EVENTOS, s.hub.publicar_desde_hilo, parar),
                Barrido(s.bd, parar),
                EnvioNotificaciones(s.bd, s.push, parar),
            ]
            for hilo in s.hilos:
                hilo.start()
        yield
        parar.set()
        for hilo in s.hilos:
            hilo.join(timeout=5)
        if s.sincronizador is not None:
            s.sincronizador.cerrar()
        s.bd.cerrar()

    produccion = cfg.entorno is Entorno.PRODUCCION
    app = FastAPI(
        title=cfg.nombre_servicio,
        version=cfg.version,
        description=DESCRIPCION,
        lifespan=ciclo_de_vida,
        docs_url=None if produccion else "/docs",
        redoc_url=None if produccion else "/redoc",
        openapi_url=None if produccion else "/openapi.json",
    )

    if cfg.cors_origenes:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origenes,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )

    @app.middleware("http")
    async def id_solicitud(request: Request, siguiente: Callable[[Request], Awaitable[Response]]) -> Response:
        """Asigna un ID a cada petición (o respeta el que venga) para rastrearla en los logs."""
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.id_solicitud = rid
        respuesta = await siguiente(request)
        respuesta.headers["X-Request-ID"] = rid
        return respuesta

    registrar_manejadores(app)
    for modulo in (sesion, catalogos, preboletas, incidencias, configuracion, consultas):
        app.include_router(modulo.router)

    @app.get("/salud", response_model=Salud, tags=["Salud"], summary="Verificar que el servicio está arriba")
    async def salud(request: Request) -> Salud:
        s: Servicios = request.app.state.servicios
        bd_ok = await run_in_threadpool(s.bd.disponible)
        escuchas = {h.name: bool(getattr(h, "conectada", h.is_alive())) for h in s.hilos}
        return Salud(
            estado="ok" if bd_ok and all(escuchas.values()) else "degradado",
            servicio=cfg.nombre_servicio,
            version=cfg.version,
            entorno=cfg.entorno.value,
            base_datos=bd_ok,
            escuchas=escuchas,
        )

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
