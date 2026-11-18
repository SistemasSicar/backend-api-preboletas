"""
Cantidades de una preboleta: acumulado, incidencias de cantidad, cierre y reapertura.

El ACUMULADO es la suma de las cajas DECLARADAS (o corregidas) de las cargas no canceladas.
No usa el peso neto: el neto solo informa las "cajas reales" (v_recepciones.cajas_reales).

Reglas (del prototipo, con las correcciones acordadas en el plan):
  * Sobre-entrega: el acumulado rebasa el máximo. En una carga nueva se crea si no hay otra
    PENDIENTE (cada carga es un hecho nuevo); al reevaluar, si no hay ninguna VIGENTE.
  * Faltante: el acumulado queda abajo del mínimo en una preboleta de UNA carga (sin múltiples
    cargas), al entrar a báscula o al cerrar. Hay como máximo una vigente (índice único en la base).
  * Una incidencia autorizada o rechazada sigue contando: la preboleta cierra "con incidencias".
  * Nunca frenan la operación.
"""

from __future__ import annotations

from typing import Any

from app.bd import Conexion
from app.errores import AccesoDenegado, Conflicto, NoEncontrado, SolicitudInvalida
from app.servicios.comun import NOMBRE_ESTADO_PB, NOMBRE_INCIDENCIA, Operacion, admins, cargar_preboleta, folio
from app.usuario import Usuario


def acumulado(con: Conexion, preboleta_id: int) -> int:
    fila = con.execute(
        "SELECT COALESCE(sum(cajas_declaradas), 0) AS a FROM preboletas.recepciones "
        "WHERE preboleta_id = %s AND cancelada_at IS NULL",
        (preboleta_id,),
    ).fetchone()
    assert fila is not None
    return int(fila["a"])


def incidencia_vigente(con: Conexion, preboleta_id: int, tipo: str, *, solo_pendiente: bool = False) -> bool:
    condicion = "estado = 'pendiente'" if solo_pendiente else "estado <> 'cancelada'"
    fila = con.execute(
        f"SELECT 1 FROM preboletas.incidencias WHERE preboleta_id = %s AND tipo = %s AND {condicion} LIMIT 1",
        (preboleta_id, tipo),
    ).fetchone()
    return fila is not None


