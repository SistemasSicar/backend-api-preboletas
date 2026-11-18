"""
Preboletas: la negociación de fruta con un proveedor.

El ESTADO no se guarda: lo calcula la vista v_preboletas (pendiente, en_bascula, en_proceso,
terminado, terminado_con_incidencias). Proveedor y fruta se validan en Dataverse ANTES de abrir
la transacción (la ruta lo hace) y se guardan con su nombre de ese momento (foto histórica).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.bd import Conexion
from app.config import CLAVE_APP, Configuracion
from app.errores import AccesoDenegado, Conflicto, NoEncontrado, SolicitudInvalida
from app.integraciones.dataverse import Fruta, Proveedor
from app.servicios import cantidad, propuestas
from app.servicios.comun import Operacion, cargar_preboleta, folio, nombre_colaborador, nombre_unidad
from app.usuario import ROL_ADMIN, ROL_COMPRADOR, Usuario


# ----------------------------------------------------------------------
# Visibilidad
# ----------------------------------------------------------------------
def filtro_visibilidad(usuario: Usuario, alias: str = "v") -> tuple[str, list[Any]]:
    """Admin: todas. Comprador: las suyas. Basculista: las de su unidad. Lo demás no existe (404)."""
    if usuario.es_admin:
        return "true", []
    partes: list[str] = []
    params: list[Any] = []
    if usuario.es_comprador:
        partes.append(f"{alias}.comprador_id = %s")
        params.append(usuario.colaborador_id)
    if usuario.es_basculista:
        partes.append(f"{alias}.unidad_negocio_id = %s")
        params.append(usuario.unidad_negocio_id)
    return ("(" + " OR ".join(partes) + ")") if partes else "false", params


def cargar_visible(con: Conexion, usuario: Usuario, preboleta_id: int, *, bloquear: bool = False) -> dict[str, Any]:
    pb = con.execute(
        "SELECT * FROM preboletas.preboletas WHERE id = %s" + (" FOR UPDATE" if bloquear else ""), (preboleta_id,)
    ).fetchone()
    if pb is None or not usuario.puede_ver_preboleta(pb["comprador_id"], pb["unidad_negocio_id"]):
        raise NoEncontrado("La preboleta no existe.")
    return dict(pb)


# ----------------------------------------------------------------------
# Lista
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Filtros:
    unidad_negocio_id: int | None = None
    comprador_id: int | None = None
    proveedor_cuenta: str | None = None
    desde: date | None = None
    hasta: date | None = None
    estados: tuple[str, ...] = ()
    limite: int = 100

    @property
    def sin_filtros(self) -> bool:
        """Sin filtros = las programadas para hoy más las atrasadas (la unidad no cuenta como filtro)."""
        return not (self.comprador_id or self.proveedor_cuenta or self.desde or self.hasta or self.estados)


_SELECT_LISTA = """
    SELECT v.*, u.nombre AS unidad, COALESCE(c.nombre, c.correo) AS comprador, tc.nombre AS tipo_caja,
           (v.fecha_llegada < core.hoy() AND v.cerrada_at IS NULL) AS atrasada, tpe.clave AS tipo_pago_clave
      FROM preboletas.v_preboletas v
      JOIN core.unidades_negocio u ON u.id = v.unidad_negocio_id
      JOIN core.colaboradores c ON c.id = v.comprador_id
      JOIN preboletas.tipos_caja tc ON tc.id = v.tipo_caja_id
      LEFT JOIN preboletas.tipos_pago tpe ON tpe.id = v.tipo_pago_id_efectivo
