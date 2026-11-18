"""
Piezas comunes a todas las operaciones que escriben.

`Operacion` envuelve la conexión de UNA transacción y a quien actúa (o el sistema) y concentra
los tres efectos secundarios que toda acción debe dejar (CLAUDE.md §6):
  * un renglón en la bitácora `movimientos`,
  * un aviso de tiempo real con `pg_notify('preboletas_eventos', …)`: PostgreSQL lo entrega
    solo si la transacción confirma, así nunca se avisa algo que se revirtió,
  * notificaciones a las personas (campana `app` + sus canales `correo` / `push` pendientes).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from app.bd import Conexion
from app.config import CLAVE_APP
from app.errores import NoEncontrado
from app.usuario import ROL_ADMIN, ROL_BASCULISTA, Usuario

CANAL_EVENTOS = "preboletas_eventos"

# Estados de D365 (mserp_ballotstatus). Cerrada = Validated, Completed o Confirmed.
D365_CANCELADA = 200000005
D365_CERRADAS = (200000002, 200000003, 200000006)

TIPOS_CANTIDAD = ("sobre_entrega", "faltante")
TIPOS_CONCILIACION = ("proveedor_distinto", "sin_preboleta", "cambio_proveedor")
NOMBRE_INCIDENCIA = {
    "cierre_manual": "Cierre a mano",
    "sobre_entrega": "Sobre-entrega",
    "faltante": "Faltante",
    "proveedor_distinto": "Proveedor distinto",
    "sin_preboleta": "Sin preboleta",
    "cambio_proveedor": "Cambio de proveedor",
}
NOMBRE_ESTADO_PB = {
    "pendiente": "Pendiente",
    "en_bascula": "En báscula",
    "en_proceso": "En proceso",
    "terminado": "Terminado",
    "terminado_con_incidencias": "Terminado con incidencias",
}


def folio(preboleta_id: int) -> str:
    """Folio visible de una preboleta. No se guarda: se deriva del id."""
    return f"PB-{preboleta_id:04d}"


def dinero(valor: Decimal | float | None) -> str:
    return "—" if valor is None else f"${Decimal(valor):,.2f}"


def kilos(valor: Decimal | float | None) -> str:
    return f"{Decimal(valor or 0):,.2f} kg"


def _json(valor: Any) -> Any:
    """Convierte Decimal y fechas para poder mandarlos en JSON."""
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, date):
        return valor.isoformat()
    raise TypeError(f"No serializable: {type(valor).__name__}")


class Operacion:
    """Una acción de negocio dentro de una transacción. `usuario=None` = la hace el sistema."""

    def __init__(self, con: Conexion, usuario: Usuario | None) -> None:
        self.con = con
        self.usuario = usuario

    @property
    def quien(self) -> int | None:
        return self.usuario.colaborador_id if self.usuario else None

    @property
    def nombre(self) -> str:
        return self.usuario.nombre if self.usuario else "Sistema"

    # ------------------------------------------------------------------
    def mov(
        self,
        tipo: str,
        unidad_negocio_id: int,
        descripcion: str,
        *,
        preboleta_id: int | None = None,
        recepcion_id: int | None = None,
        boleta_id: int | None = None,
        incidencia_id: int | None = None,
        propuesta_id: int | None = None,
        datos: dict[str, Any] | None = None,
        sistema: bool = False,
    ) -> None:
        """Agrega un renglón a la bitácora (solo inserción). `sistema=True` lo registra sin persona."""
        self.con.execute(
            """
            INSERT INTO preboletas.movimientos
                (unidad_negocio_id, tipo, preboleta_id, recepcion_id, boleta_id, incidencia_id,
                 propuesta_id, colaborador_id, descripcion, datos)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                unidad_negocio_id,
                tipo,
                preboleta_id,
                recepcion_id,
                boleta_id,
                incidencia_id,
                propuesta_id,
                None if sistema else self.quien,
                descripcion,
                Jsonb(datos, dumps=lambda d: json.dumps(d, default=_json)) if datos is not None else None,
            ),
        )

    def evento(self, nombre: str, **datos: Any) -> None:
        """
        Aviso de tiempo real. Lleva solo ids (nunca datos sensibles): el cliente vuelve a pedir
        el detalle por REST. Debe incluir `unidad_negocio_id` y, si aplica, `comprador_id`
        o `destinatario_id`: el reparto por rol del WebSocket se decide con ellos.
        """
        carga = json.dumps({"evento": nombre, **datos}, default=_json)
        self.con.execute("SELECT pg_notify(%s, %s)", (CANAL_EVENTOS, carga))

    def notificar(
        self,
        destinatarios: Iterable[int | None],
        mensaje: str,
        *,
        preboleta_id: int | None = None,
        incidencia_id: int | None = None,
        propuesta_id: int | None = None,
        excluir_a_quien_actua: bool = False,
    ) -> None:
        """
        Un aviso por persona (sin repetir). El canal `app` (la campana) siempre se crea y nace
        enviado; `correo` y `push` nacen pendientes según los canales de cada persona y los envía
        el trabajo de fondo.
        """
        vistos: set[int] = set()
        for d in destinatarios:
            if d is None or d in vistos or (excluir_a_quien_actua and d == self.quien):
                continue
            vistos.add(d)
            fila = self.con.execute(
                """
                INSERT INTO preboletas.notificaciones
                    (preboleta_id, incidencia_id, propuesta_id, destinatario_id, canal, mensaje, estado, enviada_at)
                VALUES (%s, %s, %s, %s, 'app', %s, 'enviada', now())
                RETURNING id
                """,
                (preboleta_id, incidencia_id, propuesta_id, d, mensaje),
            ).fetchone()
            self.con.execute(
                """
                INSERT INTO preboletas.notificaciones
                    (preboleta_id, incidencia_id, propuesta_id, destinatario_id, canal, mensaje)
                SELECT %s, %s, %s, c.colaborador_id, c.canal, %s
                  FROM preboletas.canales_colaborador c
                 WHERE c.colaborador_id = %s
                """,
                (preboleta_id, incidencia_id, propuesta_id, mensaje, d),
            )
            assert fila is not None
            self.evento("notificacion.nueva", destinatario_id=d, notificacion_id=int(fila["id"]))