def crear_incidencia_cantidad(
    op: Operacion, pb: dict[str, Any], tipo: str, *, recepcion_id: int | None = None, boleta_id: int | None = None
) -> int:
    """Crea una sobre-entrega o un faltante con la foto del acumulado y avisa al comprador y a los Admin."""
    a = acumulado(op.con, pb["id"])
    limite = int(pb["cajas_maximas"] if tipo == "sobre_entrega" else pb["cajas_minimas"])
    fila = op.con.execute(
        """
        INSERT INTO preboletas.incidencias
            (tipo, unidad_negocio_id, preboleta_id, recepcion_id, boleta_id,
             cajas_acumuladas, cajas_limite, cajas_diferencia)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (tipo, pb["unidad_negocio_id"], pb["id"], recepcion_id, boleta_id, a, limite, abs(a - limite)),
    ).fetchone()
    assert fila is not None
    inc_id = int(fila["id"])
    palabra = "máximo" if tipo == "sobre_entrega" else "mínimo"
    texto = f"{NOMBRE_INCIDENCIA[tipo]} en {folio(pb['id'])}: {a} cajas, {palabra} {limite}"
    # La registra quien hizo la carga; si nace al cerrar o reevaluar sola, es del sistema.
    op.mov(
        f"incidencia_{tipo}",
        pb["unidad_negocio_id"],
        texto,
        preboleta_id=pb["id"],
        recepcion_id=recepcion_id,
        incidencia_id=inc_id,
        sistema=recepcion_id is None,
    )
    op.evento(
        "incidencia.creada",
        incidencia_id=inc_id,
        tipo=tipo,
        unidad_negocio_id=pb["unidad_negocio_id"],
        preboleta_id=pb["id"],
        comprador_id=pb["comprador_id"],
    )
    op.notificar(
        [pb["comprador_id"], *admins(op.con)],
        f"{NOMBRE_INCIDENCIA[tipo]} en {folio(pb['id'])} ({pb['proveedor_nombre']}): {a} cajas, {palabra} {limite}.",
        preboleta_id=pb["id"],
        incidencia_id=inc_id,
    )
    return inc_id


def cancelar_incidencia(op: Operacion, inc: dict[str, Any], motivo: str) -> None:
    """
    Una incidencia queda 'cancelada' cuando la situación se revierte. Si ya tenía comentario
    (ej. la observación de un cierre a mano) se conserva y se le agrega el motivo.
    """
    op.con.execute(
        """
        UPDATE preboletas.incidencias
           SET estado = 'cancelada', resuelto_at = now(),
               comentario = CASE WHEN COALESCE(btrim(comentario), '') = '' THEN %s
                                 ELSE comentario || ' | Cancelada: ' || %s END
         WHERE id = %s
        """,
        (motivo, motivo, inc["id"]),
    )
    op.evento(
        "incidencia.resuelta",
        incidencia_id=inc["id"],
        tipo=inc["tipo"],
        estado="cancelada",
        unidad_negocio_id=inc["unidad_negocio_id"],
        preboleta_id=inc.get("preboleta_id"),
    )


def cancelar_incidencias(op: Operacion, where: str, params: tuple[Any, ...], motivo: str) -> None:
    filas = op.con.execute(f"SELECT * FROM preboletas.incidencias WHERE {where} FOR UPDATE", params).fetchall()
    for inc in filas:
        cancelar_incidencia(op, dict(inc), motivo)


def resultado_cierre(con: Conexion, preboleta_id: int) -> str:
    """terminado_con_incidencias si hay incidencias vigentes (no canceladas) de cantidad o de cierre a mano."""
    fila = con.execute(
        "SELECT 1 FROM preboletas.incidencias WHERE preboleta_id = %s "
        "AND tipo IN ('sobre_entrega', 'faltante', 'cierre_manual') AND estado <> 'cancelada' LIMIT 1",
        (preboleta_id,),
    ).fetchone()
    return "terminado_con_incidencias" if fila else "terminado"


def reevaluar(op: Operacion, pb: dict[str, Any]) -> None:
    """
    Después de cualquier cambio en las cargas: cancela las incidencias de cantidad que ya no
    aplican, crea la sobre-entrega si hace falta y, si la preboleta está cerrada, recalcula su resultado.
    """
    a = acumulado(op.con, pb["id"])
    pendientes = op.con.execute(
        "SELECT * FROM preboletas.incidencias WHERE preboleta_id = %s AND estado = 'pendiente' "
        "AND tipo IN ('sobre_entrega', 'faltante') FOR UPDATE",
        (pb["id"],),
    ).fetchall()
    for inc in pendientes:
        if inc["tipo"] == "sobre_entrega" and a <= pb["cajas_maximas"]:
            cancelar_incidencia(op, dict(inc), "El acumulado ya no rebasa el máximo")
        elif inc["tipo"] == "faltante" and (a >= pb["cajas_minimas"] or pb["multiples_cargas"]):
            cancelar_incidencia(op, dict(inc), "El acumulado ya alcanza el mínimo o admite más cargas")
    if a > pb["cajas_maximas"] and not incidencia_vigente(op.con, pb["id"], "sobre_entrega"):
        ultima = op.con.execute(
            "SELECT id, boleta_id FROM preboletas.recepciones WHERE preboleta_id = %s AND cancelada_at IS NULL "
            "ORDER BY ingreso_at DESC, id DESC LIMIT 1",
            (pb["id"],),
        ).fetchone()
        crear_incidencia_cantidad(op, pb, "sobre_entrega", boleta_id=ultima["boleta_id"] if ultima else None)
    if pb["cerrada_at"] is not None:
        nuevo = resultado_cierre(op.con, pb["id"])
        if nuevo != pb["resultado_cierre"]:
            op.con.execute("UPDATE preboletas.preboletas SET resultado_cierre = %s WHERE id = %s", (nuevo, pb["id"]))
            pb["resultado_cierre"] = nuevo
            op.mov(
                "preboleta_cerrada",
                pb["unidad_negocio_id"],
                f"{folio(pb['id'])}: resultado recalculado a {NOMBRE_ESTADO_PB[nuevo]}",
                preboleta_id=pb["id"],
                sistema=True,
            )
            _evento_pb(op, pb)


def cerrar(op: Operacion, pb: dict[str, Any], resultado: str, *, manual: bool) -> None:
    fila = op.con.execute(
        "UPDATE preboletas.preboletas SET cerrada_at = now(), cerrada_por_id = %s, resultado_cierre = %s "
        "WHERE id = %s RETURNING cerrada_at",
        (op.quien if manual else None, resultado, pb["id"]),
    ).fetchone()
    assert fila is not None
    pb.update(cerrada_at=fila["cerrada_at"], resultado_cierre=resultado)
    a = acumulado(op.con, pb["id"])
    modo = "a mano" if manual else "automáticamente"
    op.mov(
        "preboleta_cerrada",
        pb["unidad_negocio_id"],
        f"{folio(pb['id'])} cerrada {modo}: {NOMBRE_ESTADO_PB[resultado]} ({a} cajas)",
        preboleta_id=pb["id"],
        sistema=not manual,
    )
    _evento_pb(op, pb, estado=resultado)
    if resultado == "terminado_con_incidencias":
        op.notificar(
            [pb["comprador_id"], *admins(op.con)], f"{folio(pb['id'])} se cerró con incidencias.", preboleta_id=pb["id"]
        )


def reabrir(op: Operacion, pb: dict[str, Any], razon: str, **refs: Any) -> None:
    """
    Reabre una preboleta cerrada (llegó otra carga, se concilió una boleta o D365 canceló una).
    Siempre cancela el faltante y el cierre a mano vigentes: se calcularon con un cierre que ya no aplica.
    """
    if pb["cerrada_at"] is None:
        return
    op.con.execute(
        "UPDATE preboletas.preboletas SET cerrada_at = NULL, cerrada_por_id = NULL, resultado_cierre = NULL "
        "WHERE id = %s",
        (pb["id"],),
    )
    pb.update(cerrada_at=None, cerrada_por_id=None, resultado_cierre=None)
    cancelar_incidencias(
        op,
        "preboleta_id = %s AND tipo IN ('faltante', 'cierre_manual') AND estado <> 'cancelada'",
        (pb["id"],),
        "Preboleta reabierta",
    )
    op.mov(
        "preboleta_reabierta",
        pb["unidad_negocio_id"],
        f"{folio(pb['id'])} reabierta: {razon}",
        preboleta_id=pb["id"],
        sistema=op.usuario is None,
        **refs,
    )


def evaluar_cierre(op: Operacion, pb: dict[str, Any]) -> None:
    """
    Cierre AUTOMÁTICO: todas las cargas vivas tienen boleta completada y ninguna está en báscula.
      * Una carga (sin múltiples): cierra; si quedó abajo del mínimo, con faltante.
      * Múltiples cargas: espera hasta llegar a las cajas negociadas (no al mínimo).
    """
    if pb["cerrada_at"] is not None:
        return
    fila = op.con.execute(
        """
        SELECT count(*)                                                    AS cargas,
               count(*) FILTER (WHERE s.recepcion_id IS NOT NULL)          AS en_slot,
               count(*) FILTER (WHERE r.boleta_id IS NULL)                 AS sin_boleta,
               count(*) FILTER (WHERE b.estado IS DISTINCT FROM 'completada') AS sin_completar
          FROM preboletas.recepciones r
          LEFT JOIN preboletas.slots_bascula s ON s.recepcion_id = r.id
          LEFT JOIN core.boletas b ON b.id = r.boleta_id
         WHERE r.preboleta_id = %s AND r.cancelada_at IS NULL
        """,
        (pb["id"],),
    ).fetchone()
    assert fila is not None
    if fila["cargas"] == 0 or fila["en_slot"] or fila["sin_boleta"] or fila["sin_completar"]:
        return
    a = acumulado(op.con, pb["id"])
    if not pb["multiples_cargas"]:
        if a < pb["cajas_minimas"]:
            if not incidencia_vigente(op.con, pb["id"], "faltante"):
                crear_incidencia_cantidad(op, pb, "faltante")
            cerrar(op, pb, "terminado_con_incidencias", manual=False)
            return
    elif a < pb["cajas_negociadas"]:
        return
    cerrar(op, pb, resultado_cierre(op.con, pb["id"]), manual=False)


def cierre_manual(op: Operacion, usuario: Usuario, preboleta_id: int, observacion: str) -> None:
    """
    El comprador (o un Admin) cierra aunque no se complete lo negociado. Siempre queda una
    incidencia informativa 'cierre_manual' (nace 'registrada') y, abajo del mínimo, un faltante.
    """
    pb = cargar_preboleta(op.con, preboleta_id, bloquear=True)
    if not usuario.es_dueno(pb["comprador_id"]):
        raise AccesoDenegado("Solo su comprador o un Admin cierran a mano.")
    if pb["cerrada_at"] is not None:
        raise Conflicto("La preboleta ya está cerrada.", codigo="preboleta_cerrada")
    bloqueo = op.con.execute(
        """
        SELECT bool_or(s.recepcion_id IS NOT NULL)                                       AS en_bascula,
               bool_or(r.boleta_id IS NOT NULL AND b.estado IS DISTINCT FROM 'completada') AS sin_segunda
          FROM preboletas.recepciones r
          LEFT JOIN preboletas.slots_bascula s ON s.recepcion_id = r.id
          LEFT JOIN core.boletas b ON b.id = r.boleta_id
         WHERE r.preboleta_id = %s AND r.cancelada_at IS NULL
        """,
        (pb["id"],),
    ).fetchone()
    assert bloqueo is not None
    if bloqueo["en_bascula"]:
        raise Conflicto("Hay un camión de esta preboleta en la báscula.", codigo="cierre_bloqueado")
    if bloqueo["sin_segunda"]:
        raise Conflicto("Hay boletas sin 2da pesada.", codigo="cierre_bloqueado")
    obs = observacion.strip()
    if not obs:
        raise SolicitudInvalida("La observación es obligatoria para cerrar a mano.")

    a = acumulado(op.con, pb["id"])
    if a < pb["cajas_minimas"] and not incidencia_vigente(op.con, pb["id"], "faltante"):
        crear_incidencia_cantidad(op, pb, "faltante")
    fila = op.con.execute(
        """
        INSERT INTO preboletas.incidencias
            (tipo, unidad_negocio_id, preboleta_id, estado, resuelto_por_id, resuelto_at, comentario)
        VALUES ('cierre_manual', %s, %s, 'registrada', %s, now(), %s)
        RETURNING id
        """,
        (pb["unidad_negocio_id"], pb["id"], usuario.colaborador_id, obs),
    ).fetchone()
    assert fila is not None
    inc_id = int(fila["id"])
    f = folio(pb["id"])
    op.mov(
        "incidencia_cierre_manual",
        pb["unidad_negocio_id"],
        f"{f} cerrada a mano con {a} de {pb['cajas_negociadas']} cajas "
        f"({pb['cajas_minimas']}–{pb['cajas_maximas']}). Observación: {obs}",
        preboleta_id=pb["id"],
        incidencia_id=inc_id,
    )
    op.evento(
        "incidencia.creada",
        incidencia_id=inc_id,
        tipo="cierre_manual",
        unidad_negocio_id=pb["unidad_negocio_id"],
        preboleta_id=pb["id"],
        comprador_id=pb["comprador_id"],
    )
    op.notificar(
        [pb["comprador_id"], *admins(op.con)],
        f"{usuario.nombre} cerró a mano {f} ({pb['proveedor_nombre']}) con {a} de {pb['cajas_negociadas']} cajas. "
        f"Observación: {obs}",
        preboleta_id=pb["id"],
        incidencia_id=inc_id,
        excluir_a_quien_actua=True,
    )
    cerrar(op, pb, "terminado_con_incidencias", manual=True)


def resolver_incidencia_cantidad(
    op: Operacion, usuario: Usuario, incidencia_id: int, decision: str, comentario: str | None
) -> None:
    """La autoriza o rechaza cualquier comprador (de cualquier unidad) o un Admin. No cambia el acumulado."""
    if not (usuario.es_comprador or usuario.es_admin):
        raise AccesoDenegado("Autoriza cualquier comprador o un Admin.")
    inc = op.con.execute("SELECT * FROM preboletas.incidencias WHERE id = %s FOR UPDATE", (incidencia_id,)).fetchone()
    if inc is None or inc["tipo"] not in ("sobre_entrega", "faltante"):
        raise NoEncontrado("La incidencia no existe.")
    if inc["estado"] != "pendiente":
        raise Conflicto("Esa incidencia ya está resuelta.", codigo="incidencia_resuelta")
    comentario = (comentario or "").strip() or None
    op.con.execute(
        "UPDATE preboletas.incidencias SET estado = %s, resuelto_por_id = %s, resuelto_at = now(), comentario = %s "
        "WHERE id = %s",
        (decision, usuario.colaborador_id, comentario, incidencia_id),
    )
    pb = cargar_preboleta(op.con, inc["preboleta_id"])
    f = folio(pb["id"])
    nombre = NOMBRE_INCIDENCIA[inc["tipo"]]
    op.mov(
        "incidencia_autorizada" if decision == "autorizada" else "incidencia_rechazada",
        inc["unidad_negocio_id"],
        f"{nombre} de {f} {decision} por {usuario.nombre}{f': {comentario}' if comentario else ''}",
        preboleta_id=pb["id"],
        incidencia_id=incidencia_id,
    )
    op.evento(
        "incidencia.resuelta",
        incidencia_id=incidencia_id,
        tipo=inc["tipo"],
        estado=decision,
        unidad_negocio_id=inc["unidad_negocio_id"],
        preboleta_id=pb["id"],
        comprador_id=pb["comprador_id"],
    )
    if pb["comprador_id"] != usuario.colaborador_id:
        verbo = "autorizó" if decision == "autorizada" else "rechazó"
        op.notificar(
            [pb["comprador_id"]],
            f"{usuario.nombre} {verbo} la {nombre.lower()} de {f}.",
            preboleta_id=pb["id"],
            incidencia_id=incidencia_id,
        )


def _evento_pb(op: Operacion, pb: dict[str, Any], **extra: Any) -> None:
    op.evento(
        "preboleta.actualizada",
        preboleta_id=pb["id"],
        unidad_negocio_id=pb["unidad_negocio_id"],
        comprador_id=pb["comprador_id"],
        **extra,
    )


evento_preboleta = _evento_pb
