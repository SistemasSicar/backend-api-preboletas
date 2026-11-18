"""
Consultas de solo lectura que alimentan las pantallas de listas: bitácora, incidencias de
cantidad, propuestas y avisos. Cada una aplica la visibilidad del rol.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.bd import Conexion
from app.errores import AccesoDenegado, SolicitudInvalida
from app.servicios.comun import folio
from app.servicios.preboletas import SELECT_PROPUESTAS, filtro_visibilidad, propuesta_a_dict
from app.usuario import PERMISO_AUTORIZAR, Usuario


# ----------------------------------------------------------------------
# Bitácora
# ----------------------------------------------------------------------
def movimientos(
    con: Conexion,
    usuario: Usuario,
    *,
    preboleta_id: int | None,
    boleta_id: int | None,
    colaborador_id: int | None,
    proveedor_cuenta: str | None,
    tipos: list[str],
    desde: date | None,
    hasta: date | None,
    limite: int,
    desplazamiento: int,
) -> dict[str, Any]:
    """
    Admin ve todo; Basculista, lo de su unidad; Comprador, lo de sus preboletas y lo que él hizo.
    `colaborador_id = 0` filtra lo que hizo el sistema. Las fechas son días de México.
    """
    if desde and hasta and desde > hasta:
        raise SolicitudInvalida("La fecha «Desde» no puede ser posterior a «Hasta».")
    condiciones: list[str] = []
    params: list[Any] = []
    if not usuario.es_admin:
        partes = []
        if usuario.es_basculista:
            partes.append("m.unidad_negocio_id = %s")
            params.append(usuario.unidad_negocio_id)
        if usuario.es_comprador:
            partes.append("p.comprador_id = %s OR m.colaborador_id = %s")
            params += [usuario.colaborador_id, usuario.colaborador_id]
        condiciones.append("(" + " OR ".join(partes) + ")" if partes else "false")
    if preboleta_id is not None:
        condiciones.append("m.preboleta_id = %s")
        params.append(preboleta_id)
    if boleta_id is not None:
        condiciones.append("m.boleta_id = %s")
        params.append(boleta_id)
    if colaborador_id is not None:
        if colaborador_id == 0:
            condiciones.append("m.colaborador_id IS NULL")
        else:
            condiciones.append("m.colaborador_id = %s")
            params.append(colaborador_id)
    if proveedor_cuenta:
        condiciones.append("(p.proveedor_cuenta = %s OR b.proveedor_cuenta = %s)")
        params += [proveedor_cuenta, proveedor_cuenta]
    if tipos:
        condiciones.append("m.tipo = ANY(%s)")
        params.append(tipos)
    if desde:
        condiciones.append("m.ocurrido_at >= (%s::date::timestamp AT TIME ZONE 'America/Mexico_City')")
        params.append(desde)
    if hasta:
        condiciones.append("m.ocurrido_at < ((%s::date + 1)::timestamp AT TIME ZONE 'America/Mexico_City')")
        params.append(hasta)
    where = " AND ".join(condiciones) or "true"
    desde_sql = """
          FROM preboletas.movimientos m
          LEFT JOIN preboletas.preboletas p ON p.id = m.preboleta_id
          LEFT JOIN core.boletas b ON b.id = m.boleta_id
    """
    total = con.execute(f"SELECT count(*) AS n {desde_sql} WHERE {where}", params).fetchone()
    filas = con.execute(
        f"""
        SELECT m.id, m.ocurrido_at, m.unidad_negocio_id, m.tipo, tm.nombre AS tipo_nombre, tm.badge, tm.color,
               m.descripcion, m.preboleta_id, m.recepcion_id, m.boleta_id, b.folio AS boleta_folio,
               m.incidencia_id, m.propuesta_id, m.colaborador_id,
               COALESCE(c.nombre, c.correo, 'Sistema') AS quien
          {desde_sql}
          JOIN preboletas.tipos_movimiento tm ON tm.clave = m.tipo
          LEFT JOIN core.colaboradores c ON c.id = m.colaborador_id
         WHERE {where}
         ORDER BY m.ocurrido_at DESC, m.id DESC
         LIMIT %s OFFSET %s
        """,
        [*params, limite, desplazamiento],
    ).fetchall()
    return {
        "elementos": [
            {**dict(f), "preboleta_folio": folio(f["preboleta_id"]) if f["preboleta_id"] else None} for f in filas
        ],
        "total": int(total["n"]) if total else 0,
    }


# ----------------------------------------------------------------------
# Incidencias de cantidad (y cierres a mano)
# ----------------------------------------------------------------------
def incidencias_cantidad(con: Conexion, usuario: Usuario, estado: str | None) -> list[dict[str, Any]]:
    """Las resuelve cualquier comprador de cualquier unidad, o un Admin: se listan todas las unidades."""
    if not (usuario.es_comprador or usuario.es_admin):
        raise AccesoDenegado("Las incidencias de cantidad las ven los compradores y los Admin.")
    params: list[Any] = []
    filtro = ""
    if estado:
        filtro = "AND i.estado = %s"
        params.append(estado)
    filas = con.execute(
        f"""
        SELECT i.id, i.tipo, i.estado, i.unidad_negocio_id, u.nombre AS unidad, i.preboleta_id,
               p.proveedor_nombre, p.comprador_id, COALESCE(cc.nombre, cc.correo) AS comprador,
               p.cajas_minimas, p.cajas_maximas, p.cajas_negociadas,
               i.cajas_acumuladas, i.cajas_limite, i.cajas_diferencia, i.boleta_id, b.folio AS boleta_folio,
               i.comentario, i.creado_at, i.resuelto_at, COALESCE(cr.nombre, cr.correo) AS resuelto_por
          FROM preboletas.incidencias i
          JOIN preboletas.preboletas p ON p.id = i.preboleta_id
          JOIN core.unidades_negocio u ON u.id = i.unidad_negocio_id
          JOIN core.colaboradores cc ON cc.id = p.comprador_id
          LEFT JOIN core.colaboradores cr ON cr.id = i.resuelto_por_id
          LEFT JOIN core.boletas b ON b.id = i.boleta_id
         WHERE i.tipo IN ('sobre_entrega', 'faltante', 'cierre_manual') {filtro}
         ORDER BY (i.estado = 'pendiente') DESC, i.creado_at DESC
         LIMIT 300
        """,
        params,
    ).fetchall()
    return [
        {
            **dict(f),
            "folio": folio(f["preboleta_id"]),
            # Ver el detalle de la preboleta sigue la regla general (solo el dueño, la unidad o Admin).
            "preboleta_visible": usuario.puede_ver_preboleta(f["comprador_id"], f["unidad_negocio_id"]),
        }
        for f in filas
    ]


# ----------------------------------------------------------------------
# Propuestas de precio
# ----------------------------------------------------------------------
def propuestas(con: Conexion, usuario: Usuario, estado: str | None) -> list[dict[str, Any]]:
    """Todas las unidades. Cada fila dice si quien consulta puede autorizarla."""
    params: list[Any] = []
    filtro = "true"
    if estado:
        filtro = "pp.estado = %s"
        params.append(estado)
    filas = con.execute(
        f"""
        SELECT x.*, vr.precio_kg_efectivo AS precio_vigente_carga, vr.tipo_pago_efectivo AS tipo_vigente_carga,
               vp.precio_kg_efectivo AS precio_vigente, vp.tipo_pago_efectivo AS tipo_vigente
          FROM ({SELECT_PROPUESTAS} WHERE {filtro}) x
          JOIN preboletas.v_preboletas vp ON vp.id = x.preboleta_id
          LEFT JOIN preboletas.v_recepciones vr ON vr.recepcion_id = x.recepcion_id
         ORDER BY (x.estado = 'pendiente') DESC, x.propuesto_at DESC
         LIMIT 300
        """,
        params,
    ).fetchall()
    resultado = []
    for f in filas:
        d = propuesta_a_dict(f)
        if d["recepcion_id"] is not None:
            d["precio_vigente"], d["tipo_vigente"] = d["precio_vigente_carga"], d["tipo_vigente_carga"]
        d.pop("precio_vigente_carga")
        d.pop("tipo_vigente_carga")
        propia = d["propuesto_por_id"] == usuario.colaborador_id
        d["puede_resolver"] = (
            d["estado"] == "pendiente"
            and usuario.tiene_permiso(PERMISO_AUTORIZAR, d["unidad_negocio_id"])
            and (not propia or usuario.es_admin)
        )
        d["es_propia"] = propia
        d["preboleta_visible"] = usuario.puede_ver_preboleta(d["comprador_id"], d["unidad_negocio_id"])
        resultado.append(d)
    return resultado


# ----------------------------------------------------------------------
# Avisos (campana)
# ----------------------------------------------------------------------
_SELECT_AVISOS = """
    SELECT n.id, n.creado_at, n.mensaje, n.leida_at, n.preboleta_id, n.incidencia_id, n.propuesta_id,
           n.destinatario_id, COALESCE(c.nombre, c.correo) AS destinatario,
           i.tipo AS incidencia_tipo,
           COALESCE((
               SELECT json_agg(json_build_object('canal', e.canal, 'estado', e.estado, 'error', e.ultimo_error)
                               ORDER BY e.canal)
                 FROM preboletas.notificaciones e
                -- Los envíos por correo y push nacen en la misma transacción que el aviso de la campana:
                -- mismo destinatario, mismo mensaje y mismo instante.
                WHERE e.destinatario_id = n.destinatario_id AND e.creado_at = n.creado_at
                  AND e.mensaje = n.mensaje AND e.canal <> 'app'
           ), '[]') AS entregas
      FROM preboletas.notificaciones n
      JOIN core.colaboradores c ON c.id = n.destinatario_id
      LEFT JOIN preboletas.incidencias i ON i.id = n.incidencia_id
