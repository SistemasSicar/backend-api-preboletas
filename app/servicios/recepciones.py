"""
Báscula: entrada de camiones (recepciones) y el slot de cada unidad.

Cada unidad tiene UNA báscula y solo un camión a la vez (preboletas.slots_bascula). El basculista
registra la entrada con las cajas que declara el chofer; el camión ocupa el slot hasta su 1ra pesada
en D365, cuando el motor liga la boleta y libera la báscula.
"""

from __future__ import annotations

from typing import Any

import psycopg

from app.bd import Conexion
from app.errores import AccesoDenegado, Conflicto, NoEncontrado, SolicitudInvalida
from app.servicios import cantidad
from app.servicios.comun import D365_CERRADAS, Operacion, cargar_preboleta, folio, nombre_unidad
from app.usuario import Usuario


def bloquear_slot(con: Conexion, unidad_negocio_id: int) -> dict[str, Any]:
    """Toma el slot de la unidad con FOR UPDATE (lo crea LIBRE si aún no existe)."""
    con.execute(
        "INSERT INTO preboletas.slots_bascula (unidad_negocio_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (unidad_negocio_id,),
    )
    fila = con.execute(
        "SELECT * FROM preboletas.slots_bascula WHERE unidad_negocio_id = %s FOR UPDATE", (unidad_negocio_id,)
    ).fetchone()
    assert fila is not None
    return dict(fila)


def liberar_slot(op: Operacion, unidad_negocio_id: int) -> None:
    op.con.execute(
        "UPDATE preboletas.slots_bascula SET recepcion_id = NULL, ocupado_at = NULL WHERE unidad_negocio_id = %s",
        (unidad_negocio_id,),
    )
    op.evento("slot.actualizado", unidad_negocio_id=unidad_negocio_id, ocupado=False)


def registrar_entrada(
    op: Operacion, usuario: Usuario, preboleta_id: int, cajas: int, via: str, multiples_cargas: bool
) -> int:
    """Indicar peso en báscula: crea la recepción, ocupa el slot y revisa cantidades."""
    u = int(cargar_preboleta(op.con, preboleta_id)["unidad_negocio_id"])
    if not usuario.puede_operar_bascula(u):
        raise AccesoDenegado(f"Solo el basculista de {nombre_unidad(op.con, u)} o un Admin.", codigo="fuera_de_unidad")
    if cajas <= 0:
        raise SolicitudInvalida("Captura las cajas como número entero mayor a 0.")
    # Orden de candados en todo el sistema: báscula → carga → preboleta (evita bloqueos mutuos con el motor).
    slot = bloquear_slot(op.con, u)
    pb = cargar_preboleta(op.con, preboleta_id, bloquear=True)
    if slot["recepcion_id"] is not None:
        raise Conflicto(f"La báscula de {nombre_unidad(op.con, u)} está ocupada.", codigo="bascula_ocupada")

    previo = cantidad.acumulado(op.con, pb["id"])
    fila = op.con.execute(
        """
        INSERT INTO preboletas.recepciones (preboleta_id, unidad_negocio_id, cajas_declaradas, via, basculista_id)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, ingreso_at
        """,
        (pb["id"], u, cajas, via, usuario.colaborador_id),
    ).fetchone()
    assert fila is not None
    rec_id = int(fila["id"])
    try:
        op.con.execute(
            "UPDATE preboletas.slots_bascula SET recepcion_id = %s, ocupado_at = %s WHERE unidad_negocio_id = %s",
            (rec_id, fila["ingreso_at"], u),
        )
    except psycopg.errors.RaiseException as exc:
        # Defensa adicional: el trigger fn_slot_un_camion impide pisar un camión (HINT bascula_ocupada).
        if exc.diag.message_hint == "bascula_ocupada":
            raise Conflicto(
                f"La báscula de {nombre_unidad(op.con, u)} está ocupada.", codigo="bascula_ocupada"
            ) from exc
        raise

    cantidad.reabrir(op, pb, "llegó una carga nueva", recepcion_id=rec_id)
    if multiples_cargas and not pb["multiples_cargas"]:
        op.con.execute("UPDATE preboletas.preboletas SET multiples_cargas = true WHERE id = %s", (pb["id"],))
        pb["multiples_cargas"] = True
        op.mov(
            "multiples_cargas",
            u,
            f"{folio(pb['id'])}: múltiples cargas activadas por el basculista",
            preboleta_id=pb["id"],
        )
    descripcion = "QR leído" if via == "qr" else "sin QR: elegida desde la preboleta"
    op.mov(
        "entrada_bascula" if via == "qr" else "carga_manual",
        u,
        f"{folio(pb['id'])}: entra con {cajas} cajas ({descripcion}). Acumulado {previo + cajas}",
        preboleta_id=pb["id"],
        recepcion_id=rec_id,
    )

    total = previo + cajas
    if total > pb["cajas_maximas"]:
        # Cada carga que rebasa es un hecho nuevo, salvo que ya haya una sobre-entrega por decidir.
        if not cantidad.incidencia_vigente(op.con, pb["id"], "sobre_entrega", solo_pendiente=True):
            cantidad.crear_incidencia_cantidad(op, pb, "sobre_entrega", recepcion_id=rec_id)
    elif not pb["multiples_cargas"] and total < pb["cajas_minimas"]:
        # Preboleta de una sola carga que ya viene corta: se avisa mientras el camión sigue ahí.
        if not cantidad.incidencia_vigente(op.con, pb["id"], "faltante"):
            cantidad.crear_incidencia_cantidad(op, pb, "faltante", recepcion_id=rec_id)
    else:
        cantidad.reevaluar(op, pb)

    op.evento("slot.actualizado", unidad_negocio_id=u, ocupado=True, preboleta_id=pb["id"])
    cantidad.evento_preboleta(op, pb, cajas_acumuladas=total)
    return rec_id


