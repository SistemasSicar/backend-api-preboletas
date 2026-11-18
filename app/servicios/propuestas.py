"""
Propuestas de precio y tipo de pago distintos al precio del día.

* Las hace el comprador de la preboleta (o un Admin), para la preboleta o para una carga (boleta).
* Se compara contra el precio del día (unidad + fruta + fecha de llegada). Lo que queda IGUAL al
  del día se aplica sin autorización; lo distinto queda pendiente y mientras tanto rige el vigente.
* Autoriza quien tenga `autorizar_precios` en la unidad, o un Admin. Nadie autoriza la suya,
  salvo un Admin.
* En Precio Pactado el precio del día es un TOPE: superarlo se marca, pero no se bloquea.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.bd import Conexion
from app.errores import AccesoDenegado, Conflicto, NoEncontrado, SolicitudInvalida
from app.servicios import cantidad
from app.servicios.comun import (
    Operacion,
    admins,
    autorizadores,
    cargar_preboleta,
    dinero,
    folio,
    nombre_tipo_pago,
    precio_dia,
    precio_txt,
    tipo_pago_id,
)
from app.usuario import PERMISO_AUTORIZAR, Usuario


def _destino(con: Conexion, pb: dict[str, Any], rec: dict[str, Any] | None) -> str:
    if rec is None:
        return folio(pb["id"])
    if rec["boleta_id"]:
        fila = con.execute("SELECT folio FROM core.boletas WHERE id = %s", (rec["boleta_id"],)).fetchone()
        boleta = str(fila["folio"]) if fila else f"#{rec['boleta_id']}"
    else:
        boleta = f"(carga #{rec['id']})"
    return f"la boleta {boleta} de {folio(pb['id'])}"


def _efectivo(con: Conexion, pb_id: int, rec_id: int | None) -> tuple[Decimal | None, int | None]:
    """Precio y tipo vigentes hoy (carga > preboleta > día), tal como los calculan las vistas."""
    if rec_id is not None:
        fila = con.execute(
            "SELECT precio_kg_efectivo AS p, tipo_pago_id_efectivo AS t FROM preboletas.v_recepciones "
            "WHERE recepcion_id = %s",
            (rec_id,),
        ).fetchone()
    else:
        fila = con.execute(
            "SELECT precio_kg_efectivo AS p, tipo_pago_id_efectivo AS t FROM preboletas.v_preboletas WHERE id = %s",
            (pb_id,),
        ).fetchone()
    if fila is None:
        return None, None
    return fila["p"], fila["t"]


def proponer(
    op: Operacion,
    usuario: Usuario,
    preboleta_id: int,
    *,
    recepcion_id: int | None = None,
    precio: Decimal | None,
    tipo: int | None,
    motivo: str | None,
) -> int | None:
    """Crea la propuesta y devuelve su id, o None si quedó igual al precio del día (sin autorización)."""
    pb = cargar_preboleta(op.con, preboleta_id, bloquear=True)
    if not usuario.es_dueno(pb["comprador_id"]):
        raise AccesoDenegado("Propone el comprador de la preboleta o un Admin.")
    rec: dict[str, Any] | None = None
    if recepcion_id is not None:
        fila = op.con.execute(
            "SELECT * FROM preboletas.recepciones WHERE id = %s AND preboleta_id = %s FOR UPDATE",
            (recepcion_id, preboleta_id),
        ).fetchone()
        if fila is None:
            raise NoEncontrado("La carga no existe.")
        rec = dict(fila)
        if rec["cancelada_at"] is not None:
            raise SolicitudInvalida("La carga está cancelada.")
    elif pb["cerrada_at"] is not None:
        raise Conflicto("La preboleta está cerrada.", codigo="preboleta_cerrada")
    if precio is None and tipo is None:
        raise SolicitudInvalida("Indica un precio o un tipo de pago.")
    if precio is not None and precio <= 0:
        raise SolicitudInvalida("El precio debe ser mayor a 0.")
    if tipo is not None:
        activo = op.con.execute("SELECT activo FROM preboletas.tipos_pago WHERE id = %s", (tipo,)).fetchone()
        if activo is None or not activo["activo"]:
            raise SolicitudInvalida("Elige un tipo de pago activo.")

    dia = precio_dia(op.con, pb["unidad_negocio_id"], pb["producto_codigo"], pb["fecha_llegada"])
    dia_precio: Decimal | None = dia["precio_kg"] if dia else None
    dia_tipo: int | None = dia["tipo_pago_id"] if dia else None
    actual_precio, actual_tipo = _efectivo(op.con, pb["id"], recepcion_id)
    distinto_precio = precio is not None and (dia is None or precio != dia_precio)
    distinto_tipo = tipo is not None and (dia is None or tipo != dia_tipo)

    # Solo una propuesta pendiente por destino: la nueva reemplaza a la anterior.
    previa = op.con.execute(
        "SELECT id FROM preboletas.propuestas_precio WHERE preboleta_id = %s AND estado = 'pendiente' "
        "AND recepcion_id IS NOT DISTINCT FROM %s FOR UPDATE",
        (pb["id"], recepcion_id),
    ).fetchone()
    if previa is not None:
        op.con.execute(
            "UPDATE preboletas.propuestas_precio SET estado = 'cancelada', resuelto_at = now() WHERE id = %s",
            (previa["id"],),
        )
        op.mov(
            "propuesta_cancelada",
            pb["unidad_negocio_id"],
            f"Propuesta #{previa['id']} reemplazada por una nueva",
            preboleta_id=pb["id"],
            recepcion_id=recepcion_id,
            propuesta_id=previa["id"],
        )

    destino = _destino(op.con, pb, rec)
    if not distinto_precio and not distinto_tipo:
        # Igual al precio del día: se aplica sin autorización y vuelve a seguir al precio del día
        # (si después cambia, la preboleta o la carga lo siguen).
        if rec is not None:
            op.con.execute(
                "UPDATE preboletas.recepciones SET precio_ajustado_kg = NULL, precio_ajustado_por_id = NULL, "
                "tipo_pago_id = NULL WHERE id = %s",
                (rec["id"],),
            )
        else:
            op.con.execute(
                "UPDATE preboletas.preboletas SET precio_pactado_kg = NULL, tipo_pago_id = NULL WHERE id = %s",
                (pb["id"],),
            )
        op.mov(
            "precio_boleta" if rec else "precio_preboleta",
            pb["unidad_negocio_id"],
            f"{destino}: vuelve al precio del día ({dinero(dia_precio)} {nombre_tipo_pago(op.con, dia_tipo)}), "
            "sin autorización",
            preboleta_id=pb["id"],
            recepcion_id=recepcion_id,
        )
        cantidad.evento_preboleta(op, pb)
        return None

    fila = op.con.execute(
        """
        INSERT INTO preboletas.propuestas_precio
            (unidad_negocio_id, preboleta_id, recepcion_id, precio_propuesto_kg, tipo_pago_propuesto_id,
             precio_dia_kg, tipo_pago_dia_id, precio_actual_kg, tipo_pago_actual_id, motivo, propuesto_por_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            pb["unidad_negocio_id"],
            pb["id"],
            recepcion_id,
            precio if distinto_precio else None,
            tipo if distinto_tipo else None,
            dia_precio,
            dia_tipo,
            actual_precio,
            actual_tipo,
            (motivo or "").strip() or None,
            usuario.colaborador_id,
        ),
    ).fetchone()
    assert fila is not None
    prop_id = int(fila["id"])
    p_txt = dinero(precio) if distinto_precio else "mismo precio"
    t_txt = f", {nombre_tipo_pago(op.con, tipo)}" if distinto_tipo else ""
    dia_txt = (
        f"{nombre_tipo_pago(op.con, dia_tipo)} {precio_txt(op.con, dia_precio, dia_tipo)}" if dia else "sin asignar"
    )
    op.mov(
        "propuesta_precio",
        pb["unidad_negocio_id"],
        f"Propuesta #{prop_id} para {destino}: {p_txt}{t_txt} (del día: {dia_txt})",
        preboleta_id=pb["id"],
        recepcion_id=recepcion_id,
        propuesta_id=prop_id,
    )
    op.evento(
        "propuesta.creada",
        propuesta_id=prop_id,
        preboleta_id=pb["id"],
        unidad_negocio_id=pb["unidad_negocio_id"],
        comprador_id=pb["comprador_id"],
    )
    aviso_precio = f"{dinero(precio)}/kg" if distinto_precio else ""
    aviso_tipo = f" {nombre_tipo_pago(op.con, tipo)}" if distinto_tipo else ""
    op.notificar(
        [*autorizadores(op.con, pb["unidad_negocio_id"]), *admins(op.con)],
        f"{usuario.nombre} propone {aviso_precio}{aviso_tipo} para {destino}. Requiere autorización.".replace(
            "  ", " "
        ),
        preboleta_id=pb["id"],
        propuesta_id=prop_id,
        excluir_a_quien_actua=True,
    )
    return prop_id


