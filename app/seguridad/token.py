"""
Validación LOCAL de los access tokens de Microsoft del registro "Preboletas".

Mismas reglas que CoreAPI: firma RS256 con las llaves públicas del tenant, vigencia,
audiencia = Preboletas, emisor y tenant propios, y el permiso access_as_user en `scp`.
No se llama a Microsoft en cada petición: las llaves se cachean una hora.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient

from app.config import Configuracion
from app.errores import NoAutenticado, ServicioNoDisponible

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IdentidadToken:
    """Datos de la persona según su access token."""

    entra_oid: str
    nombre: str
    correo: str
    roles: tuple[str, ...]


class ProveedorLlaves(Protocol):
    """Devuelve la llave pública con la que se firmó un token (en pruebas, una llave propia)."""

    def llave_para(self, token: str) -> Any: ...


class LlavesMicrosoft:
    """Descarga y cachea (1 h) las llaves públicas (JWKS) del tenant."""

    def __init__(self, tenant_id: str) -> None:
        url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        self._cliente = PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=10)

    def llave_para(self, token: str) -> Any:
        try:
            return self._cliente.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError as exc:
            raise ServicioNoDisponible("No se pudieron obtener las llaves de Microsoft.", reintentar_en=5) from exc
        except jwt.PyJWKClientError as exc:
            raise NoAutenticado("Token firmado con una llave desconocida.", codigo="token_invalido") from exc


class ValidadorToken:
    def __init__(self, cfg: Configuracion, llaves: ProveedorLlaves) -> None:
        self._cfg = cfg
        self._llaves = llaves

    def validar(self, token: str) -> IdentidadToken:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=self._llaves.llave_para(token),
                algorithms=["RS256"],
                audience=self._cfg.audiencias,
                issuer=self._cfg.emisores,
                options={"require": ["exp", "iat", "aud", "iss", "oid", "tid"]},
                leeway=60,  # Tolerancia de reloj entre servidores.
            )
        except jwt.ExpiredSignatureError as exc:
            raise NoAutenticado("El token expiró.", codigo="token_expirado") from exc
        except jwt.InvalidAudienceError as exc:
            # Típico: se mandó un token de SICAR Core (u otra app) en lugar del de Preboletas.
            raise NoAutenticado("El token no fue emitido para Preboletas.", codigo="token_audiencia") from exc
        except jwt.InvalidTokenError as exc:
            raise NoAutenticado("Token inválido.", codigo="token_invalido") from exc

        if claims.get("tid") != self._cfg.entra_tenant_id:
            raise NoAutenticado("El token es de otra organización.", codigo="token_invalido")
        scp = str(claims.get("scp", ""))
        if self._cfg.entra_scope.lower() not in scp.lower().split():
            raise NoAutenticado("El token no incluye el permiso de la API.", codigo="token_sin_permiso")

        try:
            # Entra siempre manda el oid como UUID; otra cosa es un token alterado.
            oid = str(uuid.UUID(str(claims["oid"])))
        except ValueError as exc:
            raise NoAutenticado("Token inválido.", codigo="token_invalido") from exc

        correo = str(
            claims.get("email")
            or claims.get("upn")
            or claims.get("preferred_username")
            or claims.get("unique_name")
            or ""
        ).lower()
        return IdentidadToken(
            entra_oid=oid,
            nombre=str(claims.get("name") or correo),
            correo=correo,
            roles=tuple(sorted({str(r) for r in claims.get("roles") or []})),
        )