def _cargar_recepcion(con: Conexion, recepcion_id: int) -> dict[str, Any]:
    fila = con.execute("SELECT * FROM preboletas.recepciones WHERE id = %s FOR UPDATE", (recepcion_id,)).fetchone()
    if fila is None:
        raise NoEncontrado("La carga no existe.")
    return dict(fila)


def cancelar_entrada(op: Operacion, usuario: Usuario, recepcion_id: int, motivo: str) -> None:
    """Solo antes de la 1ra pesada (camión en báscula). Después, se cancela la boleta en D365."""
    fila = op.con.execute(
        "SELECT unidad_negocio_id FROM preboletas.recepciones WHERE id = %s", (recepcion_id,)
    ).fetchone()
    if fila is None:
        raise NoEncontrado("La carga no existe.")
    if not usuario.puede_operar_bascula(fila["unidad_negocio_id"]):
        raise AccesoDenegado("Solo el basculista de la unidad o un Admin.", codigo="fuera_de_unidad")
    slot = bloquear_slot(op.con, fila["unidad_negocio_id"])
    r = _cargar_recepcion(op.con, recepcion_id)
    if slot["recepcion_id"] != r["id"]:
        raise Conflicto(
            "Solo se cancela antes de la 1ra pesada (camión en báscula). Después, se cancela la boleta en D365.",
            codigo="fuera_de_bascula",
        )
    motivo = motivo.strip()
    if not motivo:
        raise SolicitudInvalida("El motivo es obligatorio.")
    pb = cargar_preboleta(op.con, r["preboleta_id"], bloquear=True)
    # Primero se libera el slot: el trigger no deja que una recepción cancelada lo ocupe.
    liberar_slot(op, r["unidad_negocio_id"])
    op.con.execute(
        "UPDATE preboletas.recepciones SET cancelada_at = now(), cancelada_por_id = %s, motivo_cancelacion = %s "
        "WHERE id = %s",
        (usuario.colaborador_id, motivo, r["id"]),
    )
    cantidad.cancelar_incidencias(op, "recepcion_id = %s AND estado = 'pendiente'", (r["id"],), "Entrada cancelada")
    op.con.execute(
        "UPDATE preboletas.propuestas_precio SET estado = 'cancelada', resuelto_at = now() "
        "WHERE recepcion_id = %s AND estado = 'pendiente'",
        (r["id"],),
    )
    op.mov(
        "entrada_cancelada",
        r["unidad_negocio_id"],
        f"{folio(pb['id'])}: entrada de {r['cajas_declaradas']} cajas cancelada. {motivo}",
        preboleta_id=pb["id"],
        recepcion_id=r["id"],
    )
    cantidad.reevaluar(op, pb)
    cantidad.evento_preboleta(op, pb)


def corregir_cajas(op: Operacion, usuario: Usuario, recepcion_id: int, cajas: int, motivo: str) -> None:
    """
    Corrige las cajas declaradas mientras la boleta no esté cerrada en D365 (Validated, Completed
    o Confirmed). Guarda las cajas originales solo la primera vez.
    """
    r = _cargar_recepcion(op.con, recepcion_id)
    if not usuario.puede_operar_bascula(r["unidad_negocio_id"]):
        raise AccesoDenegado("Solo el basculista de la unidad o un Admin.", codigo="fuera_de_unidad")
    if r["cancelada_at"] is not None:
        raise SolicitudInvalida("La carga está cancelada.")
    if r["boleta_id"] is not None:
        b = op.con.execute(
            "SELECT folio, estado_d365, estado_d365_codigo FROM core.boletas WHERE id = %s", (r["boleta_id"],)
        ).fetchone()
        if b is not None and b["estado_d365_codigo"] in D365_CERRADAS:
            raise Conflicto(
                f"La boleta {b['folio']} ya está cerrada en D365 ({b['estado_d365'] or b['estado_d365_codigo']}): "
                "ya no se corrigen cajas.",
                codigo="boleta_cerrada",
            )
    if cajas <= 0 or cajas == r["cajas_declaradas"]:
        raise SolicitudInvalida("Captura un número de cajas distinto al actual.")
    motivo = motivo.strip()
    if not motivo:
        raise SolicitudInvalida("El motivo es obligatorio.")
    antes = int(r["cajas_declaradas"])
    op.con.execute(
        """
        UPDATE preboletas.recepciones
           SET cajas_declaradas = %s, cajas_originales = COALESCE(cajas_originales, %s),
               cajas_corregidas_at = now(), cajas_corregidas_por_id = %s
         WHERE id = %s
        """,
        (cajas, antes, usuario.colaborador_id, r["id"]),
    )
    pb = cargar_preboleta(op.con, r["preboleta_id"], bloquear=True)
    op.mov(
        "cajas_corregidas",
        r["unidad_negocio_id"],
        f"{folio(pb['id'])}: cajas {antes} → {cajas}. {motivo}",
        preboleta_id=pb["id"],
        recepcion_id=r["id"],
        datos={"antes": antes, "despues": cajas},
    )
    cantidad.reevaluar(op, pb)
    cantidad.evento_preboleta(op, pb, cajas_acumuladas=cantidad.acumulado(op.con, pb["id"]))


