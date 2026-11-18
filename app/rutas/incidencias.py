"""Incidencias de cantidad, conciliación de boletas y autorización de propuestas de precio."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query

from app.dependencias import ServiciosDep, UsuarioActual
from app.errores import RESPUESTAS_COMUNES, RespuestaError
from app.esquemas.modelos import (
    Descartar,
    DetalleConciliacion,
    IncidenciaCantidad,
    ListaConciliaciones,
    Ok,
    PropuestaLista,
    Resolucion,
    Vinculada,
    Vincular,
)
from app.rutas.comun import en_transaccion
from app.servicios import cantidad, conciliacion, consultas, propuestas

router = APIRouter(responses=RESPUESTAS_COMUNES)
_409: dict[int | str, dict[str, Any]] = {
    409: {"model": RespuestaError, "description": "Ya resuelta, o la boleta ya está ligada."}
}
Decision = Literal["autorizar", "rechazar"]
_ESTADO = {"autorizar": "autorizada", "rechazar": "rechazada"}


# ----------------------------------------------------------------------
# Incidencias de cantidad
# ----------------------------------------------------------------------
@router.get(
    "/incidencias",
    response_model=list[IncidenciaCantidad],
    tags=["Incidencias"],
    summary="Incidencias de cantidad y cierres a mano (todas las unidades)",
)
def incidencias(
    usuario: UsuarioActual,
    servicios: ServiciosDep,
    estado: str | None = Query(default=None, description="pendiente, autorizada, rechazada, registrada, cancelada"),
) -> list[IncidenciaCantidad]:
    """Las ve y resuelve cualquier comprador (de cualquier unidad) o un Admin. Nunca frenan la báscula."""
    with servicios.bd.transaccion() as con:
        return [IncidenciaCantidad.model_validate(i) for i in consultas.incidencias_cantidad(con, usuario, estado)]


@router.post(
    "/incidencias/{incidencia_id}/{decision}",
    response_model=Ok,
    tags=["Incidencias"],
    responses=_409,
    summary="Autorizar o rechazar una sobre-entrega o un faltante",
)
def resolver_incidencia(
    incidencia_id: int, decision: Decision, datos: Resolucion, usuario: UsuarioActual, servicios: ServiciosDep
) -> Ok:
    en_transaccion(
        servicios,
        usuario,
        lambda op: cantidad.resolver_incidencia_cantidad(
            op, usuario, incidencia_id, _ESTADO[decision], datos.comentario
        ),
    )
    return Ok()


# ----------------------------------------------------------------------
# Conciliación de boletas
# ----------------------------------------------------------------------
@router.get(
    "/conciliaciones",
    response_model=ListaConciliaciones,
    tags=["Conciliación"],
    summary="Boletas pendientes de conciliar y las últimas resueltas",
)
def conciliaciones(usuario: UsuarioActual, servicios: ServiciosDep) -> ListaConciliaciones:
    """Admin y compradores: todas las unidades. Basculista: su unidad."""
    with servicios.bd.transaccion() as con:
        return ListaConciliaciones.model_validate(conciliacion.listar(con, usuario))


@router.get(
    "/conciliaciones/{incidencia_id}",
    response_model=DetalleConciliacion,
    tags=["Conciliación"],
    summary="Detalle de una boleta por conciliar, con preboletas y cargas candidatas",
)
def conciliacion_detalle(incidencia_id: int, usuario: UsuarioActual, servicios: ServiciosDep) -> DetalleConciliacion:
    with servicios.bd.transaccion() as con:
        return DetalleConciliacion.model_validate(conciliacion.detalle(con, usuario, incidencia_id))


@router.post(
    "/conciliaciones/{incidencia_id}/vincular",
    response_model=Vinculada,
    tags=["Conciliación"],
    responses=_409,
    summary="Ligar la boleta a una carga existente o a una carga nueva",
)
def vincular(incidencia_id: int, datos: Vincular, usuario: UsuarioActual, servicios: ServiciosDep) -> Vinculada:
    """
    El cambio de proveedor lo concilia solo el basculista de la unidad o un Admin; lo demás, también
    cualquier comprador. Si la preboleta estaba cerrada, se reabre y se vuelve a evaluar su cierre.
    """
    recepcion_id = en_transaccion(
        servicios,
        usuario,
        lambda op: conciliacion.vincular(
            op,
            usuario,
            incidencia_id,
            preboleta_id=datos.preboleta_id,
            recepcion_id=datos.recepcion_id,
            cajas_nueva_carga=datos.cajas_nueva_carga,
            comentario=datos.comentario,
        ),
    )
    return Vinculada(recepcion_id=recepcion_id)


@router.post(
    "/conciliaciones/{incidencia_id}/descartar",
    response_model=Ok,
    tags=["Conciliación"],
    responses=_409,
    summary="Marcar la boleta como no relacionada a ninguna preboleta",
)
def descartar(incidencia_id: int, datos: Descartar, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    en_transaccion(servicios, usuario, lambda op: conciliacion.descartar(op, usuario, incidencia_id, datos.comentario))
    return Ok()


# ----------------------------------------------------------------------
# Propuestas de precio
# ----------------------------------------------------------------------
@router.get("/propuestas", response_model=list[PropuestaLista], tags=["Propuestas"], summary="Propuestas de precio")
def lista_propuestas(
    usuario: UsuarioActual,
    servicios: ServiciosDep,
    estado: str | None = Query(default=None, description="pendiente, autorizada, rechazada, cancelada, resuelta_sola"),
) -> list[PropuestaLista]:
    """Cada fila indica si tú puedes autorizarla (`puede_resolver`)."""
    with servicios.bd.transaccion() as con:
        return [PropuestaLista.model_validate(p) for p in consultas.propuestas(con, usuario, estado)]


@router.post(
    "/propuestas/{propuesta_id}/{decision}",
    response_model=Ok,
    tags=["Propuestas"],
    responses=_409,
    summary="Autorizar o rechazar una propuesta",
)
def resolver_propuesta(
    propuesta_id: int, decision: Decision, datos: Resolucion, usuario: UsuarioActual, servicios: ServiciosDep
) -> Ok:
    """Requiere autorizar_precios en la unidad (o Admin). Nadie autoriza la suya, salvo un Admin."""
    en_transaccion(
        servicios,
        usuario,
        lambda op: propuestas.resolver(op, usuario, propuesta_id, _ESTADO[decision], datos.comentario),
    )
    return Ok()
