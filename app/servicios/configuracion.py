"""
Configuración por unidad: precio del día, tolerancias, fecha de implementación y tipos de caja.

* Precio del día: lo captura quien tenga `precios_diarios` en la unidad, o un Admin. Corregir uno
  de hace más de 5 días es solo de Admin. Las preboletas sin precio propio lo siguen solas (las
  vistas lo calculan), y las propuestas pendientes que quedan iguales se resuelven solas.
* Tolerancia: solo Admin. Se copia a cada preboleta AL CREARLA; cambiarla no toca las ya creadas.
* Fecha de implementación: sin fecha, la unidad no opera (el motor ignora sus boletas).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg

from app.bd import Conexion
from app.errores import AccesoDenegado, Conflicto, NoEncontrado, SolicitudInvalida
from app.integraciones.dataverse import Fruta
from app.servicios.comun import (
    Operacion,
    dinero,
    folio,
    hoy,
    nombre_tipo_pago,
    precio_txt,
    tipo_pago_id,
)
from app.usuario import PERMISO_PRECIOS, Usuario

DIAS_CORRECCION_SIN_ADMIN = 5


def _unidad(con: Conexion, unidad_negocio_id: int) -> str:
    fila = con.execute("SELECT nombre FROM core.unidades_negocio WHERE id = %s", (unidad_negocio_id,)).fetchone()
    if fila is None:
        raise NoEncontrado("La unidad no existe.")
    return str(fila["nombre"])


def precios_de_unidad(
    con: Conexion, usuario: Usuario, unidad_negocio_id: int, fecha: date | None, frutas: list[Fruta] | None
) -> dict[str, Any]:
    """
    Una fila por fruta: precio y tipo del día, tolerancia y el tipo sugerido para capturar.
    `frutas` viene de Dataverse (todas las del conjunto MP); si Dataverse no respondió llega None
    y se muestran solo las frutas que ya tienen precio o tolerancia en la unidad.
    """
    if not usuario.tiene_permiso(PERMISO_PRECIOS, unidad_negocio_id):
        raise AccesoDenegado("No tienes el permiso precios_diarios en esta unidad.", codigo="sin_permiso")
    unidad = _unidad(con, unidad_negocio_id)
    fecha = fecha or hoy(con)
    precios = {str(f["producto_codigo"]): f for f in _precios_del_dia(con, [unidad_negocio_id], fecha)}
    tolerancias = {
        str(f["producto_codigo"]): dict(f)
        for f in con.execute(
            "SELECT producto_codigo, producto_nombre, tolerancia_pct FROM preboletas.tolerancias "
            "WHERE unidad_negocio_id = %s",
            (unidad_negocio_id,),
        ).fetchall()
    }
    nombres: dict[str, str] = {}
    for origen in (tolerancias, precios):
        for codigo, fila in origen.items():
            nombres[codigo] = str(fila["producto_nombre"])
    for fruta in frutas or []:
        nombres[fruta.codigo] = fruta.nombre

    rendimiento = tipo_pago_id(con, "rendimiento")
    filas = []
    for codigo, nombre in sorted(nombres.items(), key=lambda x: x[1]):
        p = precios.get(codigo)
        t = tolerancias.get(codigo)
        sugerido = _tipo_sugerido(con, unidad_negocio_id, codigo) or rendimiento
        filas.append(
            {
                "producto_codigo": codigo,
                "producto_nombre": nombre,
                "precio_kg": p["precio_kg"] if p else None,
                "tipo_pago_id": p["tipo_pago_id"] if p else None,
                "tipo_pago": p["tipo_pago"] if p else None,
                "tipo_pago_clave": p["tipo_pago_clave"] if p else None,
                "actualizado_por": p["actualizado_por"] if p else None,
                "actualizado_at": p["actualizado_at"] if p else None,
                "tolerancia_pct": t["tolerancia_pct"] if t else None,
                "tipo_pago_sugerido_id": sugerido,
            }
        )
    return {
        "unidad_negocio_id": unidad_negocio_id,
        "unidad": unidad,
        "fecha": fecha,
        "frutas": filas,
        "dataverse_disponible": frutas is not None,
        "puede_tolerancia": usuario.es_admin,
    }


def _precios_del_dia(con: Conexion, unidades: list[int], fecha: date) -> list[dict[str, Any]]:
    """
    Precio, tipo de pago y quién lo definió de cada fruta CON precio en esas unidades y fecha. Solo lee
    `precios_diarios` (guarda el nombre de la fruta de ese día): no consulta Dataverse.
    La comparten la pantalla de una unidad y el resumen de todas (`precios_hoy`).
    """
    return [
        dict(f)
        for f in con.execute(
            """
            SELECT pd.unidad_negocio_id, pd.producto_codigo, pd.producto_nombre, pd.precio_kg, pd.tipo_pago_id,
                   tp.nombre AS tipo_pago, tp.clave AS tipo_pago_clave, pd.actualizado_at,
                   COALESCE(c.nombre, c.correo) AS actualizado_por
              FROM preboletas.precios_diarios pd
              JOIN preboletas.tipos_pago tp ON tp.id = pd.tipo_pago_id
              JOIN core.colaboradores c ON c.id = pd.actualizado_por_id
             WHERE pd.unidad_negocio_id = ANY(%s) AND pd.fecha = %s
             ORDER BY pd.producto_nombre
            """,
            (unidades, fecha),
        ).fetchall()
    ]


def precios_hoy(con: Conexion, usuario: Usuario) -> list[dict[str, Any]]:
    """
    Resumen del precio de HOY (México) de cada unidad donde la persona puede definirlo: Admin todas;
    los demás, las de su permiso precios_diarios. Una unidad sin frutas con precio va con la lista vacía.
    Rápido: solo la base, sin Dataverse (para la lista de "Precio del día" de la app).
    """
    if not usuario.tiene_permiso_en_alguna(PERMISO_PRECIOS):
        raise AccesoDenegado("No tienes el permiso precios_diarios en ninguna unidad.", codigo="sin_permiso")
    lista = [u for u in unidades(con) if usuario.tiene_permiso(PERMISO_PRECIOS, int(u["id"]))]
    fecha = hoy(con)
    por_unidad: dict[int, list[dict[str, Any]]] = {}
    for f in _precios_del_dia(con, [int(u["id"]) for u in lista], fecha):
        por_unidad.setdefault(int(f["unidad_negocio_id"]), []).append(f)
    return [
        {
            "unidad_negocio_id": u["id"],
            "unidad": u["nombre"],
            "fecha": fecha,
            "frutas": por_unidad.get(int(u["id"]), []),
        }
        for u in lista
    ]


def _tipo_sugerido(con: Conexion, unidad_negocio_id: int, codigo: str) -> int | None:
    """El último tipo usado en esa unidad y fruta; si no, el último de la unidad."""
    fila = con.execute(
        """
        SELECT tipo_pago_id FROM preboletas.precios_diarios
         WHERE unidad_negocio_id = %s
         ORDER BY (producto_codigo = %s) DESC, fecha DESC, actualizado_at DESC
         LIMIT 1
        """,
        (unidad_negocio_id, codigo),
    ).fetchone()
    return int(fila["tipo_pago_id"]) if fila else None


def guardar_precio(
    op: Operacion, usuario: Usuario, unidad_negocio_id: int, fruta: Fruta, fecha: date, precio: Decimal, tipo: int
) -> None:
    con = op.con
    if not usuario.tiene_permiso(PERMISO_PRECIOS, unidad_negocio_id):
        raise AccesoDenegado(
            f"No tienes el permiso precios_diarios en {_unidad(con, unidad_negocio_id)}.", codigo="sin_permiso"
        )
    unidad = _unidad(con, unidad_negocio_id)
    if precio <= 0:
        raise SolicitudInvalida("El precio debe ser mayor a 0.")
    activo = con.execute("SELECT activo FROM preboletas.tipos_pago WHERE id = %s", (tipo,)).fetchone()
    if activo is None or not activo["activo"]:
        raise SolicitudInvalida("Elige un tipo de pago activo.")
    if (hoy(con) - fecha).days > DIAS_CORRECCION_SIN_ADMIN and not usuario.es_admin:
        raise AccesoDenegado("Solo un Admin corrige precios de hace más de 5 días.", codigo="sin_permiso")

    antes = con.execute(
        "SELECT precio_kg, tipo_pago_id FROM preboletas.precios_diarios WHERE unidad_negocio_id = %s "
        "AND producto_codigo = %s AND fecha = %s FOR UPDATE",
        (unidad_negocio_id, fruta.codigo, fecha),
    ).fetchone()
    antes_txt = (
        f"{dinero(antes['precio_kg'])} {nombre_tipo_pago(con, antes['tipo_pago_id'])}" if antes else "sin asignar"
    )
    con.execute(
        """
        INSERT INTO preboletas.precios_diarios
            (unidad_negocio_id, producto_codigo, producto_nombre, fecha, precio_kg, tipo_pago_id, actualizado_por_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (unidad_negocio_id, producto_codigo, fecha) DO UPDATE
           SET precio_kg = EXCLUDED.precio_kg, tipo_pago_id = EXCLUDED.tipo_pago_id,
               producto_nombre = EXCLUDED.producto_nombre, actualizado_por_id = EXCLUDED.actualizado_por_id
        """,
        (unidad_negocio_id, fruta.codigo, fruta.nombre, fecha, precio, tipo, usuario.colaborador_id),
    )
    tipo_nombre = nombre_tipo_pago(con, tipo)
    op.mov(
        "precio_diario",
        unidad_negocio_id,
        f"Precio del {fecha.isoformat()} de {fruta.nombre} en {unidad}: {antes_txt} → {dinero(precio)} {tipo_nombre}",
    )

    # Las preboletas de esa unidad, fruta y fecha sin precio propio siguen al precio del día.
    siguen = con.execute(
        "SELECT id, comprador_id FROM preboletas.preboletas WHERE unidad_negocio_id = %s AND producto_codigo = %s "
        "AND fecha_llegada = %s AND precio_pactado_kg IS NULL AND tipo_pago_id IS NULL",
        (unidad_negocio_id, fruta.codigo, fecha),
    ).fetchall()
    for pb in siguen:
        op.mov(
            "precio_diario",
            unidad_negocio_id,
            f"{folio(pb['id'])} toma el precio del día: {tipo_nombre} {precio_txt(con, precio, tipo)}",
            preboleta_id=pb["id"],
            sistema=True,
        )
        op.evento(
            "preboleta.actualizada",
            preboleta_id=pb["id"],
            unidad_negocio_id=unidad_negocio_id,
            comprador_id=pb["comprador_id"],
        )

    # Propuestas pendientes que quedan iguales al nuevo precio del día: se resuelven solas.
    pendientes = con.execute(
        """
        SELECT pp.* FROM preboletas.propuestas_precio pp
          JOIN preboletas.preboletas p ON p.id = pp.preboleta_id
         WHERE pp.estado = 'pendiente' AND p.unidad_negocio_id = %s AND p.producto_codigo = %s
           AND p.fecha_llegada = %s
           FOR UPDATE OF pp
        """,
        (unidad_negocio_id, fruta.codigo, fecha),
    ).fetchall()
    for pr in pendientes:
        igual_precio = pr["precio_propuesto_kg"] is None or pr["precio_propuesto_kg"] == precio
        igual_tipo = pr["tipo_pago_propuesto_id"] is None or pr["tipo_pago_propuesto_id"] == tipo
        if not (igual_precio and igual_tipo):
            continue
        con.execute(
            "UPDATE preboletas.propuestas_precio SET estado = 'resuelta_sola', resuelto_at = now() WHERE id = %s",
            (pr["id"],),
        )
        op.mov(
            "propuesta_cancelada",
            unidad_negocio_id,
            f"Propuesta #{pr['id']} resuelta sola: el precio del día quedó igual",
            preboleta_id=pr["preboleta_id"],
            recepcion_id=pr["recepcion_id"],
            propuesta_id=pr["id"],
            sistema=True,
        )
        op.notificar(
            [pr["propuesto_por_id"]],
            f"Tu propuesta para {folio(pr['preboleta_id'])} se resolvió sola: el precio del día quedó igual.",
            preboleta_id=pr["preboleta_id"],
            propuesta_id=pr["id"],
        )
    op.evento(
        "precio_diario.actualizado", unidad_negocio_id=unidad_negocio_id, producto_codigo=fruta.codigo, fecha=fecha
    )


def guardar_tolerancia(op: Operacion, usuario: Usuario, unidad_negocio_id: int, fruta: Fruta, pct: Decimal) -> None:
    if not usuario.es_admin:
        raise AccesoDenegado("Solo un Admin cambia tolerancias.")
    unidad = _unidad(op.con, unidad_negocio_id)
    if pct < 0 or pct > 100:
        raise SolicitudInvalida("La tolerancia va de 0 a 100%.")
    op.con.execute(
        """
        INSERT INTO preboletas.tolerancias
            (unidad_negocio_id, producto_codigo, producto_nombre, tolerancia_pct, actualizado_por_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (unidad_negocio_id, producto_codigo) DO UPDATE
           SET tolerancia_pct = EXCLUDED.tolerancia_pct, producto_nombre = EXCLUDED.producto_nombre,
               actualizado_por_id = EXCLUDED.actualizado_por_id
        """,
        (unidad_negocio_id, fruta.codigo, fruta.nombre, pct, usuario.colaborador_id),
    )
    op.mov(
        "tolerancia_global",
        unidad_negocio_id,
        f"Tolerancia de {fruta.nombre} en {unidad}: {pct:g}% (no cambia preboletas ya creadas)",
    )
    op.evento("precio_diario.actualizado", unidad_negocio_id=unidad_negocio_id, producto_codigo=fruta.codigo)


def guardar_implementacion(op: Operacion, usuario: Usuario, unidad_negocio_id: int, inicio: datetime | None) -> None:
    """
    `inicio` con zona horaria se guarda tal cual; sin zona se interpreta en hora de México
    (la del negocio), nunca en la del servidor ni la del navegador.
    """
    if not usuario.es_admin:
        raise AccesoDenegado("Solo un Admin define la fecha de implementación.")
    unidad = _unidad(op.con, unidad_negocio_id)
    params: tuple[Any, ...]
    if inicio is None:
        valor_sql, params = "NULL::timestamptz", ()
    elif inicio.tzinfo is None:
        valor_sql, params = "(%s::timestamp AT TIME ZONE 'America/Mexico_City')", (inicio,)
    else:
        valor_sql, params = "%s::timestamptz", (inicio,)
    fila = op.con.execute(
        f"""
        INSERT INTO preboletas.unidades_config (unidad_negocio_id, inicio_operacion_at, actualizado_por_id)
        VALUES (%s, {valor_sql}, %s)
        ON CONFLICT (unidad_negocio_id) DO UPDATE
           SET inicio_operacion_at = EXCLUDED.inicio_operacion_at, actualizado_por_id = EXCLUDED.actualizado_por_id
        RETURNING to_char(inicio_operacion_at AT TIME ZONE 'America/Mexico_City', 'DD/MM/YYYY HH24:MI') AS texto
        """,
        (unidad_negocio_id, *params, usuario.colaborador_id),
    ).fetchone()
    assert fila is not None
    texto = f"{unidad} opera desde {fila['texto']}" if inicio else f"{unidad} queda sin operar (pausada)"
    op.mov("implementacion_unidad", unidad_negocio_id, texto)
    op.evento("unidad.actualizada", unidad_negocio_id=unidad_negocio_id)


# ----------------------------------------------------------------------
# Tipos de caja y cajas permitidas por fruta (solo Admin)
# ----------------------------------------------------------------------
def tipos_caja(con: Conexion, *, producto_codigo: str | None, incluir_inactivos: bool) -> list[dict[str, Any]]:
    condiciones = ["true" if incluir_inactivos else "tc.activo"]
    params: list[Any] = []
    if producto_codigo:
        condiciones.append(
            "EXISTS (SELECT 1 FROM preboletas.tipos_caja_productos x WHERE x.tipo_caja_id = tc.id "
            "AND x.producto_codigo = %s)"
        )
        params.append(producto_codigo)
    filas = con.execute(
        f"""
        SELECT tc.id, tc.nombre, tc.kg_por_caja, tc.activo, tc.orden,
               COALESCE(json_agg(json_build_object('codigo', tcp.producto_codigo, 'nombre', tcp.producto_nombre)
                                 ORDER BY tcp.producto_nombre) FILTER (WHERE tcp.producto_codigo IS NOT NULL),
                        '[]') AS frutas
          FROM preboletas.tipos_caja tc
          LEFT JOIN preboletas.tipos_caja_productos tcp ON tcp.tipo_caja_id = tc.id
         WHERE {" AND ".join(condiciones)}
         GROUP BY tc.id
         ORDER BY tc.orden, tc.nombre
        """,
        params,
    ).fetchall()
    return [dict(f) for f in filas]


def _solo_admin(usuario: Usuario) -> None:
    if not usuario.es_admin:
        raise AccesoDenegado("Solo un Admin administra los tipos de caja.")


def crear_caja(op: Operacion, usuario: Usuario, nombre: str, kg_por_caja: Decimal, orden: int) -> int:
    _solo_admin(usuario)
    try:
        fila = op.con.execute(
            "INSERT INTO preboletas.tipos_caja (nombre, kg_por_caja, orden) VALUES (%s, %s, %s) RETURNING id",
            (nombre.strip(), kg_por_caja, orden),
        ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise Conflicto("Ya existe una caja con ese nombre.", codigo="caja_duplicada") from exc
    assert fila is not None
    op.mov("tipo_caja", usuario.unidad_negocio_id, f"Caja nueva: {nombre.strip()} ({kg_por_caja:g} kg)")
    return int(fila["id"])


def cambiar_caja(op: Operacion, usuario: Usuario, caja_id: int, cambios: dict[str, Any]) -> None:
    """No se borran: se desactivan. Cambiar los kg no afecta preboletas ya creadas (guardan su foto)."""
    _solo_admin(usuario)
    if not cambios:
        raise SolicitudInvalida("No hay cambios.", codigo="sin_cambios")
    caja = op.con.execute("SELECT * FROM preboletas.tipos_caja WHERE id = %s FOR UPDATE", (caja_id,)).fetchone()
    if caja is None:
        raise NoEncontrado("La caja no existe.")
    columnas = {k: v for k, v in cambios.items() if k in ("nombre", "kg_por_caja", "activo", "orden")}
    asignaciones = ", ".join(f"{k} = %s" for k in columnas)
    try:
        op.con.execute(f"UPDATE preboletas.tipos_caja SET {asignaciones} WHERE id = %s", (*columnas.values(), caja_id))
    except psycopg.errors.UniqueViolation as exc:
        raise Conflicto("Ya existe una caja con ese nombre.", codigo="caja_duplicada") from exc
    detalle = ", ".join(f"{k}: {caja[k]} → {v}" for k, v in columnas.items())
    op.mov("tipo_caja", usuario.unidad_negocio_id, f"Caja {caja['nombre']}: {detalle}")


def permitir_fruta(op: Operacion, usuario: Usuario, caja_id: int, fruta: Fruta) -> None:
    _solo_admin(usuario)
    caja = op.con.execute("SELECT nombre FROM preboletas.tipos_caja WHERE id = %s", (caja_id,)).fetchone()
    if caja is None:
        raise NoEncontrado("La caja no existe.")
    op.con.execute(
        """
        INSERT INTO preboletas.tipos_caja_productos (tipo_caja_id, producto_codigo, producto_nombre)
        VALUES (%s, %s, %s)
        ON CONFLICT (tipo_caja_id, producto_codigo) DO UPDATE SET producto_nombre = EXCLUDED.producto_nombre
        """,
        (caja_id, fruta.codigo, fruta.nombre),
    )
    op.mov("tipo_caja", usuario.unidad_negocio_id, f"{caja['nombre']} permitida para {fruta.nombre}")


def quitar_fruta(op: Operacion, usuario: Usuario, caja_id: int, producto_codigo: str) -> None:
    _solo_admin(usuario)
    fila = op.con.execute(
        "SELECT tc.nombre, tcp.producto_nombre FROM preboletas.tipos_caja_productos tcp "
        "JOIN preboletas.tipos_caja tc ON tc.id = tcp.tipo_caja_id WHERE tcp.tipo_caja_id = %s "
        "AND tcp.producto_codigo = %s",
        (caja_id, producto_codigo),
    ).fetchone()
    if fila is None:
        raise NoEncontrado("Esa caja no está permitida para esa fruta.")
    try:
        with op.con.transaction():  # punto de guardado: si falla la llave, la transacción sigue viva
            op.con.execute(
                "DELETE FROM preboletas.tipos_caja_productos WHERE tipo_caja_id = %s AND producto_codigo = %s",
                (caja_id, producto_codigo),
            )
    except psycopg.errors.ForeignKeyViolation as exc:
        raise Conflicto(
            "Hay preboletas con esa caja y fruta: no se puede quitar. Desactiva la caja si ya no se usa.",
            codigo="caja_en_uso",
        ) from exc
    op.mov("tipo_caja", usuario.unidad_negocio_id, f"{fila['nombre']} ya no se permite para {fila['producto_nombre']}")


def unidades(con: Conexion) -> list[dict[str, Any]]:
    filas = con.execute(
        """
        SELECT u.id, u.nombre, uc.inicio_operacion_at, uc.inicio_operacion_at IS NOT NULL AS opera
          FROM core.unidades_negocio u
          LEFT JOIN preboletas.unidades_config uc ON uc.unidad_negocio_id = u.id
         ORDER BY u.nombre
        """
    ).fetchall()
    return [dict(f) for f in filas]
