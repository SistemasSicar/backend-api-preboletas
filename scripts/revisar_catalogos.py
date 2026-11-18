"""
Revisa (SOLO LECTURA) que la base tenga lo que la API necesita: los tipos de movimiento que
escribe, los tipos de pago, la app 'preboletas' en core.apps, y muestra por unidad su fecha de
implementación, tolerancias y cajas por fruta.

    .venv\\Scripts\\python -m scripts.revisar_catalogos

Usa BD_URL de .env / .env.local (no muestra su valor).
"""

from __future__ import annotations

from app.bd import BaseDatos
from app.config import obtener_configuracion
from app.main import TIPOS_MOVIMIENTO_USADOS


def main() -> None:
    cfg = obtener_configuracion()
    bd = BaseDatos(cfg.bd_url.get_secret_value(), 1, 1)
    bd.abrir()
    try:
        with bd.transaccion() as con:
            claves = {str(f["clave"]) for f in con.execute("SELECT clave FROM preboletas.tipos_movimiento")}
            faltan = sorted(TIPOS_MOVIMIENTO_USADOS - claves)
            print("Tipos de movimiento:", "completos" if not faltan else f"FALTAN {', '.join(faltan)}")
            pagos = [str(f["clave"]) for f in con.execute("SELECT clave FROM preboletas.tipos_pago ORDER BY id")]
            print("Tipos de pago:", ", ".join(pagos))
            for clave in ("rendimiento", "pactado", "corrida"):
                if clave not in pagos:
                    print(f"  FALTA el tipo de pago '{clave}'")
            app = con.execute("SELECT id FROM core.apps WHERE clave = 'preboletas'").fetchone()
            print("App 'preboletas' en core.apps:", "sí" if app else "NO (CoreAPI responderá app_desconocida)")
            for u in con.execute(
                """
                SELECT u.id, u.nombre, uc.inicio_operacion_at,
                       (SELECT string_agg(producto_codigo || ' ' || tolerancia_pct || '%', ', ')
                          FROM preboletas.tolerancias t WHERE t.unidad_negocio_id = u.id) AS tolerancias
                  FROM core.unidades_negocio u
                  LEFT JOIN preboletas.unidades_config uc ON uc.unidad_negocio_id = u.id
                 ORDER BY u.nombre
                """
            ):
                print(
                    f"Unidad {u['id']} {u['nombre']}: opera desde {u['inicio_operacion_at'] or '— (sin fecha)'}; "
                    f"tolerancias: {u['tolerancias'] or 'ninguna'}"
                )
            cajas = con.execute(
                "SELECT tc.nombre, tc.kg_por_caja, tc.activo, string_agg(tcp.producto_codigo, ', ') AS frutas "
                "FROM preboletas.tipos_caja tc "
                "LEFT JOIN preboletas.tipos_caja_productos tcp ON tcp.tipo_caja_id = tc.id "
                "GROUP BY tc.id ORDER BY tc.orden, tc.nombre"
            ).fetchall()
            for c in cajas:
                estado = "" if c["activo"] else " (inactiva)"
                print(f"Caja {c['nombre']} {c['kg_por_caja']} kg{estado}: frutas {c['frutas'] or 'ninguna'}")
    finally:
        bd.cerrar()


if __name__ == "__main__":
    main()