"""


def avisos(con: Conexion, usuario: Usuario, *, todos: bool, limite: int) -> list[dict[str, Any]]:
    if todos and not usuario.es_admin:
        raise AccesoDenegado("Solo un Admin ve todos los envíos.")
    where = "n.canal = 'app'" + ("" if todos else " AND n.destinatario_id = %s")
    params: list[Any] = [] if todos else [usuario.colaborador_id]
    filas = con.execute(
        f"{_SELECT_AVISOS} WHERE {where} ORDER BY n.creado_at DESC, n.id DESC LIMIT %s", [*params, limite]
    ).fetchall()
    return [
        {
            **dict(f),
            "folio": folio(f["preboleta_id"]) if f["preboleta_id"] else None,
            "leida": f["leida_at"] is not None,
        }
        for f in filas
    ]


def avisos_sin_leer(con: Conexion, usuario: Usuario) -> int:
    fila = con.execute(
        "SELECT count(*) AS n FROM preboletas.notificaciones WHERE destinatario_id = %s AND canal = 'app' "
        "AND leida_at IS NULL",
        (usuario.colaborador_id,),
    ).fetchone()
    return int(fila["n"]) if fila else 0


def marcar_leidos(con: Conexion, usuario: Usuario, ids: list[int] | None) -> int:
    """Marca como leídos los avisos indicados (o todos los míos si no se indican)."""
    filtro = "" if ids is None else " AND id = ANY(%s)"
    params: list[Any] = [usuario.colaborador_id] + ([] if ids is None else [ids])
    cursor = con.execute(
        "UPDATE preboletas.notificaciones SET leida_at = now() WHERE destinatario_id = %s AND canal = 'app' "
        f"AND leida_at IS NULL{filtro}",
        params,
    )
    return cursor.rowcount


def registrar_dispositivo(con: Conexion, usuario: Usuario, token: str, plataforma: str, nombre: str | None) -> None:
    """
    Token de Expo de un teléfono. Un token pertenece a una sola persona: si otra lo tenía
    (se cambió de cuenta en el mismo teléfono), pasa a quien lo registra ahora.
    """
    con.execute(
        """
        INSERT INTO preboletas.dispositivos_push (colaborador_id, token, plataforma, nombre_dispositivo)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (token) DO UPDATE
           SET colaborador_id = EXCLUDED.colaborador_id, plataforma = EXCLUDED.plataforma,
               nombre_dispositivo = EXCLUDED.nombre_dispositivo, activo = true, desactivado_at = NULL,
               motivo_baja = NULL, ultimo_uso_at = now()
        """,
        (usuario.colaborador_id, token, plataforma, nombre),
    )


def baja_dispositivo(con: Conexion, usuario: Usuario, token: str) -> None:
    con.execute(
        "UPDATE preboletas.dispositivos_push SET activo = false, desactivado_at = now(), motivo_baja = 'cierre_sesion' "
        "WHERE token = %s AND colaborador_id = %s AND activo",
        (token, usuario.colaborador_id),
    )


# ----------------------------------------------------------------------
# Resumen de pendientes (los números de las pestañas)
# ----------------------------------------------------------------------
def resumen(con: Conexion, usuario: Usuario) -> dict[str, Any]:
    """
    Lo que la persona tiene por resolver (mismas reglas que las listas de cada pantalla) y los kg que
    entraron hoy en las preboletas que puede ver.
    """
    incidencias = 0
    if usuario.es_comprador or usuario.es_admin:
        fila = con.execute(
            "SELECT count(*) AS n FROM preboletas.incidencias WHERE estado = 'pendiente' "
            "AND tipo IN ('sobre_entrega', 'faltante')"
        ).fetchone()
        incidencias = int(fila["n"]) if fila else 0
    conciliaciones = sum(
        int(f["n"])
        for f in con.execute(
            "SELECT tipo, unidad_negocio_id, count(*) AS n FROM preboletas.v_conciliacion GROUP BY 1, 2"
        ).fetchall()
        if usuario.puede_conciliar(f["tipo"], f["unidad_negocio_id"])
    )
    propuestas = sum(
        int(f["n"])
        for f in con.execute(
            "SELECT unidad_negocio_id, propuesto_por_id = %s AS propia, count(*) AS n "
            "FROM preboletas.propuestas_precio WHERE estado = 'pendiente' GROUP BY 1, 2",
            (usuario.colaborador_id,),
        ).fetchall()
        if usuario.tiene_permiso(PERMISO_AUTORIZAR, f["unidad_negocio_id"]) and (not f["propia"] or usuario.es_admin)
    )
    return {
        "incidencias": incidencias,
        "conciliaciones": conciliaciones,
        "propuestas": propuestas,
        "avisos_sin_leer": avisos_sin_leer(con, usuario),
        "kg_recibidos_hoy": kg_recibidos_hoy(con, usuario),
    }


def kg_recibidos_hoy(con: Conexion, usuario: Usuario) -> float:
    """
    Peso neto de las boletas completadas HOY (2da pesada en el día de México) ligadas a cargas no canceladas,
    con la misma visibilidad que la lista de preboletas: Admin todo, Comprador las suyas, Basculista su unidad.
    Cuenta lo que entra hoy de preboletas atrasadas; no cuenta cargas de otros días.
    """
    visible, params = filtro_visibilidad(usuario, "p")
    fila = con.execute(
        "SELECT COALESCE(sum(r.peso_neto), 0) AS kg FROM preboletas.v_recepciones r "
        "JOIN preboletas.preboletas p ON p.id = r.preboleta_id "
        "WHERE NOT r.cancelada AND r.estado_boleta = 'completada' "
        "AND (r.segunda_pesada_at AT TIME ZONE 'America/Mexico_City')::date = core.hoy() "
        f"AND {visible}",
        params,
    ).fetchone()
    return float(fila["kg"]) if fila else 0.0


def precio_del_dia(
    con: Conexion, usuario: Usuario, unidad_negocio_id: int, producto_codigo: str, fecha: date
) -> dict[str, Any]:
    """
    Precio del día y tolerancia de una fruta en una unidad, para capturar una preboleta.
    Lo consultan compradores, Admin y quien captura precios en esa unidad.
    """
    if not (usuario.es_comprador or usuario.es_admin or usuario.tiene_permiso("precios_diarios", unidad_negocio_id)):
        raise AccesoDenegado("Consultan el precio del día los compradores, los Admin y quien captura precios.")
    fila = con.execute(
        """
        SELECT pd.precio_kg, pd.tipo_pago_id, tp.nombre AS tipo_pago, tp.clave AS tipo_pago_clave,
               t.tolerancia_pct
          FROM (SELECT 1) x
          LEFT JOIN preboletas.precios_diarios pd ON pd.unidad_negocio_id = %s AND pd.producto_codigo = %s
               AND pd.fecha = %s
          LEFT JOIN preboletas.tipos_pago tp ON tp.id = pd.tipo_pago_id
          LEFT JOIN preboletas.tolerancias t ON t.unidad_negocio_id = %s AND t.producto_codigo = %s
        """,
        (unidad_negocio_id, producto_codigo, fecha, unidad_negocio_id, producto_codigo),
    ).fetchone()
    assert fila is not None
    return {"unidad_negocio_id": unidad_negocio_id, "producto_codigo": producto_codigo, "fecha": fecha, **dict(fila)}