def boleta_de_la_carga(con: Conexion, usuario: Usuario, recepcion_id: int) -> dict[str, Any]:
    """
    Id y folio de la boleta ligada a una carga, para "Actualizar boleta". Lo puede pedir quien ve la
    preboleta (Admin, su comprador o el basculista de la unidad); si no, 404 como si no existiera.
    """
    fila = con.execute(
        """
        SELECT r.id, r.preboleta_id, r.boleta_id, b.folio, p.comprador_id, p.unidad_negocio_id
          FROM preboletas.recepciones r
          JOIN preboletas.preboletas p ON p.id = r.preboleta_id
          LEFT JOIN core.boletas b ON b.id = r.boleta_id
         WHERE r.id = %s
        """,
        (recepcion_id,),
    ).fetchone()
    if fila is None or not usuario.puede_ver_preboleta(fila["comprador_id"], fila["unidad_negocio_id"]):
        raise NoEncontrado("La carga no existe.")
    if fila["boleta_id"] is None or fila["folio"] is None:
        raise SolicitudInvalida(
            "La carga aún no tiene boleta ligada: se liga sola en la 1ra pesada.", codigo="carga_sin_boleta"
        )
    return dict(fila)


def basculas(con: Conexion, usuario: Usuario) -> list[dict[str, Any]]:
    """
    Estado de la báscula de cada unidad. El detalle del camión (preboleta, proveedor, cajas) solo
    lo ve quien tiene que ver con él: Admin, basculista de la unidad, el comprador dueño, o
    cualquiera si hay una incidencia en esa carga.
    """
    filas = con.execute(
        """
        SELECT u.id AS unidad_negocio_id, u.nombre AS unidad,
               CASE WHEN s.recepcion_id IS NULL THEN 'LIBRE' ELSE 'OCUPADO' END AS estatus,
               s.ocupado_at, s.recepcion_id, r.preboleta_id, p.proveedor_cuenta, p.proveedor_nombre,
               p.comprador_id, r.cajas_declaradas, uc.inicio_operacion_at,
               i.id AS incidencia_id, i.tipo AS tipo_incidencia, i.estado AS estado_incidencia
          FROM core.unidades_negocio u
          LEFT JOIN preboletas.slots_bascula s ON s.unidad_negocio_id = u.id
          LEFT JOIN preboletas.recepciones r ON r.id = s.recepcion_id
          LEFT JOIN preboletas.preboletas p ON p.id = r.preboleta_id
          LEFT JOIN preboletas.unidades_config uc ON uc.unidad_negocio_id = u.id
          LEFT JOIN LATERAL (
                SELECT id, tipo, estado FROM preboletas.incidencias
                 WHERE recepcion_id = r.id AND estado <> 'cancelada'
                 ORDER BY creado_at DESC LIMIT 1) i ON true
         ORDER BY u.nombre
        """
    ).fetchall()
    resultado: list[dict[str, Any]] = []
    for f in filas:
        d = dict(f)
        ocupado = d["recepcion_id"] is not None
        visible = ocupado and (
            usuario.es_admin
            or (usuario.es_basculista and usuario.unidad_negocio_id == d["unidad_negocio_id"])
            or d["comprador_id"] == usuario.colaborador_id
            or d["incidencia_id"] is not None
        )
        d["detalle_visible"] = visible
        d["folio"] = folio(d["preboleta_id"]) if visible and d["preboleta_id"] else None
        if not visible:
            for campo in ("recepcion_id", "preboleta_id", "proveedor_cuenta", "proveedor_nombre", "cajas_declaradas"):
                d[campo] = None
        d.pop("comprador_id")
        d["opera"] = d["inicio_operacion_at"] is not None
        resultado.append(d)
    return resultado
