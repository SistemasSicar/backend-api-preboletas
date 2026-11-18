"""
Modelos de entrada y salida de la API (lo que documenta Swagger y consumen el panel y la app).

Convenciones: campos en español snake_case, fechas ISO 8601 con zona, importes y kilos como
números. Los modelos de salida ignoran columnas extra de las vistas.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Texto = Annotated[str, StringConstraints(strip_whitespace=True)]
TextoObligatorio = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Precio = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=4, examples=["18.50"])]


class Ok(BaseModel):
    ok: bool = True


# ----------------------------------------------------------------------
# Sesión
# ----------------------------------------------------------------------
class PermisoUnidad(BaseModel):
    permiso: Literal["precios_diarios", "autorizar_precios"]
    unidad_negocio_id: int


class Yo(BaseModel):
    colaborador_id: int
    nombre: str
    correo: str
    unidad_negocio_id: int
    unidad: str
    roles: list[str] = Field(description="Roles de Azure en Preboletas: Admin, Comprador, Basculista.")
    permisos: list[PermisoUnidad] = Field(description="Permisos por unidad (Admin los tiene todos sin filas).")
    canales: list[str] = Field(description="Canales de aviso: app (siempre), correo, push.")


class TicketWs(BaseModel):
    ticket: str = Field(description='Se manda en el PRIMER mensaje del WebSocket: {"ticket": "..."}.')
    vence_en_segundos: int


class Salud(BaseModel):
    estado: Literal["ok", "degradado"]
    servicio: str
    version: str
    entorno: str
    base_datos: bool
    escuchas: dict[str, bool]


# ----------------------------------------------------------------------
# Catálogos
# ----------------------------------------------------------------------
class Unidad(BaseModel):
    id: int = Field(description="Igual al sitio de D365 (1, 2, 4, 14).")
    nombre: str
    inicio_operacion_at: datetime | None
    opera: bool = Field(description="Tiene fecha de implementación: sus boletas se ligan.")


class TipoPago(BaseModel):
    id: int
    clave: str
    nombre: str
    activo: bool


class TipoMovimiento(BaseModel):
    clave: str
    nombre: str
    badge: str
    color: str


class FrutaCaja(BaseModel):
    codigo: str
    nombre: str


class TipoCaja(BaseModel):
    id: int
    nombre: str
    kg_por_caja: float
    activo: bool
    orden: int
    frutas: list[FrutaCaja] = Field(description="Frutas con las que se permite esta caja.")


class NuevaCaja(BaseModel):
    nombre: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    kg_por_caja: Annotated[Decimal, Field(gt=0, max_digits=6, decimal_places=2)]
    orden: int = 0


class CambioCaja(BaseModel):
    nombre: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)] | None = None
    kg_por_caja: Annotated[Decimal, Field(gt=0, max_digits=6, decimal_places=2)] | None = None
    activo: bool | None = None
    orden: int | None = None


class Creado(BaseModel):
    id: int


class ProveedorD365(BaseModel):
    cuenta: str
    nombre: str
    sitio: int | None = Field(description="Sitio de compra en D365 (unidad sugerida); puede venir vacío.")


class FrutaD365(BaseModel):
    codigo: str
    nombre: str


class ColaboradorBreve(BaseModel):
    id: int
    nombre: str
    correo: str
    unidad_negocio_id: int
    unidad: str
    roles: list[str]


# ----------------------------------------------------------------------
# Preboletas
# ----------------------------------------------------------------------
class NuevaPreboleta(BaseModel):
    proveedor_cuenta: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)] = Field(
        description="Cuenta del proveedor en D365 (se valida y se toma su nombre de Dataverse)."
    )
    producto_codigo: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)] = Field(
        description="Número de artículo de la fruta (conjunto de producción MP)."
    )
    unidad_negocio_id: int
    tipo_caja_id: int
    tipo_negociacion: Literal["kilos", "cajas"]
    cantidad: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)] = Field(
        description="Kilos o cajas, según tipo_negociacion."
    )
    fecha_llegada: date | None = Field(default=None, description="Vacío = hoy (México).")
    comprador_id: int | None = Field(default=None, description="Solo Admin: crear para otro comprador.")
    multiples_cargas: bool = False
    precio_kg: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=4)] | None = Field(
        default=None, description="Vacío o 0 = sin precio propio (toma el del día)."
    )
    tipo_pago_id: int | None = None
    motivo: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class PreboletaCreada(BaseModel):
    id: int
    folio: str
    propuesta_id: int | None = Field(description="Si el precio o tipo difiere del día: propuesta enviada a autorizar.")


class PreboletaResumen(BaseModel):
    id: int
    folio: str
    unidad_negocio_id: int
    unidad: str
    proveedor_cuenta: str
    proveedor_nombre: str
    producto_codigo: str
    producto_nombre: str
    comprador_id: int
    comprador: str
    fecha_llegada: date
    tipo_negociacion: Literal["kilos", "cajas"]
    tipo_caja_id: int
    tipo_caja: str
    kg_por_caja: float
    kg_negociados: float | None
    cajas_negociadas: int
    tolerancia_pct: float
    cajas_minimas: int
    cajas_maximas: int
    multiples_cargas: bool
    estado: Literal["pendiente", "en_bascula", "en_proceso", "terminado", "terminado_con_incidencias"]
    cargas: int
    cajas_acumuladas: int
    kg_recibidos: float
    precio_kg_efectivo: float | None
    precio_personalizado: bool
    tipo_pago_id_efectivo: int | None
    tipo_pago_efectivo: str | None
    tipo_pago_clave: str | None = Field(description="Clave del tipo de pago vigente (pactado = precio tope).")
    tipo_pago_personalizado: bool
    precio_dia_kg: float | None
    total_estimado: float | None
    total_recibido: float
    incidencias: int
    incidencias_pendientes: int
    propuestas_pendientes: int
    atrasada: bool
    cerrada_at: datetime | None
    resultado_cierre: str | None
    creado_at: datetime


class ListaPreboletas(BaseModel):
    elementos: list[PreboletaResumen]
    total: int = Field(description="Cuántas cumplen los filtros (puede ser más que las devueltas).")
    limite: int
    sin_filtros: bool = Field(description="True = programadas para hoy más atrasadas de días anteriores.")
    atrasadas: int


class PreboletaCompleta(PreboletaResumen):
    proveedor_unidad_id: int | None
    proveedor_unidad: str | None
    creado_por: str | None
    cerrada_por: str | None
    precio_pactado_kg: float | None
    tipo_pago_id: int | None
    tipo_pago_dia_id: int | None
    tipo_pago_dia: str | None
    cargas_sin_boleta: int
    boletas_pendientes: int
    boletas_completadas: int
    primera_carga_at: datetime | None
    kg_negociados_efectivos: float
    qr_token: uuid.UUID
    qr_url: str


class Recepcion(BaseModel):
    recepcion_id: int
    cajas_declaradas: int
    via: Literal["qr", "manual"]
    basculista_id: int
    basculista: str
    ingreso_at: datetime
    boleta_id: int | None
    boleta_folio: str | None = Field(default=None, validation_alias="folio")
    placas: str | None
    estado_boleta: str | None
    peso_bruto: float | None
    tara: float | None
    peso_neto: float | None
    primera_pesada_at: datetime | None
    segunda_pesada_at: datetime | None
    boleta_requiere_revision: bool | None
    proveedor_distinto: bool
    cajas_reales: int | None
    precio_ajustado_kg: float | None
    precio_kg_efectivo: float | None
    importe: float | None
    tipo_pago_id_efectivo: int | None
    tipo_pago_efectivo: str | None
    cancelada: bool
    cancelada_at: datetime | None
    motivo_cancelacion: str | None
    cajas_corregidas: bool
    cajas_originales: int | None
    estado_d365: str | None
    estado_d365_codigo: int | None
    boleta_cerrada_d365: bool
    en_bascula: bool
    propuesta_pendiente: bool
    por_conciliar: bool


class IncidenciaPreboleta(BaseModel):
    id: int
    tipo: str
    estado: str
    recepcion_id: int | None
    boleta_id: int | None
    boleta_folio: str | None
    cajas_acumuladas: int | None
    cajas_limite: int | None
    cajas_diferencia: int | None
    proveedor_esperado: str | None
    proveedor_boleta: str | None
    comentario: str | None
    creado_at: datetime
    resuelto_at: datetime | None
    resuelto_por: str | None


class Propuesta(BaseModel):
    id: int
    estado: Literal["pendiente", "autorizada", "rechazada", "cancelada", "resuelta_sola"]
    unidad_negocio_id: int
    unidad: str
    preboleta_id: int
    folio: str
    proveedor_nombre: str
    recepcion_id: int | None
    boleta_folio: str | None
    precio_propuesto_kg: float | None
    tipo_pago_propuesto_id: int | None
    tipo_pago_propuesto: str | None
    precio_dia_kg: float | None
    tipo_pago_dia_id: int | None
    tipo_pago_dia: str | None
    tipo_pago_dia_clave: str | None
    precio_actual_kg: float | None
    tipo_pago_actual: str | None
    supera_tope: bool = Field(description="Precio Pactado por encima del tope del día (se marca, no se bloquea).")
    motivo: str | None
    propuesto_por_id: int
    propuesto_por: str
    propuesto_at: datetime
    resuelto_por: str | None
    resuelto_at: datetime | None
    comentario: str | None


class PropuestaLista(Propuesta):
    precio_vigente: float | None
    tipo_vigente: str | None
    puede_resolver: bool
    es_propia: bool
    preboleta_visible: bool


class Movimiento(BaseModel):
    id: int
    ocurrido_at: datetime
    tipo: str
    tipo_nombre: str
    badge: str
    color: str
    descripcion: str
    colaborador_id: int | None
    quien: str
    boleta_folio: str | None


class MovimientoLista(Movimiento):
    unidad_negocio_id: int
    preboleta_id: int | None
    preboleta_folio: str | None
    recepcion_id: int | None
    boleta_id: int | None
    incidencia_id: int | None
    propuesta_id: int | None


class ListaMovimientos(BaseModel):
    elementos: list[MovimientoLista]
    total: int


class EstadoBasculaPreboleta(BaseModel):
    ocupada: bool
    es_esta_preboleta: bool
    recepcion_id: int | None
    preboleta_id: int | None = Field(description="Solo si puedes ver esa otra preboleta.")
    folio: str | None


class Acciones(BaseModel):
    proponer_precio: bool
    proponer_precio_carga: bool
    multiples_cargas: bool
    cerrar_a_mano: bool
    cambiar_comprador: bool
    operar_bascula: bool


class DetallePreboleta(BaseModel):
    preboleta: PreboletaCompleta
    recepciones: list[Recepcion]
    incidencias: list[IncidenciaPreboleta]
    propuestas: list[Propuesta]
    movimientos: list[Movimiento]
    bascula: EstadoBasculaPreboleta
    acciones: Acciones


class CambioMultiples(BaseModel):
    activar: bool


class CierreManual(BaseModel):
    observacion: TextoObligatorio


class CambioComprador(BaseModel):
    comprador_id: int


class NuevaPropuesta(BaseModel):
    precio_kg: Precio | None = None
    tipo_pago_id: int | None = None
    motivo: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class PropuestaCreada(BaseModel):
    propuesta_id: int | None = Field(description="Vacío = quedó igual al precio del día y se aplicó sin autorización.")


class PreboletaPublica(BaseModel):
    folio: str
    proveedor_nombre: str
    unidad: str
    producto_nombre: str
    fecha_llegada: date
    cerrada: bool


class PreboletaPorQr(BaseModel):
    preboleta_id: int
    folio: str


# ----------------------------------------------------------------------
# Báscula
# ----------------------------------------------------------------------
class NuevaEntrada(BaseModel):
    cajas: int = Field(gt=0, description="Cajas que declara el chofer.")
    via: Literal["qr", "manual"] = "manual"
    multiples_cargas: bool = False


class Motivo(BaseModel):
    motivo: TextoObligatorio


class CorreccionCajas(BaseModel):
    cajas: int = Field(gt=0)
    motivo: TextoObligatorio


class BoletaActualizada(BaseModel):
    """Resultado de releer la boleta de una carga en D365 (a través de CoreAPI)."""

    folio: str
    resultado: str = Field(description="actualizada | creada | sin_cambios | ignorada_atrasada (lo que dijo CoreAPI).")
    mensaje: str


class Bascula(BaseModel):
    unidad_negocio_id: int
    unidad: str
    estatus: Literal["LIBRE", "OCUPADO"]
    opera: bool
    inicio_operacion_at: datetime | None
    ocupado_at: datetime | None
    detalle_visible: bool
    recepcion_id: int | None
    preboleta_id: int | None
    folio: str | None
    proveedor_cuenta: str | None
    proveedor_nombre: str | None
    cajas_declaradas: int | None
    incidencia_id: int | None
    tipo_incidencia: str | None
    estado_incidencia: str | None


# ----------------------------------------------------------------------
# Incidencias y conciliación
# ----------------------------------------------------------------------
class Resolucion(BaseModel):
    comentario: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


class IncidenciaCantidad(BaseModel):
    id: int
    tipo: Literal["sobre_entrega", "faltante", "cierre_manual"]
    estado: str
    unidad_negocio_id: int
    unidad: str
    preboleta_id: int
    folio: str
    preboleta_visible: bool
    proveedor_nombre: str
    comprador: str
    cajas_minimas: int
    cajas_maximas: int
    cajas_negociadas: int
    cajas_acumuladas: int | None
    cajas_limite: int | None
    cajas_diferencia: int | None
    boleta_folio: str | None
    comentario: str | None
    creado_at: datetime
    resuelto_at: datetime | None
    resuelto_por: str | None


class ConciliacionPendiente(BaseModel):
    incidencia_id: int
    tipo: Literal["proveedor_distinto", "sin_preboleta", "cambio_proveedor"]
    unidad_negocio_id: int
    unidad: str
    creado_at: datetime
    preboleta_sugerida_id: int | None
    preboleta_sugerida_folio: str | None
    preboleta_proveedor_nombre: str | None
    recepcion_sugerida_id: int | None
    proveedor_esperado: str | None
    proveedor_boleta: str | None
    boleta_id: int
    folio: str
    proveedor_cuenta: str | None
    proveedor_nombre: str | None
    placas: str | None
    estado_boleta: str
    peso_bruto: float
    tara: float
    peso_neto: float
    primera_pesada_at: datetime | None
    puede_resolver: bool


class ConciliacionResuelta(BaseModel):
    incidencia_id: int
    tipo: str
    estado: str
    unidad_negocio_id: int
    unidad: str
    creado_at: datetime
    resuelto_at: datetime | None
    resuelto_por: str | None
    comentario: str | None
    boleta_id: int
    folio: str
    proveedor_nombre: str | None
    peso_bruto: float
    peso_neto: float
    conciliada_preboleta_id: int | None
    conciliada_preboleta_folio: str | None


class ListaConciliaciones(BaseModel):
    pendientes: list[ConciliacionPendiente]
    resueltas: list[ConciliacionResuelta]


class IncidenciaConciliacion(BaseModel):
    id: int
    tipo: str
    estado: str
    unidad_negocio_id: int
    creado_at: datetime
    resuelto_at: datetime | None
    comentario: str | None
    proveedor_esperado: str | None
    proveedor_boleta: str | None
    preboleta_sugerida_id: int | None
    preboleta_sugerida_folio: str | None
    recepcion_sugerida_id: int | None


class BoletaD365(BaseModel):
    id: int
    folio: str
    unidad_negocio_id: int
    unidad: str
    proveedor_cuenta: str | None
    proveedor_nombre: str | None
    placas: str | None
    chofer: str | None
    peso_bruto: float
    tara: float
    peso_neto: float
    estado: str
    estado_d365: str | None
    estado_d365_codigo: int | None
    primera_pesada_at: datetime | None
    segunda_pesada_at: datetime | None


class CandidataConciliacion(BaseModel):
    id: int
    folio: str
    proveedor_cuenta: str
    proveedor_nombre: str
    producto_nombre: str
    fecha_llegada: date
    estado: str
    cajas_acumuladas: int
    cajas_negociadas: int
    cajas_minimas: int
    cajas_maximas: int
    cerrada: bool
    mismo_proveedor: bool


class CargaSinBoleta(BaseModel):
    id: int
    preboleta_id: int
    cajas_declaradas: int
    ingreso_at: datetime
    via: str
    en_bascula: bool


class DetalleConciliacion(BaseModel):
    incidencia: IncidenciaConciliacion
    boleta: BoletaD365
    puede_resolver: bool
    preboletas: list[CandidataConciliacion]
    cargas_sin_boleta: list[CargaSinBoleta]


class Vincular(BaseModel):
    preboleta_id: int
    recepcion_id: int | None = Field(default=None, description="Carga existente sin boleta de esa preboleta.")
    cajas_nueva_carga: int | None = Field(default=None, gt=0, description="O una carga nueva con estas cajas.")
    comentario: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


class Descartar(BaseModel):
    comentario: TextoObligatorio


class Vinculada(BaseModel):
    recepcion_id: int


# ----------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------
class PrecioFruta(BaseModel):
    producto_codigo: str
    producto_nombre: str
    precio_kg: float | None
    tipo_pago_id: int | None
    tipo_pago: str | None
    tipo_pago_clave: str | None
    actualizado_por: str | None
    actualizado_at: datetime | None
    tolerancia_pct: float | None
    tipo_pago_sugerido_id: int


class PreciosUnidad(BaseModel):
    unidad_negocio_id: int
    unidad: str
    fecha: date
    frutas: list[PrecioFruta]
    dataverse_disponible: bool = Field(description="False = Dataverse no respondió: solo frutas ya configuradas.")
    puede_tolerancia: bool


class PrecioHoy(BaseModel):
    producto_codigo: str
    producto_nombre: str
    precio_kg: float
    tipo_pago_id: int
    tipo_pago: str
    tipo_pago_clave: str


class PreciosHoyUnidad(BaseModel):
    unidad_negocio_id: int
    unidad: str
    fecha: date
    frutas: list[PrecioHoy] = Field(description="Solo las frutas que ya tienen precio hoy. Vacía = sin definir.")


class GuardarPrecio(BaseModel):
    precio_kg: Precio
    tipo_pago_id: int
    fecha: date | None = Field(default=None, description="Vacío = hoy (México).")


class GuardarTolerancia(BaseModel):
    tolerancia_pct: Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]


class Implementacion(BaseModel):
    inicio_operacion_at: datetime | None = Field(
        description="Vacío = pausar la unidad. Sin zona horaria se interpreta en hora de México."
    )


class ColaboradorAdmin(ColaboradorBreve):
    canales: list[str]
    dispositivos_activos: int
    permisos: list[PermisoUnidad]


class Canales(BaseModel):
    correo: bool
    push: bool


# ----------------------------------------------------------------------
# Avisos y dispositivos
# ----------------------------------------------------------------------
class Entrega(BaseModel):
    canal: str
    estado: str
    error: str | None


class Aviso(BaseModel):
    id: int
    creado_at: datetime
    mensaje: str
    leida: bool
    leida_at: datetime | None
    preboleta_id: int | None
    folio: str | None
    incidencia_id: int | None
    incidencia_tipo: str | None
    propuesta_id: int | None
    destinatario_id: int
    destinatario: str
    entregas: list[Entrega]


class SinLeer(BaseModel):
    total: int


class Resumen(BaseModel):
    incidencias: int = Field(description="Incidencias de cantidad pendientes (compradores y Admin).")
    conciliaciones: int = Field(description="Boletas por conciliar que tú puedes resolver.")
    propuestas: int = Field(description="Propuestas pendientes que tú puedes autorizar.")
    avisos_sin_leer: int
    kg_recibidos_hoy: float = Field(
        description="Peso neto de las boletas completadas hoy (hora de México) en las preboletas que puedes ver."
    )


class PrecioDia(BaseModel):
    unidad_negocio_id: int
    producto_codigo: str
    fecha: date
    precio_kg: float | None = Field(description="Vacío = sin asignar.")
    tipo_pago_id: int | None
    tipo_pago: str | None
    tipo_pago_clave: str | None
    tolerancia_pct: float | None = Field(description="Vacío = no se pueden crear preboletas de esa fruta en la unidad.")


class MarcarLeidos(BaseModel):
    ids: list[int] | None = Field(default=None, description="Vacío = todos mis avisos.")


class Marcados(BaseModel):
    marcados: int


class NuevoDispositivo(BaseModel):
    token: Annotated[str, StringConstraints(pattern=r"^Expo(nent)?PushToken\[.+\]$", max_length=255)]
    plataforma: Literal["android", "ios"] = "android"
    nombre_dispositivo: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