"""


def listar(con: Conexion, usuario: Usuario, f: Filtros) -> dict[str, Any]:
    if f.desde and f.hasta and f.desde > f.hasta:
        raise SolicitudInvalida("La fecha «desde» no puede ser posterior a «hasta».")
    visible, params = filtro_visibilidad(usuario)
    condiciones = [visible]
    if f.unidad_negocio_id is not None:
        condiciones.append("v.unidad_negocio_id = %s")
        params.append(f.unidad_negocio_id)
    # Siempre de la más reciente a la más antigua: primero las de hoy (la recién creada arriba), luego las atrasadas.
    orden = "v.fecha_llegada DESC, v.id DESC"
    if f.sin_filtros:
        condiciones.append("(v.fecha_llegada = core.hoy() OR (v.fecha_llegada < core.hoy() AND v.cerrada_at IS NULL))")
    else:
        if f.comprador_id is not None:
            condiciones.append("v.comprador_id = %s")
            params.append(f.comprador_id)
        if f.proveedor_cuenta:
            condiciones.append("v.proveedor_cuenta = %s")
            params.append(f.proveedor_cuenta)
        if f.desde:
            condiciones.append("v.fecha_llegada >= %s")
            params.append(f.desde)
        if f.hasta:
            condiciones.append("v.fecha_llegada <= %s")
            params.append(f.hasta)
        if f.estados:
            condiciones.append("v.estado = ANY(%s)")
            params.append(list(f.estados))
    where = " AND ".join(condiciones)
    total = con.execute(f"SELECT count(*) AS n FROM preboletas.v_preboletas v WHERE {where}", params).fetchone()
    filas = con.execute(f"{_SELECT_LISTA} WHERE {where} ORDER BY {orden} LIMIT %s", [*params, f.limite]).fetchall()
    elementos = [{**dict(r), "folio": folio(r["id"])} for r in filas]
    return {
        "elementos": elementos,
        "total": int(total["n"]) if total else 0,
        "limite": f.limite,
        "sin_filtros": f.sin_filtros,
        "atrasadas": sum(1 for e in elementos if e["atrasada"]),
    }


# ----------------------------------------------------------------------
# Detalle
# ----------------------------------------------------------------------
def detalle(con: Conexion, cfg: Configuracion, usuario: Usuario, preboleta_id: int) -> dict[str, Any]:
    cargar_visible(con, usuario, preboleta_id)
    pb = con.execute(
        """
        SELECT v.*, u.nombre AS unidad, COALESCE(c.nombre, c.correo) AS comprador, tc.nombre AS tipo_caja,
               (v.fecha_llegada < core.hoy() AND v.cerrada_at IS NULL) AS atrasada,
               pd.tipo_pago_id AS tipo_pago_dia_id, tpd.nombre AS tipo_pago_dia,
               COALESCE(cp.nombre, cp.correo) AS creado_por, COALESCE(cc.nombre, cc.correo) AS cerrada_por,
               up.nombre AS proveedor_unidad, tpe.clave AS tipo_pago_clave
          FROM preboletas.v_preboletas v
          JOIN core.unidades_negocio u ON u.id = v.unidad_negocio_id
          JOIN core.colaboradores c ON c.id = v.comprador_id
          JOIN preboletas.tipos_caja tc ON tc.id = v.tipo_caja_id
          LEFT JOIN preboletas.precios_diarios pd ON pd.unidad_negocio_id = v.unidad_negocio_id
               AND pd.producto_codigo = v.producto_codigo AND pd.fecha = v.fecha_llegada
          LEFT JOIN preboletas.tipos_pago tpd ON tpd.id = pd.tipo_pago_id
          LEFT JOIN core.colaboradores cp ON cp.id = v.creado_por_id
          LEFT JOIN core.colaboradores cc ON cc.id = v.cerrada_por_id
          LEFT JOIN core.unidades_negocio up ON up.id = v.proveedor_unidad_id
          LEFT JOIN preboletas.tipos_pago tpe ON tpe.id = v.tipo_pago_id_efectivo
         WHERE v.id = %s
        """,
        (preboleta_id,),
    ).fetchone()
    assert pb is not None
    datos = {**dict(pb), "folio": folio(preboleta_id), "qr_url": cfg.url_qr(str(pb["qr_token"]))}

    recepciones = con.execute(
        """
        SELECT vr.*, COALESCE(c.nombre, c.correo) AS basculista,
               EXISTS (SELECT 1 FROM preboletas.slots_bascula s WHERE s.recepcion_id = vr.recepcion_id) AS en_bascula,
               EXISTS (SELECT 1 FROM preboletas.propuestas_precio pp
                        WHERE pp.recepcion_id = vr.recepcion_id AND pp.estado = 'pendiente') AS propuesta_pendiente,
               EXISTS (SELECT 1 FROM preboletas.incidencias i
                        WHERE i.recepcion_id = vr.recepcion_id AND i.estado = 'pendiente'
                          AND i.tipo IN ('proveedor_distinto', 'cambio_proveedor')) AS por_conciliar
          FROM preboletas.v_recepciones vr
          JOIN core.colaboradores c ON c.id = vr.basculista_id
         WHERE vr.preboleta_id = %s
         ORDER BY vr.ingreso_at, vr.recepcion_id
        """,
        (preboleta_id,),
    ).fetchall()
    incidencias = con.execute(
        """
        SELECT i.id, i.tipo, i.estado, i.recepcion_id, i.boleta_id, b.folio AS boleta_folio,
               i.cajas_acumuladas, i.cajas_limite, i.cajas_diferencia, i.proveedor_esperado, i.proveedor_boleta,
               i.comentario, i.creado_at, i.resuelto_at, COALESCE(c.nombre, c.correo) AS resuelto_por
          FROM preboletas.incidencias i
          LEFT JOIN core.boletas b ON b.id = i.boleta_id
          LEFT JOIN core.colaboradores c ON c.id = i.resuelto_por_id
         WHERE i.preboleta_id = %s
         ORDER BY i.creado_at DESC, i.id DESC
        """,
        (preboleta_id,),
    ).fetchall()
    props = con.execute(
        _SELECT_PROPUESTAS + " WHERE pp.preboleta_id = %s ORDER BY pp.propuesto_at DESC, pp.id DESC", (preboleta_id,)
    ).fetchall()
    movs = con.execute(
        """
        SELECT m.id, m.ocurrido_at, m.tipo, tm.nombre AS tipo_nombre, tm.badge, tm.color, m.descripcion,
               m.colaborador_id, COALESCE(c.nombre, c.correo, 'Sistema') AS quien, b.folio AS boleta_folio
          FROM preboletas.movimientos m
          JOIN preboletas.tipos_movimiento tm ON tm.clave = m.tipo
          LEFT JOIN core.colaboradores c ON c.id = m.colaborador_id
          LEFT JOIN core.boletas b ON b.id = m.boleta_id
         WHERE m.preboleta_id = %s
         ORDER BY m.ocurrido_at DESC, m.id DESC
         LIMIT 30
        """,
        (preboleta_id,),
    ).fetchall()
    slot = con.execute(
        """
        SELECT s.recepcion_id, r.preboleta_id, p.comprador_id
          FROM preboletas.slots_bascula s
          LEFT JOIN preboletas.recepciones r ON r.id = s.recepcion_id
          LEFT JOIN preboletas.preboletas p ON p.id = r.preboleta_id
         WHERE s.unidad_negocio_id = %s
        """,
        (pb["unidad_negocio_id"],),
    ).fetchone()
    ocupada = bool(slot and slot["recepcion_id"])
    otra_visible = bool(
        ocupada and slot is not None and usuario.puede_ver_preboleta(slot["comprador_id"], pb["unidad_negocio_id"])
    )
    abierta = pb["cerrada_at"] is None
    dueno = usuario.es_dueno(pb["comprador_id"])
    return {
        "preboleta": datos,
        "recepciones": [dict(r) for r in recepciones],
        "incidencias": [dict(i) for i in incidencias],
        "propuestas": [_propuesta(p) for p in props],
        "movimientos": [dict(m) for m in movs],
        "bascula": {
            "ocupada": ocupada,
            "recepcion_id": slot["recepcion_id"] if otra_visible and slot else None,
            "preboleta_id": slot["preboleta_id"] if otra_visible and slot else None,
            "folio": folio(slot["preboleta_id"]) if otra_visible and slot and slot["preboleta_id"] else None,
            "es_esta_preboleta": bool(ocupada and slot and slot["preboleta_id"] == preboleta_id),
        },
        "acciones": {
            "proponer_precio": dueno and abierta,
            "proponer_precio_carga": dueno,
            "multiples_cargas": dueno and abierta,
            "cerrar_a_mano": dueno and abierta,
            "cambiar_comprador": usuario.es_admin,
            "operar_bascula": usuario.puede_operar_bascula(pb["unidad_negocio_id"]),
        },
    }


_SELECT_PROPUESTAS = """
    SELECT pp.*, COALESCE(cp.nombre, cp.correo) AS propuesto_por, COALESCE(cr.nombre, cr.correo) AS resuelto_por,
           tpp.nombre AS tipo_pago_propuesto, tpd.nombre AS tipo_pago_dia, tpa.nombre AS tipo_pago_actual,
           tpd.clave AS tipo_pago_dia_clave, b.folio AS boleta_folio,
           p.proveedor_nombre, p.comprador_id, u.nombre AS unidad
      FROM preboletas.propuestas_precio pp
      JOIN preboletas.preboletas p ON p.id = pp.preboleta_id
      JOIN core.unidades_negocio u ON u.id = pp.unidad_negocio_id
      JOIN core.colaboradores cp ON cp.id = pp.propuesto_por_id
      LEFT JOIN core.colaboradores cr ON cr.id = pp.resuelto_por_id
      LEFT JOIN preboletas.tipos_pago tpp ON tpp.id = pp.tipo_pago_propuesto_id
      LEFT JOIN preboletas.tipos_pago tpd ON tpd.id = pp.tipo_pago_dia_id
      LEFT JOIN preboletas.tipos_pago tpa ON tpa.id = pp.tipo_pago_actual_id
      LEFT JOIN preboletas.recepciones r ON r.id = pp.recepcion_id
      LEFT JOIN core.boletas b ON b.id = r.boleta_id
