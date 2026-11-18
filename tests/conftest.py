"""
Pruebas contra un PostgreSQL REAL (no simulado): así se prueban también las reglas que protege
la propia base (CHECK, llaves, triggers, vistas) y los permisos de preboletas_api.

Antes de la sesión se crea una base temporal `preboletas_pruebas_<azar>`:
  1. se le aplica database/esquema_sicar.sql (la estructura exacta, sin las líneas \\restrict),
  2. se copian los catálogos de la base `sicar` (solo lectura): unidades, apps, tipos de pago y
     tipos de movimiento. Si `sicar` no está disponible se usan los valores conocidos.
Al final la base se borra. Nunca se escribe en `sicar`.

La API de pruebas se conecta con `SET ROLE preboletas_api`: opera con los permisos reales.

Variables (en la terminal, o en .env.local porque llevan contraseña):
  PRUEBAS_BD_ADMIN_URL  Usuario que puede crear bases y hacer SET ROLE (normalmente postgres).
                        Ej.: postgresql://postgres:<contraseña>@localhost:5432/postgres
  PRUEBAS_BD_ROL        Rol con el que opera la API en pruebas (por omisión preboletas_api; vacío = ninguno).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.bd import BaseDatos
from app.config import Configuracion
from app.dependencias import Servicios
from app.errores import ServicioNoDisponible
from app.integraciones.dataverse import Fruta, Proveedor
from app.integraciones.expo_push import ResultadoPush
from app.integraciones.sincronizador_core import SincronizadorCore
from app.main import TIPOS_MOVIMIENTO_USADOS, crear_app
from app.motor.motor_boletas import procesar_boleta
from app.seguridad.token import IdentidadToken, ValidadorToken

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENTE = "33333333-3333-3333-3333-333333333333"
ESQUEMA = Path(__file__).resolve().parents[2] / "database" / "esquema_sicar.sql"


class _ConfigPruebas(BaseSettings):
    """Variables de las pruebas: de la terminal o, si no, de .env / .env.local (como los secretos de la API)."""

    model_config = SettingsConfigDict(
        env_file=(Path(__file__).resolve().parents[1] / ".env", Path(__file__).resolve().parents[1] / ".env.local"),
        extra="ignore",
        hide_input_in_errors=True,
    )
    pruebas_bd_admin_url: str = ""
    pruebas_bd_rol: str = "preboletas_api"


_CFG_PRUEBAS = _ConfigPruebas()
ADMIN_URL = _CFG_PRUEBAS.pruebas_bd_admin_url
ROL_API = _CFG_PRUEBAS.pruebas_bd_rol

YUC, TEC = 14, 1  # Unidades usadas en las pruebas.
LIMON = "01"
PERSA = "02"

_LLAVE_PRIVADA = rsa.generate_private_key(public_exponent=65537, key_size=2048)

# Valores conocidos por si la base `sicar` no está disponible para copiar catálogos.
_UNIDADES = [(14, "Yucatán"), (1, "Tecomán"), (2, "Apatzingán"), (4, "Veracruz")]
_TIPOS_PAGO = [
    (1, "rendimiento", "Precio a Rendimiento"),
    (2, "pactado", "Precio Pactado"),
    (3, "corrida", "Precio a Corrida"),
]


# ----------------------------------------------------------------------
# Dobles de los servicios externos
# ----------------------------------------------------------------------
class DataverseDePrueba:
    """Proveedores y frutas fijos. `caido = True` simula que Dataverse no responde."""

    def __init__(self) -> None:
        self.caido = False
        self.proveedores = {
            "MP2520": Proveedor("MP2520", "CITRICOS DEL SURESTE", YUC),
            "MP3000": Proveedor("MP3000", "LIMONERA DEL GOLFO", None),
            "MP4000": Proveedor("MP4000", "HUERTAS TECOMAN", TEC),
        }
        self.frutas = {LIMON: Fruta(LIMON, "LIMON MEXICANO"), PERSA: Fruta(PERSA, "LIMON PERSA")}

    def _revisar(self) -> None:
        if self.caido:
            raise ServicioNoDisponible("Dataverse no responde.", codigo="dataverse_no_disponible")

    def buscar_proveedores(self, texto: str, limite: int) -> list[Proveedor]:
        self._revisar()
        t = texto.lower()
        return [p for p in self.proveedores.values() if t in p.nombre.lower() or t in p.cuenta.lower()][:limite]

    def proveedor(self, cuenta: str) -> Proveedor | None:
        self._revisar()
        return self.proveedores.get(cuenta)

    def buscar_frutas(self, texto: str, limite: int) -> list[Fruta]:
        self._revisar()
        return [f for f in self.frutas.values() if texto.lower() in f.nombre.lower()][:limite]

    def fruta(self, codigo: str) -> Fruta | None:
        self._revisar()
        return self.frutas.get(codigo)


class CoreDePrueba:
    """Simula CoreAPI. `caido = True` simula que no responde (como el cliente real: 503 core_no_disponible)."""

    def __init__(self) -> None:
        self.llamadas: list[IdentidadToken] = []
        self.caido = False
        # "Actualizar boleta": folios pedidos y lo que hace CoreAPI con cada uno (por omisión, nada).
        self.boletas_actualizadas: list[str] = []
        self.al_actualizar: Callable[[str], str] = lambda folio: "sin_cambios"

    def verificar(self, identidad: IdentidadToken) -> int:
        self.llamadas.append(identidad)
        if self.caido:
            raise ServicioNoDisponible("CoreAPI no responde.", codigo="core_no_disponible")
        return 0

    def actualizar_boleta(self, folio: str) -> str:
        self.boletas_actualizadas.append(folio)
        if self.caido:
            raise ServicioNoDisponible("CoreAPI no responde.", codigo="core_no_disponible")
        return self.al_actualizar(folio)


@dataclass
class PushDePrueba:
    enviados: list[tuple[list[str], str]] = field(default_factory=list)
    invalidos: set[str] = field(default_factory=set)

    def enviar(self, tokens: list[str], titulo: str, cuerpo: str, datos: dict[str, Any]) -> list[ResultadoPush]:
        self.enviados.append((tokens, cuerpo))
        return [
            ResultadoPush(t, ok=False, error="DeviceNotRegistered")
            if t in self.invalidos
            else ResultadoPush(t, ok=True)
            for t in tokens
        ]


class LlavesDePrueba:
    def llave_para(self, token: str) -> Any:
        return _LLAVE_PRIVADA.public_key()


def firmar_token(oid: str, roles: list[str], /, **cambios: Any) -> str:
    """Access token como los de Microsoft para Preboletas, firmado con la llave de prueba."""
    ahora = int(time.time())
    claims: dict[str, Any] = {
        "aud": f"api://{CLIENTE}",
        "iss": f"https://sts.windows.net/{TENANT}/",
        "tid": TENANT,
        "oid": oid,
        "name": "Persona de prueba",
        "upn": f"{oid[:8]}@sicar.com.mx",
        "scp": "access_as_user",
        "roles": roles,
        "iat": ahora,
        "exp": ahora + 3600,
    }
    claims.update(cambios)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, _LLAVE_PRIVADA, algorithm="RS256")


# ----------------------------------------------------------------------
# Base de datos temporal
# ----------------------------------------------------------------------
def _url_de(base: str) -> str:
    return make_conninfo(ADMIN_URL, dbname=base)


def _copiar_catalogos(destino: psycopg.Connection[Any]) -> bool:
    """Copia los catálogos de `sicar`. Devuelve False si no se pudo (y siembra los conocidos)."""
    try:
        with psycopg.connect(_url_de("sicar"), row_factory=dict_row, connect_timeout=5) as origen:
            unidades = origen.execute("SELECT id, nombre FROM core.unidades_negocio").fetchall()
            apps = origen.execute("SELECT id, clave, nombre FROM core.apps").fetchall()
            tipos_pago = origen.execute("SELECT id, clave, nombre, activo FROM preboletas.tipos_pago").fetchall()
            movs = origen.execute("SELECT clave, nombre, badge, color FROM preboletas.tipos_movimiento").fetchall()
    except psycopg.Error:
        unidades, apps, tipos_pago, movs = [], [], [], []
    copiado = bool(unidades and tipos_pago and movs)
    if not copiado:
        unidades = [{"id": i, "nombre": n} for i, n in _UNIDADES]
        tipos_pago = [{"id": i, "clave": c, "nombre": n, "activo": True} for i, c, n in _TIPOS_PAGO]
        movs = []
    for u in unidades:
        destino.execute("INSERT INTO core.unidades_negocio (id, nombre) VALUES (%(id)s, %(nombre)s)", u)
    for a in apps:
        destino.execute(
            "INSERT INTO core.apps (id, clave, nombre) OVERRIDING SYSTEM VALUE VALUES (%(id)s, %(clave)s, %(nombre)s)",
            a,
        )
    destino.execute(
        "INSERT INTO core.apps (clave, nombre) SELECT 'preboletas', 'Preboletas' "
        "WHERE NOT EXISTS (SELECT 1 FROM core.apps WHERE clave = 'preboletas')"
    )
    for t in tipos_pago:
        destino.execute(
            "INSERT INTO preboletas.tipos_pago (id, clave, nombre, activo) OVERRIDING SYSTEM VALUE "
            "VALUES (%(id)s, %(clave)s, %(nombre)s, %(activo)s)",
            t,
        )
    for m in movs:
        destino.execute(
            "INSERT INTO preboletas.tipos_movimiento (clave, nombre, badge, color) "
            "VALUES (%(clave)s, %(nombre)s, %(badge)s, %(color)s)",
            m,
        )
    # Los que use la API y no estén (solo en la base de pruebas) se agregan para poder probar;
    # test_catalogo revisa aparte que `sicar` los tenga.
    for clave in sorted(TIPOS_MOVIMIENTO_USADOS):
        destino.execute(
            "INSERT INTO preboletas.tipos_movimiento (clave, nombre, badge, color) "
            "VALUES (%s, %s, 'Prueba', '#676767') ON CONFLICT DO NOTHING",
            (clave, clave.replace("_", " ").capitalize()[:60]),
        )
    return copiado


@dataclass(frozen=True)
class BaseDePrueba:
    url: str
    catalogos_de_sicar: bool
    claves_sicar: frozenset[str]


@pytest.fixture(scope="session")
def bd_prueba() -> Iterator[BaseDePrueba]:
    if not ADMIN_URL:
        pytest.skip("Define PRUEBAS_BD_ADMIN_URL (usuario que pueda crear bases) para correr las pruebas.")
    try:
        # Siempre a la base de mantenimiento: de la URL solo se usan usuario, contraseña, host y puerto.
        admin = psycopg.connect(_url_de("postgres"), autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"No hay PostgreSQL para pruebas ({exc}).")
    nombre = f"preboletas_pruebas_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{nombre}"')
    url = _url_de(nombre)
    try:
        # Las líneas \restrict / \unrestrict son comandos de psql, no SQL.
        lineas = ESQUEMA.read_text(encoding="utf-8").splitlines()
        sql = "\n".join(x for x in lineas if not x.startswith("\\"))
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(sql)  # script completo, sin parámetros
            c.execute("SET search_path = public")
            copiado = _copiar_catalogos(c)
        claves: frozenset[str] = frozenset()
        if copiado:
            with psycopg.connect(_url_de("sicar"), row_factory=dict_row) as s:
                claves = frozenset(str(f["clave"]) for f in s.execute("SELECT clave FROM preboletas.tipos_movimiento"))
        yield BaseDePrueba(url=url, catalogos_de_sicar=copiado, claves_sicar=claves)
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{nombre}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def con(bd_prueba: BaseDePrueba) -> Iterator[psycopg.Connection[Any]]:
    """Conexión directa como administrador (para preparar datos, como lo haría CoreAPI)."""
    with psycopg.connect(bd_prueba.url, autocommit=True, row_factory=dict_row) as c:
        yield c


_TABLAS = (
    "preboletas.movimientos, preboletas.notificaciones, preboletas.propuestas_precio, preboletas.incidencias, "
    "preboletas.slots_bascula, preboletas.recepciones, preboletas.preboletas, preboletas.precios_diarios, "
    "preboletas.tolerancias, preboletas.tipos_caja_productos, preboletas.tipos_caja, preboletas.unidades_config, "
    "preboletas.permisos_colaborador, preboletas.canales_colaborador, preboletas.dispositivos_push, "
    "core.boletas_versiones, core.boletas, core.colaborador_acceso, core.colaboradores"
)


@pytest.fixture(autouse=True)
def _limpiar(request: pytest.FixtureRequest) -> Iterator[None]:
    """Cada prueba que use la base empieza con las tablas vacías (se conservan los catálogos)."""
    yield
    if "bd_prueba" in request.fixturenames:
        url = request.getfixturevalue("bd_prueba").url
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(f"TRUNCATE {_TABLAS} RESTART IDENTITY CASCADE")


# ----------------------------------------------------------------------
# Aplicación
# ----------------------------------------------------------------------
def configuracion(url: str) -> Configuracion:
    return Configuracion(
        _env_file=None,
        bd_url=url,
        entra_tenant_id=TENANT,
        entra_client_id=CLIENTE,
        core_api_key="llave-de-prueba",
        dataverse_url="https://prueba.crm.dynamics.com",
        dataverse_client_id="dv",
        dataverse_client_secret="secreto",
        url_publica="https://preboletas.prueba",
        publico_limite_por_minuto=5,
    )


@dataclass
class Entorno:
    """Todo lo que una prueba necesita: cliente HTTP, servicios, dobles y ayudas para preparar datos."""

    cliente: TestClient
    servicios: Servicios
    dataverse: DataverseDePrueba
    core: CoreDePrueba
    push: PushDePrueba
    con: psycopg.Connection[Any]
    _secuencia: int = 0

    # -------------------------- personas --------------------------
    def colaborador(
        self, nombre: str, roles: list[str], unidad: int = YUC, *, vinculado: bool = True
    ) -> dict[str, Any]:
        """
        Colaborador con su copia de roles. `vinculado=True`: como lo deja CoreAPI tras su primer login
        (con entra_oid y nombre). `vinculado=False`: solo dado de alta (correo y unidad), aún sin entrar.
        El token de prueba trae su correo en `upn`.
        """
        oid = str(uuid.uuid4())
        correo = f"{nombre.lower().replace(' ', '.')}@sicar.com.mx"
        fila = self.con.execute(
            "INSERT INTO core.colaboradores (nombre, correo, entra_oid, unidad_negocio_id) VALUES (%s, %s, %s, %s) "
            "RETURNING id",
            (nombre if vinculado else None, correo, oid if vinculado else None, unidad),
        ).fetchone()
        assert fila is not None
        for rol in roles:
            self.con.execute(
                "INSERT INTO core.colaborador_acceso (colaborador_id, app_id, rol) "
                "SELECT %s, id, %s FROM core.apps WHERE clave = 'preboletas'",
                (fila["id"], rol),
            )
        return {
            "id": int(fila["id"]),
            "oid": oid,
            "correo": correo,
            "roles": roles,
            "h": self.encabezado(oid, roles, upn=correo, name=nombre),
        }

    @staticmethod
    def encabezado(oid: str, roles: list[str], **cambios: Any) -> dict[str, str]:
        return {"Authorization": f"Bearer {firmar_token(oid, roles, **cambios)}"}

    def permiso(self, colaborador_id: int, permiso: str, unidad: int, por: int) -> None:
        self.con.execute(
            "INSERT INTO preboletas.permisos_colaborador (colaborador_id, permiso, unidad_negocio_id, otorgado_por_id) "
            "VALUES (%s, %s, %s, %s)",
            (colaborador_id, permiso, unidad, por),
        )

    # -------------------------- configuración --------------------------
    def caja(self, nombre: str = "Caja estándar", kg: str = "18", frutas: tuple[str, ...] = (LIMON,)) -> int:
        fila = self.con.execute(
            "INSERT INTO preboletas.tipos_caja (nombre, kg_por_caja) VALUES (%s, %s) RETURNING id",
            (nombre, Decimal(kg)),
        ).fetchone()
        assert fila is not None
        for f in frutas:
            self.con.execute(
                "INSERT INTO preboletas.tipos_caja_productos (tipo_caja_id, producto_codigo, producto_nombre) "
                "VALUES (%s, %s, %s)",
                (fila["id"], f, self.dataverse.frutas[f].nombre),
            )
        return int(fila["id"])

    def tolerancia(self, unidad: int, codigo: str, pct: str, por: int) -> None:
        self.con.execute(
            "INSERT INTO preboletas.tolerancias (unidad_negocio_id, producto_codigo, producto_nombre, tolerancia_pct, "
            "actualizado_por_id) VALUES (%s, %s, %s, %s, %s)",
            (unidad, codigo, self.dataverse.frutas[codigo].nombre, Decimal(pct), por),
        )

    def implementar(self, unidad: int, por: int, desde: datetime | None = None) -> None:
        self.con.execute(
            "INSERT INTO preboletas.unidades_config (unidad_negocio_id, inicio_operacion_at, actualizado_por_id) "
            "VALUES (%s, COALESCE(%s, now() - interval '1 day'), %s)",
            (unidad, desde, por),
        )

    def precio_dia(self, unidad: int, codigo: str, precio: str, tipo: str, por: int, fecha: date | None = None) -> None:
        self.con.execute(
            "INSERT INTO preboletas.precios_diarios (unidad_negocio_id, producto_codigo, producto_nombre, fecha, "
            "precio_kg, tipo_pago_id, actualizado_por_id) "
            "SELECT %s, %s, %s, COALESCE(%s, core.hoy()), %s, id, %s FROM preboletas.tipos_pago WHERE clave = %s",
            (unidad, codigo, self.dataverse.frutas[codigo].nombre, fecha, Decimal(precio), por, tipo),
        )

    # -------------------------- boletas de D365 --------------------------
    def boleta(
        self,
        unidad: int,
        proveedor: str | None,
        *,
        bruto: str = "9000",
        tara: str = "0",
        estado_d365: int = 200000001,
        primera_pesada_at: datetime | None = None,
    ) -> int:
        """Inserta una boleta como lo haría CoreAPI (la base calcula su estado)."""
        self._secuencia += 1
        nombre = self.dataverse.proveedores[proveedor].nombre if proveedor in self.dataverse.proveedores else None
        if proveedor and nombre is None:
            nombre = f"PROVEEDOR {proveedor}"
        bruto_d, tara_d = Decimal(bruto), Decimal(tara)
        fila = self.con.execute(
            """
            INSERT INTO core.boletas (folio, d365_recid, empresa, unidad_negocio_id, proveedor_cuenta, proveedor_nombre,
                                      peso_bruto, tara, peso_neto, primera_pesada_at, segunda_pesada_at, datos_d365)
            VALUES (%s, %s, 'sic', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                f"BMP-{100000 + self._secuencia}",
                uuid.uuid4().int % 10**15,
                unidad,
                proveedor,
                nombre,
                bruto_d,
                tara_d,
                bruto_d - tara_d if tara_d > 0 else 0,
                (primera_pesada_at or datetime.now().astimezone()) if bruto_d > 0 else None,
                datetime.now().astimezone() + timedelta(seconds=1) if tara_d > 0 else None,
                json.dumps({"mserp_ballotstatus": str(estado_d365)}),
            ),
        ).fetchone()
        assert fila is not None
        return int(fila["id"])

    def actualizar_boleta(self, boleta_id: int, **campos: Any) -> None:
        """Cambia una boleta como lo haría un evento nuevo de Power Automate."""
        if "estado_d365" in campos:
            campos["datos_d365"] = json.dumps({"mserp_ballotstatus": str(campos.pop("estado_d365"))})
        if "tara" in campos:
            fila = self.con.execute("SELECT peso_bruto FROM core.boletas WHERE id = %s", (boleta_id,)).fetchone()
            assert fila is not None
            campos["tara"] = Decimal(campos["tara"])
            campos["peso_neto"] = fila["peso_bruto"] - campos["tara"]
            campos.setdefault("segunda_pesada_at", datetime.now().astimezone())
        if "proveedor_cuenta" in campos:
            cuenta = campos["proveedor_cuenta"]
            prov = self.dataverse.proveedores.get(cuenta)
            campos["proveedor_nombre"] = prov.nombre if prov else f"PROVEEDOR {cuenta}"
        asignaciones = ", ".join(f"{k} = %s" for k in campos)
        self.con.execute(
            f"UPDATE core.boletas SET {asignaciones}, version = version + 1 WHERE id = %s",
            (*campos.values(), boleta_id),
        )

    def procesar(self, boleta_id: int) -> None:
        """Lo que haría la escucha de NOTIFY core_boletas: pasar la boleta por el motor."""
        with self.servicios.bd.transaccion() as tx:
            procesar_boleta(tx, boleta_id)

    # -------------------------- consultas rápidas --------------------------
    def uno(self, sql: str, *params: Any) -> dict[str, Any]:
        fila = self.con.execute(sql, params).fetchone()
        assert fila is not None, sql
        return dict(fila)

    def todos(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        return [dict(f) for f in self.con.execute(sql, params).fetchall()]


@pytest.fixture
def entorno(bd_prueba: BaseDePrueba, con: psycopg.Connection[Any]) -> Iterator[Entorno]:
    cfg = configuracion(bd_prueba.url)
    dataverse, core, push = DataverseDePrueba(), CoreDePrueba(), PushDePrueba()
    servicios = Servicios(
        cfg=cfg,
        bd=BaseDatos(bd_prueba.url, 1, 4, rol=ROL_API or None),
        validador=ValidadorToken(cfg, LlavesDePrueba()),
        dataverse=dataverse,
        core=core,
        push=push,
        # En pruebas la sincronización con CoreAPI corre en el mismo hilo (se puede revisar al momento).
        sincronizador=SincronizadorCore(core, en_segundo_plano=False),
    )
    with TestClient(crear_app(cfg, servicios, trabajos_de_fondo=False)) as cliente:
        yield Entorno(cliente=cliente, servicios=servicios, dataverse=dataverse, core=core, push=push, con=con)


@dataclass
class Escenario:
    """Un escenario base listo para operar: Yucatán implementada, limón con caja y tolerancia de 10%."""

    e: Entorno
    admin: dict[str, Any]
    comprador: dict[str, Any]
    comprador2: dict[str, Any]
    basculista: dict[str, Any]
    basculista_tec: dict[str, Any]
    caja_id: int

    def crear_preboleta(self, quien: dict[str, Any] | None = None, **cambios: Any) -> dict[str, Any]:
        datos: dict[str, Any] = {
            "proveedor_cuenta": "MP2520",
            "producto_codigo": LIMON,
            "unidad_negocio_id": YUC,
            "tipo_caja_id": self.caja_id,
            "tipo_negociacion": "cajas",
            "cantidad": 100,
        }
        datos.update(cambios)
        r = self.e.cliente.post("/preboletas", json=datos, headers=(quien or self.comprador)["h"])
        assert r.status_code == 201, r.text
        resultado: dict[str, Any] = r.json()
        return resultado

    def entrada(self, preboleta_id: int, cajas: int, quien: dict[str, Any] | None = None, **extra: Any) -> int:
        r = self.e.cliente.post(
            f"/preboletas/{preboleta_id}/recepciones",
            json={"cajas": cajas, **extra},
            headers=(quien or self.basculista)["h"],
        )
        assert r.status_code == 201, r.text
        return int(r.json()["id"])

    def detalle(self, preboleta_id: int, quien: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self.e.cliente.get(f"/preboletas/{preboleta_id}", headers=(quien or self.admin)["h"])
        assert r.status_code == 200, r.text
        resultado: dict[str, Any] = r.json()
        return resultado


@pytest.fixture
def esc(entorno: Entorno) -> Escenario:
    admin = entorno.colaborador("Eduardo Admin", ["Admin"])
    comprador = entorno.colaborador("Mariana Compradora", ["Comprador"])
    comprador2 = entorno.colaborador("Jorge Comprador", ["Comprador"], unidad=TEC)
    basculista = entorno.colaborador("Luis Basculista", ["Basculista"])
    basculista_tec = entorno.colaborador("Rosa Basculista", ["Basculista"], unidad=TEC)
    caja_id = entorno.caja()
    entorno.tolerancia(YUC, LIMON, "10", admin["id"])
    entorno.implementar(YUC, admin["id"])
    return Escenario(entorno, admin, comprador, comprador2, basculista, basculista_tec, caja_id)
