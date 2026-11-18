"""
Conciliación: boletas de D365 que el motor no pudo ligar solas (proveedor distinto, báscula libre
o cambio de proveedor). Alguien las liga a mano a una carga existente o a una carga nueva, o las
marca como no relacionadas (descartar, con comentario obligatorio).
"""

from __future__ import annotations

from typing import Any

from app.bd import Conexion
from app.errores import AccesoDenegado, Conflicto, NoEncontrado, SolicitudInvalida
from app.motor import motor_boletas
from app.servicios import cantidad
from app.servicios.comun import TIPOS_CONCILIACION, Operacion, cargar_preboleta, folio
from app.servicios.recepciones import bloquear_slot, liberar_slot
from app.usuario import Usuario


def _filtro_visibilidad(usuario: Usuario) -> tuple[str, tuple[Any, ...]]:
    """Admin y compradores: todas las unidades. Basculista: solo la suya."""
    if usuario.es_admin or usuario.es_comprador:
        return "true", ()
    return "i.unidad_negocio_id = %s", (usuario.unidad_negocio_id,)


def listar(con: Conexion, usuario: Usuario) -> dict[str, list[dict[str, Any]]]:
    filtro, params = _filtro_visibilidad(usuario)
    pendientes = con.execute(
        f"""
        SELECT v.*, u.nombre AS unidad
          FROM preboletas.v_conciliacion v
          JOIN preboletas.incidencias i ON i.id = v.incidencia_id
          JOIN core.unidades_negocio u ON u.id = v.unidad_negocio_id
         WHERE {filtro}
         ORDER BY v.creado_at
        """,
        params,
    ).fetchall()
    resueltas = con.execute(
        f"""
        SELECT i.id AS incidencia_id, i.tipo, i.estado, i.unidad_negocio_id, u.nombre AS unidad, i.creado_at,
               i.resuelto_at, i.comentario, i.proveedor_esperado, i.proveedor_boleta,
               b.id AS boleta_id, b.folio, b.proveedor_nombre, b.peso_bruto, b.peso_neto,
               COALESCE(c.nombre, c.correo) AS resuelto_por, rc.preboleta_id AS conciliada_preboleta_id
          FROM preboletas.incidencias i
          JOIN core.boletas b ON b.id = i.boleta_id
          JOIN core.unidades_negocio u ON u.id = i.unidad_negocio_id
          LEFT JOIN core.colaboradores c ON c.id = i.resuelto_por_id
          LEFT JOIN preboletas.recepciones rc ON rc.id = i.conciliada_con_recepcion_id
         WHERE i.tipo = ANY(%s) AND i.estado <> 'pendiente' AND {filtro}
         ORDER BY i.resuelto_at DESC NULLS LAST, i.id DESC
         LIMIT 50
        """,
        (list(TIPOS_CONCILIACION), *params),
    ).fetchall()
    lista_p = []
    for f in pendientes:
        d = dict(f)
        d["preboleta_sugerida_folio"] = folio(d["preboleta_sugerida_id"]) if d["preboleta_sugerida_id"] else None
        d["puede_resolver"] = usuario.puede_conciliar(d["tipo"], d["unidad_negocio_id"])
        lista_p.append(d)
    lista_r = []
    for f in resueltas:
        d = dict(f)
        pid = d["conciliada_preboleta_id"]
        d["conciliada_preboleta_folio"] = folio(pid) if pid else None
        lista_r.append(d)
    return {"pendientes": lista_p, "resueltas": lista_r}


def _cargar_incidencia(con: Conexion, usuario: Usuario, incidencia_id: int, *, bloquear: bool) -> dict[str, Any]:
    fila = con.execute(
        "SELECT * FROM preboletas.incidencias WHERE id = %s AND tipo = ANY(%s)" + (" FOR UPDATE" if bloquear else ""),
        (incidencia_id, list(TIPOS_CONCILIACION)),
    ).fetchone()
    visible = fila is not None and (
        usuario.es_admin or usuario.es_comprador or fila["unidad_negocio_id"] == usuario.unidad_negocio_id
    )
    if fila is None or not visible:
        raise NoEncontrado("La conciliación no existe.")
    return dict(fila)


