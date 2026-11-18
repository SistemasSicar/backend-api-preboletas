"""
Errores de la API.

Todas las respuestas de error tienen la misma forma (`RespuestaError`), con un `codigo`
estable que el panel o la app móvil usan para decidir qué mostrar. Es el mismo formato de CoreAPI.
"""

from __future__ import annotations

import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class RespuestaError(BaseModel):
    """Cuerpo estándar de cualquier error devuelto por el servicio."""

    codigo: str = Field(description="Código estable del error, en snake_case.", examples=["bascula_ocupada"])
    mensaje: str = Field(description="Mensaje legible para mostrar al usuario.")
    id_solicitud: str | None = Field(
        default=None, description="ID de la petición (encabezado X-Request-ID), útil para soporte."
    )


class ErrorApi(Exception):
    """Error controlado. Cada subclase fija su estado HTTP y su código."""

    estado: int = 500
    codigo: str = "error_interno"

    def __init__(self, mensaje: str, *, codigo: str | None = None, reintentar_en: int | None = None) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje
        if codigo:
            self.codigo = codigo
        self.reintentar_en = reintentar_en


class SolicitudInvalida(ErrorApi):
    """Regla de negocio o dato que no se cumple (el mensaje explica qué corregir)."""

    estado, codigo = 400, "validacion"


class NoAutenticado(ErrorApi):
    """Falta el token, está vencido o no es válido para esta API."""

    estado, codigo = 401, "no_autenticado"


class AccesoDenegado(ErrorApi):
    """La persona está autenticada pero no tiene el rol o permiso para la acción."""

    estado, codigo = 403, "rol_insuficiente"


class NoEncontrado(ErrorApi):
    """No existe, o existe pero el rol no lo puede ver (se responde igual a propósito)."""

    estado, codigo = 404, "no_encontrado"


class Conflicto(ErrorApi):
    """El estado actual impide la acción (báscula ocupada, ya resuelta, ya cerrada...)."""

    estado, codigo = 409, "conflicto"


class DemasiadasSolicitudes(ErrorApi):
    estado, codigo = 429, "demasiadas_solicitudes"


class ErrorServicioExterno(ErrorApi):
    """Un servicio externo respondió mal (configuración o permisos)."""

    estado, codigo = 502, "error_servicio_externo"


class ServicioNoDisponible(ErrorApi):
    """Un servicio externo no responde o está saturado; se puede reintentar."""

    estado, codigo = 503, "servicio_no_disponible"


# Documentación de respuestas de error para OpenAPI (se reutiliza en las rutas).
RESPUESTAS_COMUNES: dict[int | str, dict[str, object]] = {
    401: {"model": RespuestaError, "description": "Token ausente, vencido o inválido."},
    403: {"model": RespuestaError, "description": "Sin el rol o el permiso necesario."},
    404: {"model": RespuestaError, "description": "No existe o no es visible para tu rol."},
    503: {"model": RespuestaError, "description": "Base de datos o servicio externo no disponible; reintentar."},
}


def _id_solicitud(request: Request) -> str | None:
    valor = getattr(request.state, "id_solicitud", None)
    return valor if isinstance(valor, str) else None


def registrar_manejadores(app: FastAPI) -> None:
    """Conecta los manejadores de error a la aplicación."""

    @app.exception_handler(ErrorApi)
    async def _error_api(request: Request, exc: ErrorApi) -> JSONResponse:
        if exc.estado >= 500:
            log.warning("Error %s (%s): %s", exc.estado, exc.codigo, exc.mensaje)
        cuerpo = RespuestaError(codigo=exc.codigo, mensaje=exc.mensaje, id_solicitud=_id_solicitud(request))
        encabezados: dict[str, str] = {}
        if exc.reintentar_en is not None:
            encabezados["Retry-After"] = str(exc.reintentar_en)
        if exc.estado == 401:
            encabezados["WWW-Authenticate"] = "Bearer"
        return JSONResponse(status_code=exc.estado, content=cuerpo.model_dump(), headers=encabezados)

    @app.exception_handler(RequestValidationError)
    async def _validacion(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Se resume el primer problema para que el mensaje sea legible.
        errores = exc.errors()
        detalle = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', '')}" for e in errores[:3])
        cuerpo = RespuestaError(
            codigo="validacion", mensaje=detalle or "Datos inválidos.", id_solicitud=_id_solicitud(request)
        )
        return JSONResponse(status_code=422, content=cuerpo.model_dump())

    @app.exception_handler(psycopg.OperationalError)
    async def _bd_caida(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
        log.error("Base de datos no disponible: %s", exc)
        cuerpo = RespuestaError(
            codigo="bd_no_disponible",
            mensaje="La base de datos no está disponible.",
            id_solicitud=_id_solicitud(request),
        )
        return JSONResponse(status_code=503, content=cuerpo.model_dump(), headers={"Retry-After": "5"})

    @app.exception_handler(Exception)
    async def _inesperado(request: Request, exc: Exception) -> JSONResponse:
        # Nunca se expone el detalle interno al cliente; solo queda en el log.
        log.exception("Error no controlado")
        cuerpo = RespuestaError(
            codigo="error_interno", mensaje="Ocurrió un error inesperado.", id_solicitud=_id_solicitud(request)
        )
        return JSONResponse(status_code=500, content=cuerpo.model_dump())