"""
SELECT_PROPUESTAS = _SELECT_PROPUESTAS


def _propuesta(p: Any) -> dict[str, Any]:
    d = dict(p)
    d["folio"] = folio(d["preboleta_id"])
    # En Precio Pactado el precio del día es el tope: superarlo se marca (no se bloquea).
    d["supera_tope"] = bool(
        d["tipo_pago_dia_clave"] == "pactado"
        and d["precio_propuesto_kg"] is not None
        and d["precio_dia_kg"] is not None
        and d["precio_propuesto_kg"] > d["precio_dia_kg"]
    )
    return d


propuesta_a_dict = _propuesta


# ----------------------------------------------------------------------
# Alta y cambios
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class DatosNueva:
    unidad_negocio_id: int
    tipo_caja_id: int
    tipo_negociacion: str  # kilos | cajas
    cantidad: Decimal
    fecha_llegada: date | None
    comprador_id: int | None
    multiples_cargas: bool
    precio_kg: Decimal | None
    tipo_pago_id: int | None
    motivo: str | None


def es_comprador_valido(con: Conexion, colaborador_id: int) -> bool:
    """Puede ser comprador de una preboleta quien tenga el rol Comprador o Admin en Preboletas."""
    fila = con.execute(
        """
        SELECT 1 FROM core.colaborador_acceso ca JOIN core.apps a ON a.id = ca.app_id AND a.clave = %s
         WHERE ca.colaborador_id = %s AND ca.rol IN (%s, %s) LIMIT 1
        """,
        (CLAVE_APP, colaborador_id, ROL_COMPRADOR, ROL_ADMIN),
    ).fetchone()
    return fila is not None


def calcular_cajas(tipo_negociacion: str, cantidad: Decimal, kg_por_caja: Decimal) -> int:
    """
    Cajas negociadas, o equivalentes si fue en kilos: ROUND(kg / kg_por_caja), redondeando
    las mitades hacia arriba igual que round() de PostgreSQL (la base lo revisa con un CHECK).
    """
    if tipo_negociacion == "kilos":
        return int((cantidad / kg_por_caja).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return int(cantidad)


def crear(op: Operacion, usuario: Usuario, d: DatosNueva, proveedor: Proveedor, fruta: Fruta) -> tuple[int, int | None]:
    """Devuelve (id de la preboleta, id de la propuesta de precio si se creó)."""
    con = op.con
    if not (usuario.es_comprador or usuario.es_admin):
        raise AccesoDenegado("Solo compradores y Admin crean preboletas.")
    if con.execute("SELECT 1 FROM core.unidades_negocio WHERE id = %s", (d.unidad_negocio_id,)).fetchone() is None:
        raise SolicitudInvalida("Elige la unidad.")
    caja = con.execute(
        """
        SELECT tc.id, tc.nombre, tc.kg_por_caja, tc.activo, tcp.producto_codigo IS NOT NULL AS permitida
          FROM preboletas.tipos_caja tc
          LEFT JOIN preboletas.tipos_caja_productos tcp ON tcp.tipo_caja_id = tc.id AND tcp.producto_codigo = %s
         WHERE tc.id = %s
        """,
        (fruta.codigo, d.tipo_caja_id),
    ).fetchone()
    if caja is None or not caja["activo"]:
        raise SolicitudInvalida("Elige una caja activa.")
    if not caja["permitida"]:
        raise SolicitudInvalida(
            f"La {str(caja['nombre']).lower()} no está permitida para {fruta.nombre}.", codigo="caja_no_permitida"
        )
    tol = con.execute(
        "SELECT tolerancia_pct FROM preboletas.tolerancias WHERE unidad_negocio_id = %s AND producto_codigo = %s",
        (d.unidad_negocio_id, fruta.codigo),
    ).fetchone()
    if tol is None:
        raise SolicitudInvalida(
            f"No hay tolerancia global de {fruta.nombre} en {nombre_unidad(con, d.unidad_negocio_id)}. "
            "Pídela al Admin.",
            codigo="sin_tolerancia",
        )
    if d.cantidad <= 0:
        raise SolicitudInvalida("Captura una cantidad mayor a 0.")
    if d.tipo_negociacion == "cajas" and d.cantidad != d.cantidad.to_integral_value():
        raise SolicitudInvalida("Las cajas se capturan en número entero.")
    cajas = calcular_cajas(d.tipo_negociacion, d.cantidad, caja["kg_por_caja"])
    if cajas <= 0:
        raise SolicitudInvalida("La cantidad no alcanza ni una caja.")

    comprador = usuario.colaborador_id
    if d.comprador_id is not None and d.comprador_id != usuario.colaborador_id:
        if not usuario.es_admin:
            raise AccesoDenegado("Solo un Admin crea preboletas para otro comprador.")
        if not es_comprador_valido(con, d.comprador_id):
            raise SolicitudInvalida("Elige un comprador.")
        comprador = d.comprador_id
    # El sitio que trae el proveedor en D365 solo se guarda si es una unidad conocida.
    sitio = proveedor.sitio
    if (
        sitio is not None
        and con.execute("SELECT 1 FROM core.unidades_negocio WHERE id = %s", (sitio,)).fetchone() is None
    ):
        sitio = None

    fila = con.execute(
        """
        INSERT INTO preboletas.preboletas
            (unidad_negocio_id, proveedor_cuenta, proveedor_nombre, proveedor_unidad_id, comprador_id, creado_por_id,
             fecha_llegada, tipo_negociacion, tipo_caja_id, kg_por_caja, kg_negociados, cajas_negociadas,
             tolerancia_pct, multiples_cargas, producto_codigo, producto_nombre)
        VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, core.hoy()), %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            d.unidad_negocio_id,
            proveedor.cuenta,
            proveedor.nombre,
            sitio,
            comprador,
            usuario.colaborador_id,
            d.fecha_llegada,
            d.tipo_negociacion,
            caja["id"],
            caja["kg_por_caja"],
            d.cantidad if d.tipo_negociacion == "kilos" else None,
            cajas,
            tol["tolerancia_pct"],
            d.multiples_cargas,
            fruta.codigo,
            fruta.nombre,
        ),
    ).fetchone()
    assert fila is not None
    pb = dict(fila)
    f = folio(pb["id"])
    op.mov(
        "preboleta_creada",
        pb["unidad_negocio_id"],
        f"{f} para {proveedor.nombre}: {cajas} cajas ({pb['cajas_minimas']}–{pb['cajas_maximas']}), "
        f"tolerancia {pb['tolerancia_pct']:g}%",
        preboleta_id=pb["id"],
    )
    if sitio is not None and sitio != pb["unidad_negocio_id"]:
        op.mov(
            "unidad_preboleta",
            pb["unidad_negocio_id"],
            f"{f}: el proveedor trae {nombre_unidad(con, sitio)} en D365 y se eligió "
            f"{nombre_unidad(con, pb['unidad_negocio_id'])}",
            preboleta_id=pb["id"],
        )
    cantidad.evento_preboleta(op, pb, estado="pendiente")
    prop = propuestas.precio_al_crear(op, usuario, pb, d.precio_kg, d.tipo_pago_id, d.motivo)
    return int(pb["id"]), prop