def detalle(con: Conexion, usuario: Usuario, incidencia_id: int) -> dict[str, Any]:
    """Incidencia + boleta + candidatos para ligarla (preboletas de la unidad y cargas sin boleta)."""
    inc = _cargar_incidencia(con, usuario, incidencia_id, bloquear=False)
    b = con.execute(
        "SELECT b.*, u.nombre AS unidad FROM core.boletas b JOIN core.unidades_negocio u ON u.id = b.unidad_negocio_id "
        "WHERE b.id = %s",
        (inc["boleta_id"],),
    ).fetchone()
    assert b is not None
    boleta = {
        k: b[k]
        for k in (
            "id", "folio", "unidad_negocio_id", "unidad", "proveedor_cuenta", "proveedor_nombre", "placas", "chofer",
            "peso_bruto", "tara", "peso_neto", "estado", "estado_d365", "estado_d365_codigo", "primera_pesada_at",
            "segunda_pesada_at",
        )
    }  # fmt: skip
    # Mismo proveedor primero, abiertas antes que cerradas, más recientes primero.
    preboletas = con.execute(
        """
        SELECT id, proveedor_cuenta, proveedor_nombre, producto_nombre, fecha_llegada, estado,
               cajas_acumuladas, cajas_negociadas, cajas_minimas, cajas_maximas, cerrada_at IS NOT NULL AS cerrada,
               proveedor_cuenta IS NOT DISTINCT FROM %s AS mismo_proveedor
          FROM preboletas.v_preboletas
         WHERE unidad_negocio_id = %s
         ORDER BY mismo_proveedor DESC, (cerrada_at IS NULL) DESC, fecha_llegada DESC, id DESC
         LIMIT 50
        """,
        (b["proveedor_cuenta"], b["unidad_negocio_id"]),
    ).fetchall()
    cargas = con.execute(
        """
        SELECT r.id, r.preboleta_id, r.cajas_declaradas, r.ingreso_at, r.via,
               EXISTS (SELECT 1 FROM preboletas.slots_bascula s WHERE s.recepcion_id = r.id) AS en_bascula
          FROM preboletas.recepciones r
         WHERE r.unidad_negocio_id = %s AND r.boleta_id IS NULL AND r.cancelada_at IS NULL
         ORDER BY r.ingreso_at DESC
        """,
        (b["unidad_negocio_id"],),
    ).fetchall()
    sugerida = inc["preboleta_id"]
    return {
        "incidencia": {
            **{
                k: inc[k]
                for k in ("id", "tipo", "estado", "unidad_negocio_id", "creado_at", "resuelto_at", "comentario")
            },
            "proveedor_esperado": inc["proveedor_esperado"],
            "proveedor_boleta": inc["proveedor_boleta"],
            "preboleta_sugerida_id": sugerida,
            "preboleta_sugerida_folio": folio(sugerida) if sugerida else None,
            "recepcion_sugerida_id": inc["recepcion_id"],
        },
        "boleta": boleta,
        "puede_resolver": inc["estado"] == "pendiente"
        and usuario.puede_conciliar(inc["tipo"], inc["unidad_negocio_id"]),
        "preboletas": [{**dict(p), "folio": folio(p["id"])} for p in preboletas],
        "cargas_sin_boleta": [dict(c) for c in cargas],
    }


def _validar_resolucion(op: Operacion, usuario: Usuario, inc: dict[str, Any]) -> None:
    if not usuario.puede_conciliar(inc["tipo"], inc["unidad_negocio_id"]):
        if inc["tipo"] == "cambio_proveedor":
            raise AccesoDenegado("El cambio de proveedor solo lo concilia el basculista de la unidad o un Admin.")
        raise AccesoDenegado("Concilia el basculista de la unidad, un comprador o un Admin.")
    if inc["estado"] != "pendiente":
        raise Conflicto("Esta boleta ya se resolvió.", codigo="incidencia_resuelta")


