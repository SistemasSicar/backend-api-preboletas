"""Ayudas compartidas por las rutas."""

from __future__ import annotations

from collections.abc import Callable

from app.dependencias import Servicios
from app.errores import SolicitudInvalida
from app.integraciones.dataverse import Fruta, Proveedor
from app.servicios.comun import Operacion
from app.usuario import Usuario


def en_transaccion[T](servicios: Servicios, usuario: Usuario | None, accion: Callable[[Operacion], T]) -> T:
    """Ejecuta una acción de negocio en UNA transacción (todo o nada, incluidos bitácora y avisos)."""
    with servicios.bd.transaccion() as con:
        return accion(Operacion(con, usuario))


def fruta_valida(servicios: Servicios, codigo: str) -> Fruta:
    """
    Confirma en Dataverse que la fruta existe y es de SIC con conjunto MP, y devuelve su nombre.
    Se hace ANTES de abrir la transacción (Dataverse puede tardar segundos). Si Dataverse no
    responde, el error 503 sale de aquí y no se guarda nada.
    """
    fruta = servicios.dataverse.fruta(codigo)
    if fruta is None:
        raise SolicitudInvalida(
            "Esa fruta no existe en D365 o no está admitida (conjunto de producción MP).", codigo="fruta_inexistente"
        )
    return fruta


def proveedor_valido(servicios: Servicios, cuenta: str) -> Proveedor:
    proveedor = servicios.dataverse.proveedor(cuenta)
    if proveedor is None:
        raise SolicitudInvalida(
            "Ese proveedor no existe en D365 (o no es de materia prima de SIC).", codigo="proveedor_inexistente"
        )
    return proveedor