def cambiar_multiples(op: Operacion, usuario: Usuario, preboleta_id: int, activar: bool) -> None:
    pb = cargar_visible(op.con, usuario, preboleta_id, bloquear=True)
    if not usuario.es_dueno(pb["comprador_id"]):
        raise AccesoDenegado("Solo su comprador o un Admin.")
    if pb["cerrada_at"] is not None:
        raise Conflicto("La preboleta está cerrada.", codigo="preboleta_cerrada")
    if pb["multiples_cargas"] == activar:
        return
    op.con.execute("UPDATE preboletas.preboletas SET multiples_cargas = %s WHERE id = %s", (activar, pb["id"]))
    pb["multiples_cargas"] = activar
    op.mov(
        "multiples_cargas",
        pb["unidad_negocio_id"],
        f"{folio(pb['id'])}: múltiples cargas {'activadas' if activar else 'desactivadas'}",
        preboleta_id=pb["id"],
    )
    cantidad.reevaluar(op, pb)
    cantidad.evento_preboleta(op, pb)


def cambiar_comprador(op: Operacion, usuario: Usuario, preboleta_id: int, nuevo: int) -> None:
    if not usuario.es_admin:
        raise AccesoDenegado("Solo un Admin cambia el comprador.")
    pb = cargar_preboleta(op.con, preboleta_id, bloquear=True)
    if not es_comprador_valido(op.con, nuevo):
        raise SolicitudInvalida("Elige un comprador.")
    antes = int(pb["comprador_id"])
    if antes == nuevo:
        return
    op.con.execute("UPDATE preboletas.preboletas SET comprador_id = %s WHERE id = %s", (nuevo, pb["id"]))
    f = folio(pb["id"])
    nuevo_nombre = nombre_colaborador(op.con, nuevo)
    op.mov(
        "comprador_preboleta",
        pb["unidad_negocio_id"],
        f"{f}: comprador {nombre_colaborador(op.con, antes)} → {nuevo_nombre}",
        preboleta_id=pb["id"],
    )
    op.notificar([antes, nuevo], f"{f} cambió de comprador: ahora es de {nuevo_nombre}.", preboleta_id=pb["id"])
    # Se avisa a los dos compradores: el anterior deja de verla y el nuevo empieza a verla.
    cantidad.evento_preboleta(op, pb)
    pb["comprador_id"] = nuevo
    cantidad.evento_preboleta(op, pb)


