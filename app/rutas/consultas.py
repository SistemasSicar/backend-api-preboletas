"""Bitácora, avisos (campana), dispositivos push y la página pública del QR."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Query, Request

from app.dependencias import ServiciosDep, UsuarioActual
from app.errores import RESPUESTAS_COMUNES, DemasiadasSolicitudes, NoEncontrado, RespuestaError
from app.esquemas.modelos import (
    Aviso,
    ListaMovimientos,
    Marcados,
    MarcarLeidos,
    NuevoDispositivo,
    Ok,
    PreboletaPublica,
    PrecioDia,
    Resumen,
    SinLeer,
)
from app.servicios import consultas, preboletas
from app.servicios.comun import hoy

router = APIRouter(responses=RESPUESTAS_COMUNES)


@router.get("/movimientos", response_model=ListaMovimientos, tags=["Bitácora"], summary="Historial de movimientos")
def movimientos(
    usuario: UsuarioActual,
    servicios: ServiciosDep,
    preboleta_id: int | None = None,
    boleta_id: int | None = None,
    colaborador_id: int | None = Query(default=None, description="0 = lo que hizo el sistema."),
    proveedor_cuenta: str | None = None,
    tipos: list[str] = Query(default=[]),
    desde: date | None = None,
    hasta: date | None = None,
    limite: int = Query(default=30, ge=1, le=200),
    desplazamiento: int = Query(default=0, ge=0),
) -> ListaMovimientos:
    """Admin ve todo; Basculista, su unidad; Comprador, sus preboletas y lo que él hizo. Más recientes primero."""
    with servicios.bd.transaccion() as con:
        return ListaMovimientos.model_validate(
            consultas.movimientos(
                con,
                usuario,
                preboleta_id=preboleta_id,
                boleta_id=boleta_id,
                colaborador_id=colaborador_id,
                proveedor_cuenta=proveedor_cuenta,
                tipos=tipos,
                desde=desde,
                hasta=hasta,
                limite=limite,
                desplazamiento=desplazamiento,
            )
        )


@router.get("/resumen", response_model=Resumen, tags=["Avisos"], summary="Pendientes por resolver (pestañas)")
def resumen(usuario: UsuarioActual, servicios: ServiciosDep) -> Resumen:
    with servicios.bd.transaccion() as con:
        return Resumen.model_validate(consultas.resumen(con, usuario))


@router.get(
    "/unidades/{unidad_id}/precio-dia/{producto_codigo}",
    response_model=PrecioDia,
    tags=["Preboletas y báscula"],
    summary="Precio del día y tolerancia de una fruta (para capturar una preboleta)",
)
def precio_dia(
    unidad_id: int, producto_codigo: str, usuario: UsuarioActual, servicios: ServiciosDep, fecha: date | None = None
) -> PrecioDia:
    with servicios.bd.transaccion() as con:
        return PrecioDia.model_validate(
            consultas.precio_del_dia(con, usuario, unidad_id, producto_codigo, fecha or hoy(con))
        )


# ----------------------------------------------------------------------
# Avisos
# ----------------------------------------------------------------------
@router.get("/notificaciones", response_model=list[Aviso], tags=["Avisos"], summary="Mis avisos (campana)")
def notificaciones(
    usuario: UsuarioActual, servicios: ServiciosDep, limite: int = Query(default=50, ge=1, le=200)
) -> list[Aviso]:
    with servicios.bd.transaccion() as con:
        return [Aviso.model_validate(a) for a in consultas.avisos(con, usuario, todos=False, limite=limite)]


@router.get("/notificaciones/sin-leer", response_model=SinLeer, tags=["Avisos"], summary="Cuántos avisos sin leer")
def sin_leer(usuario: UsuarioActual, servicios: ServiciosDep) -> SinLeer:
    with servicios.bd.transaccion() as con:
        return SinLeer(total=consultas.avisos_sin_leer(con, usuario))


@router.post("/notificaciones/leidas", response_model=Marcados, tags=["Avisos"], summary="Marcar avisos como leídos")
def leidas(datos: MarcarLeidos, usuario: UsuarioActual, servicios: ServiciosDep) -> Marcados:
    with servicios.bd.transaccion() as con:
        return Marcados(marcados=consultas.marcar_leidos(con, usuario, datos.ids))


@router.get(
    "/notificaciones/envios", response_model=list[Aviso], tags=["Avisos"], summary="Historial de envíos (Admin)"
)
def envios(
    usuario: UsuarioActual, servicios: ServiciosDep, limite: int = Query(default=100, ge=1, le=500)
) -> list[Aviso]:
    """Todos los avisos de todas las personas, con el resultado de su envío por correo y push."""
    with servicios.bd.transaccion() as con:
        return [Aviso.model_validate(a) for a in consultas.avisos(con, usuario, todos=True, limite=limite)]


@router.post("/dispositivos", response_model=Ok, tags=["Avisos"], summary="Registrar el teléfono para push (app)")
def registrar_dispositivo(datos: NuevoDispositivo, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    with servicios.bd.transaccion() as con:
        consultas.registrar_dispositivo(con, usuario, datos.token, datos.plataforma, datos.nombre_dispositivo)
    return Ok()


@router.delete(
    "/dispositivos/{token}", response_model=Ok, tags=["Avisos"], summary="Dar de baja el teléfono (cierre de sesión)"
)
def baja_dispositivo(token: str, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    with servicios.bd.transaccion() as con:
        consultas.baja_dispositivo(con, usuario, token)
    return Ok()


# ----------------------------------------------------------------------
# Página pública del QR (sin sesión)
# ----------------------------------------------------------------------
@router.get(
    "/publico/qr/{token}",
    response_model=PreboletaPublica,
    tags=["Público"],
    summary="Lo que ve el proveedor al abrir su QR",
    responses={429: {"model": RespuestaError, "description": "Demasiadas consultas desde tu red."}},
)
def publico_qr(token: str, request: Request, servicios: ServiciosDep) -> PreboletaPublica:
    """Sin cajas, kilos, precios ni comprador. Limitado por minuto y por IP."""
    ip = ip_cliente(request)
    assert servicios.limitador is not None
    if not servicios.limitador.permitir(ip):
        raise DemasiadasSolicitudes("Demasiadas consultas. Intenta en un minuto.", reintentar_en=60)
    try:
        uuid.UUID(token)
    except ValueError as exc:
        raise NoEncontrado("Ese QR no corresponde a ninguna preboleta.") from exc
    with servicios.bd.transaccion() as con:
        datos = preboletas.publico(con, token)
    if datos is None:
        raise NoEncontrado("Ese QR no corresponde a ninguna preboleta.")
    return PreboletaPublica.model_validate(datos)


def ip_cliente(request: Request) -> str:
    """
    IP de quien abre el QR. La página la sirve el panel (que llama desde su servidor) y reenvía
    la IP original en X-Forwarded-For; sin ese encabezado, la de la conexión. El token del QR es
    un UUID aleatorio: el límite protege la carga, no es lo que impide adivinar tokens.
    """
    reenviada = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if reenviada:
        return reenviada
    return request.client.host if request.client else "desconocida"
