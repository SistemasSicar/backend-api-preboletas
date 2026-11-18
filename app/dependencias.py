"""
Contenedor de dependencias y autenticación de las rutas.

Toda ruta de personas recibe `Authorization: Bearer <access_token del registro Preboletas>`.
  * IdentidadActual: token válido (lo usa GET /yo, que vincula la cuenta con CoreAPI).
  * UsuarioActual: token válido + colaborador vinculado + algún rol de Preboletas.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.bd import BaseDatos
from app.config import Configuracion
from app.errores import AccesoDenegado, NoAutenticado
from app.integraciones.core_api import ClienteCore
from app.integraciones.dataverse import CatalogosD365
from app.integraciones.expo_push import EnviadorPush
from app.integraciones.sincronizador_core import SincronizadorCore
from app.seguridad.limitador import LimitadorPorMinuto
from app.seguridad.tickets_ws import TicketsWs
from app.seguridad.token import IdentidadToken, ValidadorToken
from app.servicios.colaboradores import cargar_usuario
from app.tiempo_real.hub import HubTiempoReal
from app.usuario import ROLES_APP, Usuario


@dataclass
class Servicios:
    cfg: Configuracion
    bd: BaseDatos
    validador: ValidadorToken
    dataverse: CatalogosD365
    core: ClienteCore
    push: EnviadorPush
    hub: HubTiempoReal = field(default_factory=HubTiempoReal)
    tickets: TicketsWs = field(default_factory=TicketsWs)
    limitador: LimitadorPorMinuto | None = None
    # Vinculación con CoreAPI en segundo plano (si es None se crea con `core`).
    sincronizador: SincronizadorCore | None = None
    # Para /salud: se marca en el arranque cuáles hilos de fondo están vivos.
    hilos: list[threading.Thread] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.limitador is None:
            self.limitador = LimitadorPorMinuto(self.cfg.publico_limite_por_minuto)
        if self.sincronizador is None:
            self.sincronizador = SincronizadorCore(self.core)

    def sincronizar_cuenta(self, identidad: IdentidadToken) -> None:
        """Pide a CoreAPI, sin esperar, que guarde el vínculo, el nombre y la copia de roles."""
        if self.sincronizador is not None:
            self.sincronizador.programar(identidad)


def obtener_servicios(request: Request) -> Servicios:
    servicios: Servicios = request.app.state.servicios
    return servicios


ServiciosDep = Annotated[Servicios, Depends(obtener_servicios)]

_bearer = HTTPBearer(auto_error=False, description="Access token de Microsoft emitido para Preboletas.")


def identidad_actual(
    servicios: ServiciosDep,
    credenciales: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> IdentidadToken:
    if credenciales is None or credenciales.scheme.lower() != "bearer":
        raise NoAutenticado("Falta el encabezado Authorization: Bearer <token>.")
    identidad = servicios.validador.validar(credenciales.credentials)
    if not ROLES_APP.intersection(identidad.roles):
        raise AccesoDenegado(
            "Tu cuenta no tiene un rol de Preboletas (Admin, Comprador o Basculista). Pídelo al administrador.",
            codigo="rol_insuficiente",
        )
    return identidad


IdentidadActual = Annotated[IdentidadToken, Depends(identidad_actual)]


def usuario_actual(servicios: ServiciosDep, identidad: IdentidadActual) -> Usuario:
    """Colaborador vinculado (la vinculación ocurre en GET /yo al iniciar sesión)."""
    with servicios.bd.transaccion() as con:
        usuario = cargar_usuario(con, identidad)
    if usuario is None:
        raise AccesoDenegado(
            "Tu cuenta no está vinculada a un colaborador. Inicia sesión de nuevo.", codigo="sin_vinculacion"
        )
    return usuario


UsuarioActual = Annotated[Usuario, Depends(usuario_actual)]