def publico(con: Conexion, token: str) -> dict[str, Any] | None:
    """Lo que ve el proveedor con el QR: sin cajas, kilos, precios ni comprador."""
    fila = con.execute(
        """
        SELECT p.id, p.proveedor_nombre, p.producto_nombre, p.fecha_llegada, p.cerrada_at IS NOT NULL AS cerrada,
               u.nombre AS unidad
          FROM preboletas.preboletas p JOIN core.unidades_negocio u ON u.id = p.unidad_negocio_id
         WHERE p.qr_token = %s
        """,
        (token,),
    ).fetchone()
    if fila is None:
        return None
    d = dict(fila)
    d["folio"] = folio(d.pop("id"))
    return d


def por_qr(con: Conexion, usuario: Usuario, token: str) -> int:
    """Para la app móvil: el basculista lee el QR y abre la preboleta de SU unidad."""
    fila = con.execute(
        "SELECT id, unidad_negocio_id FROM preboletas.preboletas WHERE qr_token = %s", (token,)
    ).fetchone()
    if fila is None:
        raise NoEncontrado("Ese QR no corresponde a ninguna preboleta.")
    if not usuario.puede_operar_bascula(fila["unidad_negocio_id"]):
        raise Conflicto(
            f"El QR es de una preboleta de {nombre_unidad(con, fila['unidad_negocio_id'])}.", codigo="unidad_distinta"
        )
    return int(fila["id"])
