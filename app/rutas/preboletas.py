"""Preboletas, báscula (recepciones) y propuestas de precio por preboleta o por carga."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Query, status

from app.dependencias import ServiciosDep, UsuarioActual
from app.errores import RESPUESTAS_COMUNES, AccesoDenegado, NoEncontrado, RespuestaError
from app.esquemas.modelos import (
    Bascula,
    BoletaActualizada,
    CambioComprador,
    CambioMultiples,
    CierreManual,
    CorreccionCajas,
    Creado,
    DetallePreboleta,
    ListaPreboletas,
    Motivo,
    NuevaEntrada,
    NuevaPreboleta,
    NuevaPropuesta,
    Ok,
    PreboletaCreada,
    PreboletaPorQr,
    PropuestaCreada,
)
from app.motor.motor_boletas import procesar_boleta
from app.rutas.comun import en_transaccion, fruta_valida, proveedor_valido
from app.servicios import cantidad, preboletas, propuestas, recepciones
from app.servicios.comun import Operacion, folio
from app.usuario import Usuario

router = APIRouter(tags=["Preboletas y báscula"], responses=RESPUESTAS_COMUNES)
_409: dict[int | str, dict[str, Any]] = {
    409: {"model": RespuestaError, "description": "El estado actual no lo permite (código explica por qué)."}
}


@router.get("/preboletas", response_model=ListaPreboletas, summary="Preboletas visibles para tu rol")
def listar(
    usuario: UsuarioActual,
    servicios: ServiciosDep,
    unidad_negocio_id: int | None = None,
    comprador_id: int | None = None,
    proveedor_cuenta: str | None = None,
    desde: date | None = None,
    hasta: date | None = None,
    estados: list[str] = Query(default=[]),
    limite: int = Query(default=100, ge=1, le=500),
) -> ListaPreboletas:
    """
    Sin filtros (aparte de la unidad): las programadas para hoy más las atrasadas de días anteriores.
    Con filtros: las más recientes que los cumplan, hasta `limite`.
    Admin ve todas; Comprador, las suyas; Basculista, las de su unidad.
    """
    f = preboletas.Filtros(
        unidad_negocio_id=unidad_negocio_id,
        comprador_id=comprador_id,
        proveedor_cuenta=proveedor_cuenta,
        desde=desde,
        hasta=hasta,
        estados=tuple(estados),
        limite=limite,
    )
    with servicios.bd.transaccion() as con:
        return ListaPreboletas.model_validate(preboletas.listar(con, usuario, f))


@router.post(
    "/preboletas",
    response_model=PreboletaCreada,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una preboleta (Comprador o Admin)",
)
def crear(datos: NuevaPreboleta, usuario: UsuarioActual, servicios: ServiciosDep) -> PreboletaCreada:
    """
    Valida en Dataverse el proveedor y la fruta (si Dataverse no responde: 503 y no se guarda),
    la caja permitida para la fruta y que exista tolerancia en la unidad. Si el precio o el tipo de
    pago difieren del precio del día, crea una propuesta que requiere autorización.
    """
    if not (usuario.es_comprador or usuario.es_admin):
        raise AccesoDenegado("Solo compradores y Admin crean preboletas.")
    proveedor = proveedor_valido(servicios, datos.proveedor_cuenta)
    fruta = fruta_valida(servicios, datos.producto_codigo)
    nueva = preboletas.DatosNueva(
        unidad_negocio_id=datos.unidad_negocio_id,
        tipo_caja_id=datos.tipo_caja_id,
        tipo_negociacion=datos.tipo_negociacion,
        cantidad=datos.cantidad,
        fecha_llegada=datos.fecha_llegada,
        comprador_id=datos.comprador_id,
        multiples_cargas=datos.multiples_cargas,
        precio_kg=datos.precio_kg,
        tipo_pago_id=datos.tipo_pago_id,
        motivo=datos.motivo,
    )
    pb_id, prop_id = en_transaccion(
        servicios, usuario, lambda op: preboletas.crear(op, usuario, nueva, proveedor, fruta)
    )
    return PreboletaCreada(id=pb_id, folio=folio(pb_id), propuesta_id=prop_id)


@router.get("/preboletas/qr/{token}", response_model=PreboletaPorQr, summary="Abrir una preboleta por su QR (báscula)")
def por_qr(token: str, usuario: UsuarioActual, servicios: ServiciosDep) -> PreboletaPorQr:
    """Para la app móvil: el basculista escanea el QR; debe ser de una preboleta de su unidad."""
    with servicios.bd.transaccion() as con:
        pb_id = preboletas.por_qr(con, usuario, _token(token))
    return PreboletaPorQr(preboleta_id=pb_id, folio=folio(pb_id))


@router.get("/preboletas/{preboleta_id}", response_model=DetallePreboleta, summary="Detalle de una preboleta")
def detalle(preboleta_id: int, usuario: UsuarioActual, servicios: ServiciosDep) -> DetallePreboleta:
    """Datos, cargas, incidencias, propuestas, últimos movimientos, estado de la báscula y acciones permitidas."""
    with servicios.bd.transaccion() as con:
        return DetallePreboleta.model_validate(preboletas.detalle(con, servicios.cfg, usuario, preboleta_id))


@router.patch("/preboletas/{preboleta_id}/multiples-cargas", response_model=Ok, responses=_409)
def multiples(preboleta_id: int, datos: CambioMultiples, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    """Activar o quitar 'múltiples cargas' (su comprador o un Admin)."""
    en_transaccion(
        servicios, usuario, lambda op: preboletas.cambiar_multiples(op, usuario, preboleta_id, datos.activar)
    )
    return Ok()


@router.post("/preboletas/{preboleta_id}/cierre-manual", response_model=Ok, responses=_409)
def cierre_manual(preboleta_id: int, datos: CierreManual, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    """Cerrar a mano con observación obligatoria. Queda incidencia 'cierre_manual' (y faltante si aplica)."""

    def accion(op: Operacion) -> None:
        preboletas.cargar_visible(op.con, usuario, preboleta_id)
        cantidad.cierre_manual(op, usuario, preboleta_id, datos.observacion)

    en_transaccion(servicios, usuario, accion)
    return Ok()


@router.patch("/preboletas/{preboleta_id}/comprador", response_model=Ok, summary="Cambiar el comprador (Admin)")
def comprador(preboleta_id: int, datos: CambioComprador, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    en_transaccion(
        servicios, usuario, lambda op: preboletas.cambiar_comprador(op, usuario, preboleta_id, datos.comprador_id)
    )
    return Ok()


@router.post(
    "/preboletas/{preboleta_id}/propuestas",
    response_model=PropuestaCreada,
    responses=_409,
    summary="Proponer precio o tipo de pago para la preboleta",
)
def proponer(
    preboleta_id: int, datos: NuevaPropuesta, usuario: UsuarioActual, servicios: ServiciosDep
) -> PropuestaCreada:
    """Igual al precio del día: se aplica sin autorización. Distinto: queda pendiente de autorizar."""
    prop = en_transaccion(
        servicios,
        usuario,
        lambda op: _proponer(op, usuario, preboleta_id, None, datos),
    )
    return PropuestaCreada(propuesta_id=prop)


# ----------------------------------------------------------------------
# Báscula
# ----------------------------------------------------------------------
@router.get("/basculas", response_model=list[Bascula], tags=["Preboletas y báscula"], summary="Estado de cada báscula")
def basculas(usuario: UsuarioActual, servicios: ServiciosDep) -> list[Bascula]:
    with servicios.bd.transaccion() as con:
        return [Bascula.model_validate(b) for b in recepciones.basculas(con, usuario)]


@router.post(
    "/preboletas/{preboleta_id}/recepciones",
    response_model=Creado,
    status_code=status.HTTP_201_CREATED,
    responses=_409,
    summary="Indicar peso en báscula (entrada del camión)",
)
def entrada(preboleta_id: int, datos: NuevaEntrada, usuario: UsuarioActual, servicios: ServiciosDep) -> Creado:
    """
    Basculista de la unidad o Admin. Ocupa la báscula hasta la 1ra pesada en D365 (409
    `bascula_ocupada` si ya hay un camión). Si la preboleta estaba cerrada, se reabre.
    """

    def accion(op: Operacion) -> int:
        preboletas.cargar_visible(op.con, usuario, preboleta_id)
        return recepciones.registrar_entrada(op, usuario, preboleta_id, datos.cajas, datos.via, datos.multiples_cargas)

    return Creado(id=en_transaccion(servicios, usuario, accion))


@router.post("/recepciones/{recepcion_id}/cancelar", response_model=Ok, responses=_409, summary="Cancelar la entrada")
def cancelar_entrada(recepcion_id: int, datos: Motivo, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    """Solo antes de la 1ra pesada (camión aún en báscula). Libera la báscula."""
    en_transaccion(servicios, usuario, lambda op: recepciones.cancelar_entrada(op, usuario, recepcion_id, datos.motivo))
    return Ok()


@router.post("/recepciones/{recepcion_id}/corregir-cajas", response_model=Ok, responses=_409, summary="Corregir cajas")
def corregir_cajas(recepcion_id: int, datos: CorreccionCajas, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    """Mientras la boleta no esté cerrada en D365. Se guardan las cajas originales."""
    en_transaccion(
        servicios, usuario, lambda op: recepciones.corregir_cajas(op, usuario, recepcion_id, datos.cajas, datos.motivo)
    )
    return Ok()


_MENSAJES_ACTUALIZAR = {
    "actualizada": "Boleta {folio} actualizada con los datos de D365.",
    "creada": "Boleta {folio} actualizada con los datos de D365.",
    "sin_cambios": "La boleta {folio} ya estaba al día con D365.",
    "ignorada_atrasada": "D365 trae datos más viejos que los guardados para {folio}: no se aplicaron.",
}


@router.post(
    "/recepciones/{recepcion_id}/actualizar-boleta",
    response_model=BoletaActualizada,
    summary="Actualizar la boleta de la carga desde D365",
    responses={
        400: {"model": RespuestaError, "description": "carga_sin_boleta: aún no tiene boleta ligada."},
        404: {"model": RespuestaError, "description": "La carga no existe, o la boleta no existe en D365."},
        502: {"model": RespuestaError, "description": "CoreAPI o Dataverse rechazaron la consulta (configuración)."},
    },
)
def actualizar_boleta(recepcion_id: int, usuario: UsuarioActual, servicios: ServiciosDep) -> BoletaActualizada:
    """
    Para cuando se perdió un cambio de la boleta (Power Automate, CoreAPI o esta API estuvieron caídos):
    CoreAPI la relee **en vivo** de Dataverse y la guarda con las mismas reglas que Power Automate.
    Después la boleta pasa por el motor al momento (liga, completa la 2da pesada, evalúa el cierre),
    aunque CoreAPI responda `sin_cambios`: así también se recupera un aviso que esta API no procesó.
    """
    with servicios.bd.transaccion() as con:
        carga = recepciones.boleta_de_la_carga(con, usuario, recepcion_id)
    # Fuera de la transacción: CoreAPI y Dataverse pueden tardar varios segundos.
    resultado = servicios.core.actualizar_boleta(carga["folio"])
    with servicios.bd.transaccion() as con:
        procesar_boleta(con, int(carga["boleta_id"]))
    plantilla = _MENSAJES_ACTUALIZAR.get(resultado, "Boleta {folio} consultada en D365.")
    return BoletaActualizada(folio=carga["folio"], resultado=resultado, mensaje=plantilla.format(folio=carga["folio"]))


@router.post(
    "/recepciones/{recepcion_id}/propuestas",
    response_model=PropuestaCreada,
    responses=_409,
    summary="Proponer precio o tipo de pago para una carga (boleta)",
)
def proponer_carga(
    recepcion_id: int, datos: NuevaPropuesta, usuario: UsuarioActual, servicios: ServiciosDep
) -> PropuestaCreada:
    def accion(op: Operacion) -> int | None:
        fila = op.con.execute(
            "SELECT preboleta_id FROM preboletas.recepciones WHERE id = %s", (recepcion_id,)
        ).fetchone()
        if fila is None:
            raise NoEncontrado("La carga no existe.")
        return _proponer(op, usuario, int(fila["preboleta_id"]), recepcion_id, datos)

    return PropuestaCreada(propuesta_id=en_transaccion(servicios, usuario, accion))


def _proponer(
    op: Operacion, usuario: Usuario, preboleta_id: int, recepcion_id: int | None, datos: NuevaPropuesta
) -> int | None:
    preboletas.cargar_visible(op.con, usuario, preboleta_id)
    return propuestas.proponer(
        op,
        usuario,
        preboleta_id,
        recepcion_id=recepcion_id,
        precio=datos.precio_kg,
        tipo=datos.tipo_pago_id,
        motivo=datos.motivo,
    )


def _token(texto: str) -> str:
    """Acepta el token solo o el enlace completo del QR (…/qr/<token>)."""
    return texto.strip().rstrip("/").rsplit("/", 1)[-1]