def resolver(op: Operacion, usuario: Usuario, propuesta_id: int, decision: str, comentario: str | None) -> None:
    pr = op.con.execute(
        "SELECT * FROM preboletas.propuestas_precio WHERE id = %s FOR UPDATE", (propuesta_id,)
    ).fetchone()
    if pr is None:
        raise NoEncontrado("La propuesta no existe.")
    if not usuario.tiene_permiso(PERMISO_AUTORIZAR, pr["unidad_negocio_id"]):
        raise AccesoDenegado("No tienes el permiso autorizar_precios en esta unidad.", codigo="sin_permiso")
    if pr["estado"] != "pendiente":
        raise Conflicto("Esa propuesta ya está resuelta.", codigo="propuesta_resuelta")
    if pr["propuesto_por_id"] == usuario.colaborador_id and not usuario.es_admin:
        raise AccesoDenegado(
            "No puedes autorizar tu propia propuesta (solo un Admin puede).", codigo="autoautorizacion"
        )
    comentario = (comentario or "").strip() or None
    op.con.execute(
        "UPDATE preboletas.propuestas_precio SET estado = %s, resuelto_por_id = %s, resuelto_at = now(), "
        "comentario = %s WHERE id = %s",
        (decision, usuario.colaborador_id, comentario, propuesta_id),
    )
    pb = cargar_preboleta(op.con, pr["preboleta_id"], bloquear=True)
    if decision == "autorizada":
        if pr["recepcion_id"] is not None:
            if pr["precio_propuesto_kg"] is not None:
                op.con.execute(
                    "UPDATE preboletas.recepciones SET precio_ajustado_kg = %s, precio_ajustado_por_id = %s "
                    "WHERE id = %s",
                    (pr["precio_propuesto_kg"], usuario.colaborador_id, pr["recepcion_id"]),
                )
            if pr["tipo_pago_propuesto_id"] is not None:
                op.con.execute(
                    "UPDATE preboletas.recepciones SET tipo_pago_id = %s WHERE id = %s",
                    (pr["tipo_pago_propuesto_id"], pr["recepcion_id"]),
                )
        else:
            if pr["precio_propuesto_kg"] is not None:
                op.con.execute(
                    "UPDATE preboletas.preboletas SET precio_pactado_kg = %s WHERE id = %s",
                    (pr["precio_propuesto_kg"], pb["id"]),
                )
            if pr["tipo_pago_propuesto_id"] is not None:
                op.con.execute(
                    "UPDATE preboletas.preboletas SET tipo_pago_id = %s WHERE id = %s",
                    (pr["tipo_pago_propuesto_id"], pb["id"]),
                )
    op.mov(
        "propuesta_autorizada" if decision == "autorizada" else "propuesta_rechazada",
        pb["unidad_negocio_id"],
        f"Propuesta #{propuesta_id} {decision} por {usuario.nombre}{f': {comentario}' if comentario else ''}",
        preboleta_id=pb["id"],
        recepcion_id=pr["recepcion_id"],
        propuesta_id=propuesta_id,
    )
    op.evento(
        "propuesta.resuelta",
        propuesta_id=propuesta_id,
        estado=decision,
        preboleta_id=pb["id"],
        unidad_negocio_id=pb["unidad_negocio_id"],
        comprador_id=pb["comprador_id"],
    )
    op.notificar(
        [pr["propuesto_por_id"]],
        f"Tu propuesta para {folio(pb['id'])} fue {decision} por {usuario.nombre}.",
        preboleta_id=pb["id"],
        propuesta_id=propuesta_id,
    )


