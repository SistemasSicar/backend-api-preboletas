"""
Exporta el contrato OpenAPI de la API a openapi.json (para el panel, la app móvil o Postman).

    .venv\\Scripts\\python -m scripts.exportar_openapi

No necesita base de datos ni secretos: arma la app con una configuración de relleno.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import Configuracion
from app.main import crear_app


def main() -> None:
    cfg = Configuracion(
        _env_file=None,
        bd_url="postgresql://relleno@localhost/relleno",
        entra_tenant_id="relleno",
        entra_client_id="relleno",
        core_api_key="relleno",
        dataverse_url="https://relleno.crm.dynamics.com",
        dataverse_client_id="relleno",
        dataverse_client_secret="relleno",
    )
    esquema = crear_app(cfg, trabajos_de_fondo=False).openapi()
    destino = Path(__file__).resolve().parents[1] / "openapi.json"
    destino.write_text(json.dumps(esquema, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OpenAPI exportado a {destino} ({len(esquema['paths'])} rutas)")


if __name__ == "__main__":
    main()