# ----------------------------------------------------------------------
# Destinatarios (por rol de Azure, según la copia que CoreAPI guarda en cada login)
# ----------------------------------------------------------------------
_SQL_POR_ROL = """
    SELECT ca.colaborador_id
      FROM core.colaborador_acceso ca
      JOIN core.apps a ON a.id = ca.app_id AND a.clave = %s
      JOIN core.colaboradores c ON c.id = ca.colaborador_id
     WHERE ca.rol = %s
"""


def admins(con: Conexion) -> list[int]:
    filas = con.execute(_SQL_POR_ROL, (CLAVE_APP, ROL_ADMIN)).fetchall()
    return [int(f["colaborador_id"]) for f in filas]


def basculistas(con: Conexion, unidad_negocio_id: int) -> list[int]:
    filas = con.execute(
        _SQL_POR_ROL + " AND c.unidad_negocio_id = %s", (CLAVE_APP, ROL_BASCULISTA, unidad_negocio_id)
    ).fetchall()
    return [int(f["colaborador_id"]) for f in filas]


def autorizadores(con: Conexion, unidad_negocio_id: int) -> list[int]:
    """Quienes tienen autorizar_precios en la unidad; si nadie, los Admin."""
    filas = con.execute(
        "SELECT colaborador_id FROM preboletas.permisos_colaborador WHERE permiso = 'autorizar_precios' "
        "AND unidad_negocio_id = %s",
        (unidad_negocio_id,),
    ).fetchall()
    return [int(f["colaborador_id"]) for f in filas] or admins(con)


# ----------------------------------------------------------------------
# Consultas pequeñas que usan varios servicios
# ----------------------------------------------------------------------
def nombre_unidad(con: Conexion, unidad_negocio_id: int) -> str:
    fila = con.execute("SELECT nombre FROM core.unidades_negocio WHERE id = %s", (unidad_negocio_id,)).fetchone()
    return str(fila["nombre"]) if fila else str(unidad_negocio_id)


def nombre_colaborador(con: Conexion, colaborador_id: int | None) -> str:
    if colaborador_id is None:
        return "Sistema"
    fila = con.execute("SELECT nombre, correo FROM core.colaboradores WHERE id = %s", (colaborador_id,)).fetchone()
    return str(fila["nombre"] or fila["correo"]) if fila else f"#{colaborador_id}"


def hoy(con: Conexion) -> date:
    """Fecha de hoy en México (core.hoy()), nunca la del servidor."""
    fila = con.execute("SELECT core.hoy() AS hoy").fetchone()
    assert fila is not None
    valor: date = fila["hoy"]
    return valor


def tipo_pago_id(con: Conexion, clave: str) -> int:
    fila = con.execute("SELECT id FROM preboletas.tipos_pago WHERE clave = %s", (clave,)).fetchone()
    if fila is None:
        raise RuntimeError(f"No existe el tipo de pago '{clave}' en preboletas.tipos_pago.")
    return int(fila["id"])


def nombre_tipo_pago(con: Conexion, tipo_id: int | None) -> str:
    if tipo_id is None:
        return "—"
    fila = con.execute("SELECT nombre FROM preboletas.tipos_pago WHERE id = %s", (tipo_id,)).fetchone()
    return str(fila["nombre"]) if fila else str(tipo_id)


def precio_txt(con: Conexion, precio: Decimal | None, tipo_id: int | None) -> str:
    """'tope $X/kg' en Precio Pactado (el precio del día es el tope), '$X/kg' en otros, o 'sin asignar'."""
    if precio is None:
        return "sin asignar"
    tope = tipo_id is not None and tipo_id == tipo_pago_id(con, "pactado")
    return f"{'tope ' if tope else ''}{dinero(precio)}/kg"


def precio_dia(con: Conexion, unidad_negocio_id: int, producto_codigo: str, fecha: date) -> dict[str, Any] | None:
    """Precio del día (unidad + fruta + fecha). Sin fila = sin asignar."""
    return con.execute(
        "SELECT precio_kg, tipo_pago_id FROM preboletas.precios_diarios "
        "WHERE unidad_negocio_id = %s AND producto_codigo = %s AND fecha = %s",
        (unidad_negocio_id, producto_codigo, fecha),
    ).fetchone()


def cargar_preboleta(con: Conexion, preboleta_id: int, *, bloquear: bool = False) -> dict[str, Any]:
    """
    Fila de preboletas.preboletas. `bloquear=True` la toma con FOR UPDATE: así dos cargas o
    cierres simultáneos de la misma preboleta no calculan el acumulado al mismo tiempo.
    """
    fila = con.execute(
        "SELECT * FROM preboletas.preboletas WHERE id = %s" + (" FOR UPDATE" if bloquear else ""), (preboleta_id,)
    ).fetchone()
    if fila is None:
        raise NoEncontrado("La preboleta no existe.")
    return dict(fila)
