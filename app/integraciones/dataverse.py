"""
Proveedores y frutas consultados DIRECTO en Dataverse (CLAUDE.md §4). No hay tablas espejo.

- La API se identifica con su propio usuario de aplicación (credencial de cliente). El token
  lo pide y cachea azure-identity hasta poco antes de vencer; no se pide en cada consulta.
- Son tablas VIRTUALES de D365 F&O (prefijo mserp_): cada consulta viaja en vivo a F&O,
  puede tardar 1-3 s. Solo lectura.
- Toda consulta lleva SIEMPRE el filtro fijo de empresa y grupo, también la validación por código.
- El texto del usuario va como literal OData con las comillas simples duplicadas (anti inyección).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import Configuracion
from app.errores import ErrorServicioExterno, ServicioNoDisponible

log = logging.getLogger(__name__)

Fila = dict[str, Any]


@dataclass(frozen=True)
class DefinicionCatalogo:
    """Tabla y columnas de un catálogo de Dataverse, con su filtro fijo."""

    tabla: str  # Nombre lógico SINGULAR (así lo pide el SDK; la Web API usa el plural).
    col_clave: str
    col_nombre: str
    col_empresa: str
    empresa: str  # Cada entidad guarda la empresa con distinta capitalización.
    col_grupo: str
    grupo: str
    col_sitio: str = ""  # Solo proveedores: sitio de compra (= unidad de negocio).

    @property
    def columnas(self) -> list[str]:
        return [c for c in (self.col_clave, self.col_nombre, self.col_sitio) if c]


# Proveedores de materia prima de SIC.
PROVEEDORES = DefinicionCatalogo(
    tabla="mserp_retailvendvendorv3entity",
    col_clave="mserp_vendoraccountnumber",
    col_nombre="mserp_vendororganizationname",
    col_empresa="mserp_dataareaid",
    empresa="SIC",  # MAYÚSCULAS en esta tabla.
    col_grupo="mserp_vendorgroupid",
    grupo="MATPRIMA",
    col_sitio="mserp_defaultpurchasesiteid",
)

# Frutas: productos liberados de SIC con conjunto de producción MP.
FRUTAS = DefinicionCatalogo(
    tabla="mserp_ecoresreleasedproductv2entity",
    col_clave="mserp_itemnumber",
    col_nombre="mserp_productsearchname",
    col_empresa="mserp_dataareaid",
    empresa="sic",  # minúsculas en esta tabla.
    col_grupo="mserp_productionpoolid",
    grupo="MP",
)


@dataclass(frozen=True)
class Proveedor:
    cuenta: str
    nombre: str
    sitio: int | None  # Unidad de negocio sugerida (puede venir vacía en D365).


@dataclass(frozen=True)
class Fruta:
    codigo: str
    nombre: str


# ----------------------------------------------------------------------
# Construcción segura de filtros OData
# ----------------------------------------------------------------------
def literal_odata(texto: str) -> str:
    """
    Convierte texto del usuario en un literal OData seguro.
    En OData la comilla simple se escapa duplicándola: O'Brien -> 'O''Brien'.
    """
    return "'" + texto.replace("'", "''") + "'"


def _filtros_fijos(defi: DefinicionCatalogo) -> list[str]:
    """Condiciones que SIEMPRE aplican: empresa y grupo."""
    return [
        f"{defi.col_empresa} eq {literal_odata(defi.empresa)}",
        f"{defi.col_grupo} eq {literal_odata(defi.grupo)}",
    ]


def filtro_busqueda(defi: DefinicionCatalogo, texto: str) -> str:
    """(nombre o clave contienen el texto) y empresa y grupo. Texto vacío = todo el catálogo filtrado."""
    partes = _filtros_fijos(defi)
    texto = texto.strip()
    if texto:
        lit = literal_odata(texto)
        partes.insert(0, f"(contains({defi.col_nombre},{lit}) or contains({defi.col_clave},{lit}))")
    return " and ".join(partes)


def filtro_clave(defi: DefinicionCatalogo, clave: str) -> str:
    """Filtro exacto por cuenta / código, con los mismos filtros fijos que la búsqueda."""
    return " and ".join([f"{defi.col_clave} eq {literal_odata(clave.strip())}", *_filtros_fijos(defi)])


def _texto(fila: Fila, columna: str) -> str:
    valor = fila.get(columna)
    return str(valor).strip() if valor is not None else ""


def a_proveedor(fila: Fila) -> Proveedor:
    sitio = _texto(fila, PROVEEDORES.col_sitio)
    return Proveedor(
        cuenta=_texto(fila, PROVEEDORES.col_clave),
        nombre=_texto(fila, PROVEEDORES.col_nombre),
        sitio=int(sitio) if sitio.isdigit() else None,
    )


def a_fruta(fila: Fila) -> Fruta:
    return Fruta(codigo=_texto(fila, FRUTAS.col_clave), nombre=_texto(fila, FRUTAS.col_nombre))


# ----------------------------------------------------------------------
# Contrato y consultor real
# ----------------------------------------------------------------------
class CatalogosD365(Protocol):
    """Lo que la API necesita de Dataverse (las pruebas lo sustituyen)."""

    def buscar_proveedores(self, texto: str, limite: int) -> list[Proveedor]: ...
    def proveedor(self, cuenta: str) -> Proveedor | None: ...
    def buscar_frutas(self, texto: str, limite: int) -> list[Fruta]: ...
    def fruta(self, codigo: str) -> Fruta | None: ...


class ConsultorDataverse:
    """Implementación real con el SDK oficial (PowerPlatform-Dataverse-Client)."""

    def __init__(self, cfg: Configuracion) -> None:
        self._cfg = cfg
        self._cliente: Any = None

    def _sdk(self) -> Any:
        # Se crea al primer uso. El SDK y azure-identity cachean el token y los metadatos.
        if self._cliente is None:
            from azure.identity import ClientSecretCredential
            from PowerPlatform.Dataverse.client import DataverseClient

            credencial = ClientSecretCredential(
                tenant_id=self._cfg.entra_tenant_id,
                client_id=self._cfg.dataverse_client_id,
                client_secret=self._cfg.dataverse_client_secret.get_secret_value(),
            )
            self._cliente = DataverseClient(self._cfg.dataverse_url, credencial)
        return self._cliente

    def _consultar(self, defi: DefinicionCatalogo, filtro: str, limite: int, ordenar: bool) -> list[Fila]:
        try:
            resultado = self._sdk().records.list(
                defi.tabla,
                filter=filtro,
                select=defi.columnas,
                orderby=[f"{defi.col_nombre} asc"] if ordenar else None,
                top=limite,
            )
            return [dict(r.items()) for r in resultado]
        except Exception as exc:  # noqa: BLE001 - se traduce cualquier falla del SDK a un error claro
            raise _traducir(exc) from exc

    def buscar_proveedores(self, texto: str, limite: int) -> list[Proveedor]:
        filas = self._consultar(PROVEEDORES, filtro_busqueda(PROVEEDORES, texto), limite, ordenar=True)
        return [a_proveedor(f) for f in filas]

    def proveedor(self, cuenta: str) -> Proveedor | None:
        filas = self._consultar(PROVEEDORES, filtro_clave(PROVEEDORES, cuenta), 1, ordenar=False)
        return a_proveedor(filas[0]) if filas else None

    def buscar_frutas(self, texto: str, limite: int) -> list[Fruta]:
        filas = self._consultar(FRUTAS, filtro_busqueda(FRUTAS, texto), limite, ordenar=True)
        return [a_fruta(f) for f in filas]

    def fruta(self, codigo: str) -> Fruta | None:
        filas = self._consultar(FRUTAS, filtro_clave(FRUTAS, codigo), 1, ordenar=False)
        return a_fruta(filas[0]) if filas else None

    def cerrar(self) -> None:
        if self._cliente is not None and hasattr(self._cliente, "close"):
            self._cliente.close()


def _traducir(exc: Exception) -> Exception:
    """
    Errores del SDK → errores de la API. Por nombre de clase para no depender de su jerarquía
    interna (el SDK es joven). Lo que no se reconoce se trata como "Dataverse no responde" (503).
    """
    nombre = type(exc).__name__
    estado = getattr(exc, "status_code", None)
    mensaje = str(getattr(exc, "message", "") or exc)
    if nombre == "ClientAuthenticationError":
        log.error("Credencial de Dataverse rechazada: %s", mensaje)
        return ErrorServicioExterno(
            "La API no pudo autenticarse en Dataverse. Revisa DATAVERSE_CLIENT_ID y su secreto (pudo vencer).",
            codigo="dataverse_credencial",
        )
    if nombre == "MetadataError":
        log.error("Tabla de Dataverse no encontrada: %s", mensaje)
        return ErrorServicioExterno(
            "Tabla de Dataverse no encontrada. Habilítala en 'Available Finance and Operations Entities'.",
            codigo="dataverse_tabla",
        )
    if estado in (401, 403) or "prvRead" in mensaje:
        log.error("Sin permiso de lectura en Dataverse: %s", mensaje)
        return ErrorServicioExterno(
            "El usuario de aplicación no tiene permiso de Leer en Dataverse.", codigo="dataverse_sin_permiso"
        )
    if estado == 400:
        log.error("Dataverse rechazó la consulta: %s", mensaje)
        return ErrorServicioExterno("Dataverse rechazó la consulta.", codigo="dataverse_consulta")
    log.warning("Dataverse no disponible (%s): %s", nombre, mensaje)
    return ServicioNoDisponible(
        "Dynamics 365 (Dataverse) no responde. Intenta de nuevo en unos segundos.",
        codigo="dataverse_no_disponible",
        reintentar_en=5,
    )