def vincular(
    op: Operacion,
    usuario: Usuario,
    incidencia_id: int,
    *,
    preboleta_id: int,
    recepcion_id: int | None,
    cajas_nueva_carga: int | None,
    comentario: str | None,
) -> int:
    """Liga la boleta a una carga existente (sin boleta) o a una carga nueva. Devuelve la carga."""
    inc = _cargar_incidencia(op.con, usuario, incidencia_id, bloquear=True)
    motor_boletas.bloquear_boleta(op.con, inc["boleta_id"])
    _validar_resolucion(op, usuario, inc)
    b = op.con.execute("SELECT * FROM core.boletas WHERE id = %s", (inc["boleta_id"],)).fetchone()
    assert b is not None
    if op.con.execute("SELECT 1 FROM preboletas.recepciones WHERE boleta_id = %s", (b["id"],)).fetchone():
        raise Conflicto("La boleta ya está ligada.", codigo="boleta_ligada")
    pb = cargar_preboleta(op.con, preboleta_id)
    if pb["unidad_negocio_id"] != b["unidad_negocio_id"]:
        raise SolicitudInvalida("La preboleta debe ser de la unidad de la boleta.", codigo="unidad_distinta")

    if recepcion_id is not None:
        slot = bloquear_slot(op.con, pb["unidad_negocio_id"])
        fila = op.con.execute(
            "SELECT * FROM preboletas.recepciones WHERE id = %s FOR UPDATE", (recepcion_id,)
        ).fetchone()
        if fila is None or fila["preboleta_id"] != pb["id"] or fila["cancelada_at"] or fila["boleta_id"]:
            raise SolicitudInvalida("Elige una carga sin boleta de esa preboleta.")
        r = dict(fila)
        if slot["recepcion_id"] == r["id"]:
            liberar_slot(op, r["unidad_negocio_id"])
    else:
        if cajas_nueva_carga is None or cajas_nueva_carga <= 0:
            raise SolicitudInvalida("Elige la carga, o «Nueva carga» con sus cajas.")
        fila = op.con.execute(
            """
            INSERT INTO preboletas.recepciones (preboleta_id, unidad_negocio_id, cajas_declaradas, via, basculista_id)
            VALUES (%s, %s, %s, 'manual', %s)
            RETURNING *
            """,
            (pb["id"], pb["unidad_negocio_id"], cajas_nueva_carga, usuario.colaborador_id),
        ).fetchone()
        assert fila is not None
        r = dict(fila)
        op.mov(
            "carga_manual",
            pb["unidad_negocio_id"],
            f"{folio(pb['id'])}: carga de {cajas_nueva_carga} cajas registrada al conciliar {b['folio']}",
            preboleta_id=pb["id"],
            recepcion_id=r["id"],
        )

    pb = cargar_preboleta(op.con, preboleta_id, bloquear=True)
    cantidad.reabrir(op, pb, f"se concilió la boleta {b['folio']}", recepcion_id=r["id"])
    op.con.execute(
        "UPDATE preboletas.recepciones SET boleta_id = %s, vinculada_at = now(), vinculada_por_id = %s WHERE id = %s",
        (b["id"], usuario.colaborador_id, r["id"]),
    )
    comentario = (comentario or "").strip() or None
    op.con.execute(
        "UPDATE preboletas.incidencias SET estado = 'conciliada', conciliada_con_recepcion_id = %s, "
        "resuelto_por_id = %s, resuelto_at = now(), comentario = %s WHERE id = %s",
        (r["id"], usuario.colaborador_id, comentario, inc["id"]),
    )
    distinto = b["proveedor_cuenta"] != pb["proveedor_cuenta"]
    op.mov(
        "boleta_conciliada",
        b["unidad_negocio_id"],
        f"{b['folio']} conciliada con {folio(pb['id'])}{' (proveedores distintos, documentado)' if distinto else ''}"
        f"{f'. {comentario}' if comentario else ''}",
        preboleta_id=pb["id"],
        recepcion_id=r["id"],
        boleta_id=b["id"],
        incidencia_id=inc["id"],
        # Desde ahora se compara contra el proveedor que traía la boleta al conciliar.
        datos={"proveedor_cuenta": b["proveedor_cuenta"]},
    )
    op.evento(
        "incidencia.resuelta",
        incidencia_id=inc["id"],
        tipo=inc["tipo"],
        estado="conciliada",
        unidad_negocio_id=b["unidad_negocio_id"],
        preboleta_id=pb["id"],
        comprador_id=pb["comprador_id"],
    )
    op.evento(
        "boleta.vinculada",
        boleta_id=b["id"],
        folio=b["folio"],
        preboleta_id=pb["id"],
        unidad_negocio_id=pb["unidad_negocio_id"],
        comprador_id=pb["comprador_id"],
    )
    cantidad.reevaluar(op, pb)
    # El motor completa lo que siga (salida de báscula y cierre si ya tiene 2da pesada).
    motor_boletas.procesar_boleta(op.con, b["id"])
    cantidad.evento_preboleta(op, pb)
    return int(r["id"])


def descartar(op: Operacion, usuario: Usuario, incidencia_id: int, comentario: str) -> None:
    """La boleta no corresponde a ninguna preboleta (ej. compra directa). Comentario obligatorio."""
    inc = _cargar_incidencia(op.con, usuario, incidencia_id, bloquear=True)
    _validar_resolucion(op, usuario, inc)
    comentario = comentario.strip()
    if not comentario:
        raise SolicitudInvalida("El comentario es obligatorio para descartar.")
    op.con.execute(
        "UPDATE preboletas.incidencias SET estado = 'descartada', resuelto_por_id = %s, resuelto_at = now(), "
        "comentario = %s WHERE id = %s",
        (usuario.colaborador_id, comentario, inc["id"]),
    )
    b = op.con.execute("SELECT folio FROM core.boletas WHERE id = %s", (inc["boleta_id"],)).fetchone()
    op.mov(
        "boleta_descartada",
        inc["unidad_negocio_id"],
        f"{b['folio'] if b else inc['boleta_id']} descartada: {comentario}",
        preboleta_id=inc["preboleta_id"],
        boleta_id=inc["boleta_id"],
        incidencia_id=inc["id"],
    )
    op.evento(
        "incidencia.resuelta",
        incidencia_id=inc["id"],
        tipo=inc["tipo"],
        estado="descartada",
        unidad_negocio_id=inc["unidad_negocio_id"],
        preboleta_id=inc["preboleta_id"],
    )
