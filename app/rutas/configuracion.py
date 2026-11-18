"""Configuración por unidad (precio del día, tolerancias, implementación) y colaboradores (Admin)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Query

from app.dependencias import ServiciosDep, UsuarioActual
from app.errores import RESPUESTAS_COMUNES, AccesoDenegado, ErrorServicioExterno, ServicioNoDisponible
from app.esquemas.modelos import (
    Canales,
    ColaboradorAdmin,
    GuardarPrecio,
    GuardarTolerancia,
    Implementacion,
    Ok,
    PreciosHoyUnidad,
    PreciosUnidad,
)
from app.integraciones.dataverse import Fruta
from app.rutas.comun import en_transaccion, fruta_valida
from app.servicios import colaboradores, configuracion
from app.servicios.comun import Operacion, hoy
from app.usuario import PERMISO_PRECIOS

router = APIRouter(tags=["Configuración"], responses=RESPUESTAS_COMUNES)


@router.get("/precios-hoy", response_model=list[PreciosHoyUnidad], summary="Precio de hoy de cada unidad (resumen)")
def precios_hoy(usuario: UsuarioActual, servicios: ServiciosDep) -> list[PreciosHoyUnidad]:
    """
    Las unidades donde puedes definir el precio del día (Admin todas; si no, las de tu permiso
    precios_diarios), cada una con sus frutas que YA tienen precio hoy. Solo lee la base: no consulta
    Dataverse, por eso es rápida. El detalle completo de una unidad está en `/unidades/{id}/precios`.
    """
    with servicios.bd.transaccion() as con:
        return [PreciosHoyUnidad.model_validate(u) for u in configuracion.precios_hoy(con, usuario)]


@router.get(
    "/unidades/{unidad_id}/precios", response_model=PreciosUnidad, summary="Precio del día y tolerancia por fruta"
)
def precios(
    unidad_id: int, usuario: UsuarioActual, servicios: ServiciosDep, fecha: date | None = Query(default=None)
) -> PreciosUnidad:
    """
    Admin o quien tenga precios_diarios en la unidad. Lista todas las frutas admitidas en D365
    (conjunto MP); si Dataverse no responde, solo las que ya tienen precio o tolerancia.
    """
    if not usuario.tiene_permiso(PERMISO_PRECIOS, unidad_id):
        raise AccesoDenegado("No tienes el permiso precios_diarios en esta unidad.", codigo="sin_permiso")
    frutas: list[Fruta] | None
    try:
        frutas = servicios.dataverse.buscar_frutas("", 50)
    except (ServicioNoDisponible, ErrorServicioExterno):
        frutas = None
    with servicios.bd.transaccion() as con:
        return PreciosUnidad.model_validate(configuracion.precios_de_unidad(con, usuario, unidad_id, fecha, frutas))


@router.put("/unidades/{unidad_id}/precios/{producto_codigo}", response_model=Ok, summary="Definir el precio del día")
def guardar_precio(
    unidad_id: int, producto_codigo: str, datos: GuardarPrecio, usuario: UsuarioActual, servicios: ServiciosDep
) -> Ok:
    """
    Precio por kilo y tipo de pago de la fruta en la unidad para la fecha (vacío = hoy). En Precio
    Pactado es el tope. Corregir uno de hace más de 5 días solo lo hace un Admin.
    """
    if not usuario.tiene_permiso(PERMISO_PRECIOS, unidad_id):
        raise AccesoDenegado("No tienes el permiso precios_diarios en esta unidad.", codigo="sin_permiso")
    fruta = fruta_valida(servicios, producto_codigo)

    def accion(op: Operacion) -> None:
        fecha = datos.fecha or hoy(op.con)
        configuracion.guardar_precio(op, usuario, unidad_id, fruta, fecha, datos.precio_kg, datos.tipo_pago_id)

    en_transaccion(servicios, usuario, accion)
    return Ok()


@router.put("/unidades/{unidad_id}/tolerancias/{producto_codigo}", response_model=Ok, summary="Tolerancia (Admin)")
def guardar_tolerancia(
    unidad_id: int, producto_codigo: str, datos: GuardarTolerancia, usuario: UsuarioActual, servicios: ServiciosDep
) -> Ok:
    """Se copia a cada preboleta nueva; no cambia las ya creadas. Sin tolerancia no se crean preboletas de esa fruta."""
    if not usuario.es_admin:
        raise AccesoDenegado("Solo un Admin cambia tolerancias.")
    fruta = fruta_valida(servicios, producto_codigo)
    en_transaccion(
        servicios,
        usuario,
        lambda op: configuracion.guardar_tolerancia(op, usuario, unidad_id, fruta, datos.tolerancia_pct),
    )
    return Ok()


@router.put("/unidades/{unidad_id}/implementacion", response_model=Ok, summary="Fecha de implementación (Admin)")
def implementacion(unidad_id: int, datos: Implementacion, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    """Desde cuándo opera la unidad. Vacío = pausada: sus boletas no se ligan ni generan incidencias."""
    en_transaccion(
        servicios,
        usuario,
        lambda op: configuracion.guardar_implementacion(op, usuario, unidad_id, datos.inicio_operacion_at),
    )
    return Ok()


# ----------------------------------------------------------------------
# Colaboradores (Admin)
# ----------------------------------------------------------------------
@router.get(
    "/admin/colaboradores", response_model=list[ColaboradorAdmin], summary="Colaboradores con canales y permisos"
)
def colaboradores_admin(
    usuario: UsuarioActual, servicios: ServiciosDep, buscar: str | None = Query(default=None, max_length=100)
) -> list[ColaboradorAdmin]:
    """El rol viene de Azure; aquí se ven y definen canales de aviso y permisos por unidad."""
    with servicios.bd.transaccion() as con:
        return [ColaboradorAdmin.model_validate(c) for c in colaboradores.listar_admin(con, usuario, buscar)]


@router.put(
    "/colaboradores/{colaborador_id}/permisos/{permiso}/{unidad_id}", response_model=Ok, summary="Dar un permiso"
)
def dar_permiso(
    colaborador_id: int,
    permiso: Literal["precios_diarios", "autorizar_precios"],
    unidad_id: int,
    usuario: UsuarioActual,
    servicios: ServiciosDep,
) -> Ok:
    en_transaccion(
        servicios,
        usuario,
        lambda op: colaboradores.cambiar_permiso(op, usuario, colaborador_id, permiso, unidad_id, otorgar=True),
    )
    return Ok()


@router.delete(
    "/colaboradores/{colaborador_id}/permisos/{permiso}/{unidad_id}", response_model=Ok, summary="Quitar un permiso"
)
def quitar_permiso(
    colaborador_id: int,
    permiso: Literal["precios_diarios", "autorizar_precios"],
    unidad_id: int,
    usuario: UsuarioActual,
    servicios: ServiciosDep,
) -> Ok:
    en_transaccion(
        servicios,
        usuario,
        lambda op: colaboradores.cambiar_permiso(op, usuario, colaborador_id, permiso, unidad_id, otorgar=False),
    )
    return Ok()


@router.put("/colaboradores/{colaborador_id}/canales", response_model=Ok, summary="Canales de aviso (correo, push)")
def canales(colaborador_id: int, datos: Canales, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    en_transaccion(
        servicios,
        usuario,
        lambda op: colaboradores.cambiar_canales(op, usuario, colaborador_id, datos.correo, datos.push),
    )
    return Ok()
