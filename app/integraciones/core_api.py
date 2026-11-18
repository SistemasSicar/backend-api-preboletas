"""
Cliente de CoreAPI. Preboletas no puede escribir en `core`, por eso se lo pide a CoreAPI:

- `POST /internas/acceso/verificar`: en `GET /yo` (inicio de sesión del panel o de la app). CoreAPI
  vincula la cuenta de Microsoft con su colaborador (la primera vez, por correo), actualiza su nombre
  y correo y reemplaza su copia de roles de Preboletas en core.colaborador_acceso.
- `POST /internas/boletas/{folio}/actualizar`: botón "Actualizar boleta". CoreAPI lee la boleta en vivo
  de Dataverse y la guarda en core.boletas con las mismas reglas que Power Automate.
"""

from __future__ import annotations

import logging
from typing import Protocol
from urllib.parse import quote

import httpx

from app.config import CLAVE_APP, Configuracion
from app.errores import AccesoDenegado, Conflicto, ErrorServicioExterno, NoEncontrado, ServicioNoDisponible
from app.seguridad.token import IdentidadToken

log = logging.getLogger(__name__)

# Leer la boleta en Dataverse puede tardar varios segundos (tabla virtual de F&O + token).
TIMEOUT_ACTUALIZAR_BOLETA = 30.0


class VerificadorAcceso(Protocol):
    """Contrato (las pruebas lo sustituyen)."""

    def verificar(self, identidad: IdentidadToken) -> int:
        """Vincula la cuenta y devuelve el id del colaborador."""
        ...


class ClienteCore(VerificadorAcceso, Protocol):
    """Todo lo que la API pide a CoreAPI (las pruebas lo sustituyen)."""

    def actualizar_boleta(self, folio: str) -> str:
        """Pide a CoreAPI releer la boleta en Dataverse. Devuelve su `resultado` (ej. `sin_cambios`)."""
        ...


class ClienteCoreApi:
    def __init__(self, cfg: Configuracion) -> None:
        self._cfg = cfg

    def verificar(self, identidad: IdentidadToken) -> int:
        cuerpo = {
            "app": CLAVE_APP,
            "entra_oid": identidad.entra_oid,
            "correo": identidad.correo,
            "nombre": identidad.nombre,
            "roles": list(identidad.roles),
        }
        try:
            r = httpx.post(
                f"{self._cfg.core_url}/internas/acceso/verificar",
                json=cuerpo,
                headers={"X-Api-Key": self._cfg.core_api_key.get_secret_value()},
                timeout=self._cfg.core_timeout_segundos,
            )
        except httpx.HTTPError as exc:
            log.error("CoreAPI no responde: %s", exc)
            raise ServicioNoDisponible(
                "No se pudo verificar tu cuenta: CoreAPI no responde.", codigo="core_no_disponible", reintentar_en=5
            ) from exc

        if r.status_code == 200:
            return int(r.json()["colaborador_id"])
        codigo = _codigo(r)
        if r.status_code == 404:
            raise AccesoDenegado(
                "Tu cuenta no está dada de alta como colaborador de SICAR. Pide tu alta al administrador.",
                codigo="sin_vinculacion",
            )
        if r.status_code == 409:
            raise Conflicto(
                "Tu correo de Microsoft ya pertenece a otro colaborador. Lo resuelve un administrador.",
                codigo="correo_duplicado",
            )
        # 400 app_desconocida, 401 llave_invalida, 403 llave_otra_app: configuración del servidor.
        log.error("CoreAPI rechazó la verificación: HTTP %s %s", r.status_code, codigo)
        raise ErrorServicioExterno(
            f"CoreAPI rechazó la verificación ({codigo}). Revisa CORE_API_KEY y la app en core.apps.",
            codigo="core_configuracion",
        )

    def actualizar_boleta(self, folio: str) -> str:
        try:
            r = httpx.post(
                f"{self._cfg.core_url}/internas/boletas/{quote(folio, safe='')}/actualizar",
                headers={"X-Api-Key": self._cfg.core_api_key.get_secret_value()},
                timeout=max(self._cfg.core_timeout_segundos, TIMEOUT_ACTUALIZAR_BOLETA),
            )
        except httpx.HTTPError as exc:
            log.error("CoreAPI no responde al actualizar la boleta %s: %s", folio, exc)
            raise ServicioNoDisponible(
                "No se pudo actualizar la boleta: CoreAPI no responde.", codigo="core_no_disponible", reintentar_en=5
            ) from exc

        if r.status_code == 200:
            return str(r.json().get("resultado", ""))
        codigo, mensaje = _codigo(r), _mensaje(r)
        if r.status_code == 404:
            raise NoEncontrado(f"La boleta {folio} no existe en D365.", codigo="boleta_no_encontrada")
        if r.status_code == 503:
            # Dataverse no responde o CoreAPI no lo tiene configurado: se transmite el motivo.
            raise ServicioNoDisponible(mensaje or "D365 no responde.", codigo=codigo, reintentar_en=5)
        if r.status_code in (401, 403):
            log.error("CoreAPI rechazó la llave al actualizar la boleta: HTTP %s %s", r.status_code, codigo)
            raise ErrorServicioExterno(
                f"CoreAPI rechazó la llave ({codigo}). Revisa CORE_API_KEY.", codigo="core_configuracion"
            )
        log.error("CoreAPI no pudo actualizar la boleta %s: HTTP %s %s %s", folio, r.status_code, codigo, mensaje)
        raise ErrorServicioExterno(mensaje or f"CoreAPI respondió HTTP {r.status_code}.", codigo=codigo)


def _codigo(r: httpx.Response) -> str:
    try:
        return str(r.json().get("codigo", r.status_code))
    except ValueError:
        return str(r.status_code)


def _mensaje(r: httpx.Response) -> str:
    try:
        return str(r.json().get("mensaje", ""))
    except ValueError:
        return ""
