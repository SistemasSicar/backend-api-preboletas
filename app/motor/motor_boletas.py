"""
Motor de boletas: decide qué hacer con cada boleta de D365 que llega a core.boletas.

CoreAPI escribe la boleta (desde Power Automate) y la base avisa con NOTIFY core_boletas.
Aquí se procesa una boleta a la vez, con un candado por boleta (pg_advisory_xact_lock), y el
proceso es IDEMPOTENTE: el barrido de respaldo puede volver a pasar la misma boleta sin efectos dobles.

Decisiones (CLAUDE.md §6 y el prototipo):
  1. Unidad sin fecha de implementación, o 1ra pesada anterior a ella → se ignora.
  2. Cancelada en D365 → si estaba ligada, la carga deja de contar (y se reabre la preboleta);
     si no, se cancelan sus incidencias de conciliación.
  3. 1ra pesada sin ligar: si hay camión en el slot y es el mismo proveedor → se liga y se libera
     la báscula; proveedor distinto → incidencia `proveedor_distinto` (también libera); slot libre →
     `sin_preboleta`.
  4. Ligada: si D365 le cambió el proveedor → se desliga y nace `cambio_proveedor`; si la 2da pesada
     la completó → salida de báscula y se evalúa el cierre de la preboleta.
  Los ajustes de peso posteriores (Correction) no cambian nada en Preboletas.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from app.bd import Conexion
from app.servicios import cantidad
from app.servicios.comun import (
    D365_CANCELADA,
    NOMBRE_INCIDENCIA,
    TIPOS_CONCILIACION,
    Operacion,
    admins,
    basculistas,
    cargar_preboleta,
    folio,
    kilos,
    nombre_unidad,
)
from app.servicios.recepciones import bloquear_slot, liberar_slot

log = logging.getLogger(__name__)

# Primer número de los candados de dos llaves (723 = Preboletas); el segundo es la boleta.
CANDADO_BOLETAS = 723
# Tolerancia de reloj entre D365 y esta base al comparar la entrada a báscula con la 1ra pesada.
TOLERANCIA_RELOJ = timedelta(minutes=10)


def bloquear_boleta(con: Conexion, boleta_id: int) -> None:
    """Candado de transacción por boleta: el motor y la conciliación nunca la tocan a la vez."""
    con.execute("SELECT pg_advisory_xact_lock(%s, %s)", (CANDADO_BOLETAS, boleta_id % 2_147_483_647))


def procesar_boleta(con: Conexion, boleta_id: int) -> None:
    """Punto de entrada: lo llaman la escucha de NOTIFY, el barrido y la conciliación."""
    bloquear_boleta(con, boleta_id)
    b = con.execute("SELECT * FROM core.boletas WHERE id = %s", (boleta_id,)).fetchone()
    if b is None:
        return
    boleta = dict(b)
    op = Operacion(con, None)

    if boleta["peso_bruto"] > 0 and not _en_operacion(con, boleta):
        return
    r = con.execute("SELECT * FROM preboletas.recepciones WHERE boleta_id = %s FOR UPDATE", (boleta_id,)).fetchone()
    recepcion = dict(r) if r else None

    if boleta["estado_d365_codigo"] == D365_CANCELADA:
        _cancelada(op, boleta, recepcion)
        return
    if boleta["peso_bruto"] <= 0:
        return
    if recepcion is None:
        recepcion = _sin_vincular(op, boleta)
        if recepcion is None:
            return
    _vinculada(op, boleta, recepcion)


# ----------------------------------------------------------------------
def _en_operacion(con: Conexion, b: dict[str, Any]) -> bool:
    """La unidad tiene fecha de implementación y la 1ra pesada es posterior a ella."""
    fila = con.execute(
        "SELECT inicio_operacion_at FROM preboletas.unidades_config WHERE unidad_negocio_id = %s",
        (b["unidad_negocio_id"],),
    ).fetchone()
    inicio = fila["inicio_operacion_at"] if fila else None
    return bool(inicio is not None and b["primera_pesada_at"] is not None and b["primera_pesada_at"] >= inicio)


def _cancelada(op: Operacion, b: dict[str, Any], r: dict[str, Any] | None) -> None:
    if r is not None:
        if r["cancelada_at"] is not None:
            return
        pb = cargar_preboleta(op.con, r["preboleta_id"], bloquear=True)
        op.con.execute(
            "UPDATE preboletas.recepciones SET cancelada_at = now(), cancelada_por_id = NULL, "
            "motivo_cancelacion = 'Boleta cancelada en D365' WHERE id = %s",
            (r["id"],),
        )
        cantidad.cancelar_incidencias(
            op, "recepcion_id = %s AND estado = 'pendiente'", (r["id"],), "Boleta cancelada en D365"
        )
        op.mov(
            "recepcion_cancelada_d365",
            b["unidad_negocio_id"],
            f"{b['folio']} cancelada en D365: la carga de {r['cajas_declaradas']} cajas deja de contar "
            f"en {folio(pb['id'])}",
            preboleta_id=pb["id"],
            recepcion_id=r["id"],
            boleta_id=b["id"],
            sistema=True,
        )
        # Su cierre se calculó con esta carga: se reabre y se vuelve a evaluar.
        cantidad.reabrir(op, pb, "una de sus boletas se canceló en D365", boleta_id=b["id"])
        cantidad.reevaluar(op, pb)
        cantidad.evaluar_cierre(op, pb)
        cantidad.evento_preboleta(op, pb)
        return
    pendientes = op.con.execute(
        "SELECT * FROM preboletas.incidencias WHERE boleta_id = %s AND tipo = ANY(%s) AND estado = 'pendiente' "
        "FOR UPDATE",
        (b["id"], list(TIPOS_CONCILIACION)),
    ).fetchall()
    for inc in pendientes:
        cantidad.cancelar_incidencia(op, dict(inc), "Boleta cancelada en D365")
        op.mov(
            "boleta_descartada",
            b["unidad_negocio_id"],
            f"{b['folio']} cancelada en D365: su incidencia se cancela",
            boleta_id=b["id"],
            incidencia_id=inc["id"],
            preboleta_id=inc["preboleta_id"],
            sistema=True,
        )


def _sin_vincular(op: Operacion, b: dict[str, Any]) -> dict[str, Any] | None:
    """Boleta con bruto y sin carga ligada. Devuelve la carga si se ligó en este paso."""
    concs = op.con.execute(
        "SELECT * FROM preboletas.incidencias WHERE boleta_id = %s AND tipo = ANY(%s) ORDER BY id",
        (b["id"], list(TIPOS_CONCILIACION)),
    ).fetchall()
    if concs:
        # Ya pasó por la decisión de la 1ra pesada. Solo se vuelve a ligar sola si D365
        # le regresó el proveedor original después de un cambio de proveedor.
        cp = next((i for i in concs if i["tipo"] == "cambio_proveedor" and i["estado"] == "pendiente"), None)
        if cp is None or cp["recepcion_id"] is None or b["proveedor_cuenta"] != cp["proveedor_esperado"]:
            return None
        rr = op.con.execute(
            "SELECT * FROM preboletas.recepciones WHERE id = %s FOR UPDATE", (cp["recepcion_id"],)
        ).fetchone()
        if rr is None or rr["boleta_id"] is not None or rr["cancelada_at"] is not None:
            return None
        recepcion = dict(rr)
        _ligar(op, recepcion, b, por=None)
        cantidad.cancelar_incidencia(op, dict(cp), "D365 regresó el proveedor original")
        pb = cargar_preboleta(op.con, recepcion["preboleta_id"])
        op.mov(
            "boleta_vinculada",
            b["unidad_negocio_id"],
            f"{b['folio']} vuelve a ligarse a {folio(pb['id'])}: D365 regresó el proveedor original",
            preboleta_id=pb["id"],
            recepcion_id=recepcion["id"],
            boleta_id=b["id"],
            datos={"proveedor_cuenta": b["proveedor_cuenta"]},
            sistema=True,
        )
        _evento_vinculada(op, b, pb)
        return recepcion

    # Decisión en la 1ra pesada.
    op.mov(
        "boleta_creada",
        b["unidad_negocio_id"],
        f"{b['folio']} llega con bruto {kilos(b['peso_bruto'])} ({b['proveedor_nombre'] or 'sin proveedor'})",
        boleta_id=b["id"],
        sistema=True,
    )
    slot = bloquear_slot(op.con, b["unidad_negocio_id"])
    if slot["recepcion_id"] is not None and _puede_ser_el_camion(slot, b):
        sr = op.con.execute(
            "SELECT * FROM preboletas.recepciones WHERE id = %s FOR UPDATE", (slot["recepcion_id"],)
        ).fetchone()
        assert sr is not None
        recepcion = dict(sr)
        pb = cargar_preboleta(op.con, recepcion["preboleta_id"], bloquear=True)
        # La 1ra pesada siempre libera la báscula, coincida o no el proveedor.
        liberar_slot(op, b["unidad_negocio_id"])
        if b["proveedor_cuenta"] == pb["proveedor_cuenta"]:
            _ligar(op, recepcion, b, por=None)
            op.con.execute(
                "UPDATE preboletas.incidencias SET boleta_id = %s WHERE recepcion_id = %s AND boleta_id IS NULL "
                "AND tipo IN ('sobre_entrega', 'faltante')",
                (b["id"], recepcion["id"]),
            )
            op.mov(
                "boleta_vinculada",
                b["unidad_negocio_id"],
                f"{b['folio']} ligada a {folio(pb['id'])}: proveedor y báscula coinciden. Báscula libre",
                preboleta_id=pb["id"],
                recepcion_id=recepcion["id"],
                boleta_id=b["id"],
                datos={"proveedor_cuenta": b["proveedor_cuenta"]},
                sistema=True,
            )
            _evento_vinculada(op, b, pb)
            return recepcion
        crear_conciliacion(op, "proveedor_distinto", b, pb, recepcion, esperado=pb["proveedor_cuenta"])
        cantidad.evento_preboleta(op, pb)
    else:
        crear_conciliacion(op, "sin_preboleta", b, None, None, esperado=None)
    return None


def _puede_ser_el_camion(slot: dict[str, Any], b: dict[str, Any]) -> bool:
    """
    El camión del slot solo puede ser el de esta boleta si entró ANTES de su 1ra pesada.
    Evita que una boleta atrasada (ej. procesada por el barrido tras una caída) se lleve
    al camión que entró después.
    """
    if slot["ocupado_at"] is None or b["primera_pesada_at"] is None:
        return True
    return bool(slot["ocupado_at"] <= b["primera_pesada_at"] + TOLERANCIA_RELOJ)


def _vinculada(op: Operacion, b: dict[str, Any], r: dict[str, Any]) -> None:
    if r["cancelada_at"] is not None:
        return
    pb = cargar_preboleta(op.con, r["preboleta_id"], bloquear=True)
    referencia = proveedor_referencia(op.con, r["id"], b["id"]) or pb["proveedor_cuenta"]
    if b["proveedor_cuenta"] != referencia:
        op.con.execute(
            "UPDATE preboletas.recepciones SET boleta_id = NULL, vinculada_at = NULL, vinculada_por_id = NULL "
            "WHERE id = %s",
            (r["id"],),
        )
        op.mov(
            "boleta_desvinculada",
            b["unidad_negocio_id"],
            f"{b['folio']} cambió de proveedor en D365 y se desliga de {folio(pb['id'])}",
            preboleta_id=pb["id"],
            recepcion_id=r["id"],
            boleta_id=b["id"],
            sistema=True,
        )
        op.evento(
            "boleta.desvinculada",
            boleta_id=b["id"],
            folio=b["folio"],
            preboleta_id=pb["id"],
            unidad_negocio_id=pb["unidad_negocio_id"],
            comprador_id=pb["comprador_id"],
        )
        crear_conciliacion(op, "cambio_proveedor", b, pb, r, esperado=referencia)
        return
    if b["estado"] != "completada":
        return
    if not _salida_registrada(op.con, r["id"], b["id"]):
        cajas_reales = round(b["peso_neto"] / pb["kg_por_caja"])
        op.mov(
            "salida_bascula",
            b["unidad_negocio_id"],
            f"{b['folio']}: 2da pesada, neto {kilos(b['peso_neto'])} (≈ {cajas_reales} cajas reales)",
            preboleta_id=pb["id"],
            recepcion_id=r["id"],
            boleta_id=b["id"],
            sistema=True,
        )
        op.evento(
            "boleta.completada",
            boleta_id=b["id"],
            folio=b["folio"],
            preboleta_id=pb["id"],
            unidad_negocio_id=pb["unidad_negocio_id"],
            comprador_id=pb["comprador_id"],
        )
    # Idempotente: si ya está cerrada no hace nada.
    cantidad.evaluar_cierre(op, pb)


# ----------------------------------------------------------------------
def _ligar(op: Operacion, r: dict[str, Any], b: dict[str, Any], *, por: int | None) -> None:
    """vinculada_por_id vacío = vinculación automática."""
    op.con.execute(
        "UPDATE preboletas.recepciones SET boleta_id = %s, vinculada_at = now(), vinculada_por_id = %s WHERE id = %s",
        (b["id"], por, r["id"]),
    )
    r["boleta_id"] = b["id"]


def proveedor_referencia(con: Conexion, recepcion_id: int, boleta_id: int) -> str | None:
    """
    Proveedor contra el que se compara una boleta ya ligada. Se guarda en los datos del movimiento
    de vinculación o conciliación (al conciliar puede ser distinto al de la preboleta, documentado).
    """
    fila = con.execute(
        "SELECT datos->>'proveedor_cuenta' AS cuenta FROM preboletas.movimientos "
        "WHERE recepcion_id = %s AND boleta_id = %s AND tipo IN ('boleta_vinculada', 'boleta_conciliada') "
        "ORDER BY id DESC LIMIT 1",
        (recepcion_id, boleta_id),
    ).fetchone()
    return str(fila["cuenta"]) if fila and fila["cuenta"] else None


def _salida_registrada(con: Conexion, recepcion_id: int, boleta_id: int) -> bool:
    fila = con.execute(
        "SELECT 1 FROM preboletas.movimientos WHERE tipo = 'salida_bascula' AND recepcion_id = %s "
        "AND boleta_id = %s LIMIT 1",
        (recepcion_id, boleta_id),
    ).fetchone()
    return fila is not None


def _evento_vinculada(op: Operacion, b: dict[str, Any], pb: dict[str, Any]) -> None:
    op.evento(
        "boleta.vinculada",
        boleta_id=b["id"],
        folio=b["folio"],
        preboleta_id=pb["id"],
        unidad_negocio_id=pb["unidad_negocio_id"],
        comprador_id=pb["comprador_id"],
    )
    cantidad.evento_preboleta(op, pb)


def crear_conciliacion(
    op: Operacion,
    tipo: str,
    b: dict[str, Any],
    pb: dict[str, Any] | None,
    r: dict[str, Any] | None,
    *,
    esperado: str | None,
) -> int:
    """Incidencia de conciliación: la boleta no se pudo ligar sola y alguien debe decidir."""
    # La base exige proveedor en ambos lados para proveedor_distinto y cambio_proveedor.
    proveedor_boleta = b["proveedor_cuenta"] or ("—" if tipo != "sin_preboleta" else None)
    fila = op.con.execute(
        """
        INSERT INTO preboletas.incidencias
            (tipo, unidad_negocio_id, preboleta_id, recepcion_id, boleta_id, proveedor_esperado, proveedor_boleta)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            tipo,
            b["unidad_negocio_id"],
            pb["id"] if pb else None,
            r["id"] if r else None,
            b["id"],
            esperado,
            proveedor_boleta,
        ),
    ).fetchone()
    assert fila is not None
    inc_id = int(fila["id"])
    con_pb = f" y {folio(pb['id'])} ({pb['proveedor_nombre']})" if pb else ""
    op.mov(
        f"incidencia_{tipo}",
        b["unidad_negocio_id"],
        f"{NOMBRE_INCIDENCIA[tipo]}: {b['folio']} ({b['proveedor_nombre'] or 'sin proveedor'}){con_pb}. A conciliación",
        preboleta_id=pb["id"] if pb else None,
        recepcion_id=r["id"] if r else None,
        boleta_id=b["id"],
        incidencia_id=inc_id,
        sistema=True,
    )
    op.evento(
        "incidencia.creada",
        incidencia_id=inc_id,
        tipo=tipo,
        unidad_negocio_id=b["unidad_negocio_id"],
        preboleta_id=pb["id"] if pb else None,
        comprador_id=pb["comprador_id"] if pb else None,
    )
    op.notificar(
        [*basculistas(op.con, b["unidad_negocio_id"]), pb["comprador_id"] if pb else None, *admins(op.con)],
        f"{NOMBRE_INCIDENCIA[tipo]} en {nombre_unidad(op.con, b['unidad_negocio_id'])}: la boleta {b['folio']} "
        f"({b['proveedor_nombre'] or 'sin proveedor'}) quedó por conciliar.",
        preboleta_id=pb["id"] if pb else None,
        incidencia_id=inc_id,
    )
    return inc_id
