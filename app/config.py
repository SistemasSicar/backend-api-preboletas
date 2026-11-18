"""
Configuración de la API de Preboletas.

Se lee de variables de entorno y de dos archivos: `.env` (configuración sin secretos)
y `.env.local` (secretos). Si una variable está en los dos, gana `.env.local`.
Nada sensible vive en el código.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Clave de esta app en core.apps (CoreAPI guarda los roles de Azure por app).
CLAVE_APP = "preboletas"


class Entorno(StrEnum):
    DESARROLLO = "desarrollo"
    PRUEBAS = "pruebas"
    PRODUCCION = "produccion"


def _lista_csv(valor: object) -> object:
    """Permite escribir listas en el .env separadas por comas."""
    if isinstance(valor, str):
        return [v.strip() for v in valor.split(",") if v.strip()]
    return valor


class Configuracion(BaseSettings):
    """Configuración completa de la API de Preboletas."""

    model_config = SettingsConfigDict(
        # El orden importa: el último archivo gana (los secretos de .env.local mandan).
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Un error de configuración nunca debe imprimir los valores leídos (hay secretos).
        hide_input_in_errors=True,
    )

    # ------------------------------------------------------------------
    # General
    # ------------------------------------------------------------------
    entorno: Entorno = Entorno.DESARROLLO
    nombre_servicio: str = "API de Preboletas"
    version: str = "1.0.0"
    # Orígenes del navegador permitidos (el panel). El panel llama desde su servidor;
    # el navegador solo abre el WebSocket.
    cors_origenes: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Base del enlace público del QR: <URL_PUBLICA>/qr/<token>. La página la sirve el panel.
    url_publica: str = "http://localhost:7301"
    publico_limite_por_minuto: int = Field(default=30, ge=1, le=10_000)

    # ------------------------------------------------------------------
    # Base de datos (usuario preboletas_api: lee core, escribe preboletas)
    # ------------------------------------------------------------------
    bd_url: SecretStr
    bd_pool_min: int = Field(default=1, ge=1, le=20)
    bd_pool_max: int = Field(default=10, ge=1, le=50)

    # ------------------------------------------------------------------
    # Entra ID: tokens del registro "Preboletas"
    # ------------------------------------------------------------------
    entra_tenant_id: str
    entra_client_id: str
    # Permiso de "Exponer una API" que debe traer el token (claim scp).
    entra_scope: str = "access_as_user"

    # ------------------------------------------------------------------
    # CoreAPI (vinculación de la cuenta en GET /yo)
    # ------------------------------------------------------------------
    core_url: str = "http://localhost:7220"
    core_api_key: SecretStr
    core_timeout_segundos: float = 10.0

    # ------------------------------------------------------------------
    # Dataverse (proveedores y frutas, consultados en el momento)
    # ------------------------------------------------------------------
    dataverse_url: str
    dataverse_client_id: str
    dataverse_client_secret: SecretStr

    # ------------------------------------------------------------------
    @field_validator("cors_origenes", mode="before")
    @classmethod
    def _separar(cls, valor: object) -> object:
        return _lista_csv(valor)

    @field_validator("url_publica", "core_url", "dataverse_url")
    @classmethod
    def _sin_diagonal_final(cls, valor: str) -> str:
        return valor.strip().rstrip("/")

    @field_validator("dataverse_url")
    @classmethod
    def _sin_api(cls, valor: str) -> str:
        # El SDK arma la URL de la Web API a partir del entorno; con ".api." apunta mal.
        if ".api." in valor.lower():
            raise ValueError("DATAVERSE_URL va sin '.api' (ej. https://<entorno>.crm.dynamics.com).")
        return valor

    @model_validator(mode="after")
    def _validar(self) -> Self:
        if self.bd_pool_min > self.bd_pool_max:
            raise ValueError("BD_POOL_MIN no puede ser mayor que BD_POOL_MAX.")
        return self

    # ------------------------------------------------------------------
    @property
    def audiencias(self) -> list[str]:
        """Valores válidos del claim aud (tokens v1 y v2)."""
        return [self.entra_client_id, f"api://{self.entra_client_id}"]

    @property
    def emisores(self) -> list[str]:
        """Valores válidos del claim iss (tokens v1 y v2)."""
        return [
            f"https://login.microsoftonline.com/{self.entra_tenant_id}/v2.0",
            f"https://sts.windows.net/{self.entra_tenant_id}/",
        ]

    def url_qr(self, token: str) -> str:
        """Enlace público del QR de una preboleta."""
        return f"{self.url_publica}/qr/{token}"


@lru_cache
def obtener_configuracion() -> Configuracion:
    """Instancia única de configuración (se lee una sola vez)."""
    return Configuracion()  # los obligatorios vienen del entorno