def precio_al_crear(
    op: Operacion, usuario: Usuario, pb: dict[str, Any], precio: Decimal | None, tipo: int | None, motivo: str | None
) -> int | None:
    """
    Precio capturado al crear la preboleta (vacío o 0 = sin precio propio):
      * Sin precio del día y con precio: nace como Precio Pactado y pide autorización.
      * Sin precio del día y sin precio: tomará el tipo y precio del día en cuanto se definan.
      * Con precio del día y algo distinto: propuesta.
    """
    if precio is not None and precio <= 0:
        precio = None
    dia = precio_dia(op.con, pb["unidad_negocio_id"], pb["producto_codigo"], pb["fecha_llegada"])
    if dia is None:
        if precio is not None:
            return proponer(
                op,
                usuario,
                pb["id"],
                precio=precio,
                tipo=tipo_pago_id(op.con, "pactado"),
                motivo=motivo or "Sin precio del día: precio pactado",
            )
        op.mov(
            "precio_preboleta",
            pb["unidad_negocio_id"],
            f"{folio(pb['id'])} creada sin precio: tomará el precio del día ({pb['producto_nombre']}, "
            f"{pb['fecha_llegada'].isoformat()}) cuando se defina",
            preboleta_id=pb["id"],
        )
        return None
    if (precio is not None and precio != dia["precio_kg"]) or (tipo is not None and tipo != dia["tipo_pago_id"]):
        return proponer(
            op,
            usuario,
            pb["id"],
            precio=precio if precio is not None else dia["precio_kg"],
            tipo=tipo if tipo is not None else dia["tipo_pago_id"],
            motivo=motivo or "Al crear la preboleta",
        )
    return None
