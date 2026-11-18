"""Catálogos: unidades, tipos de pago y de movimiento, cajas, y proveedores y frutas de Dataverse."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.dependencias import ServiciosDep, UsuarioActual
from app.errores import RESPUESTAS_COMUNES, AccesoDenegado
from app.esquemas.modelos import (
    CambioCaja,
    ColaboradorBreve,
    Creado,
    FrutaD365,
    NuevaCaja,
    Ok,
    ProveedorD365,
    TipoCaja,
    TipoMovimiento,
    TipoPago,
    Unidad,
)
from app.rutas.comun import en_transaccion, fruta_valida
from app.servicios import colaboradores, configuracion
from app.usuario import PERMISO_PRECIOS

router = APIRouter(tags=["Catálogos"], responses=RESPUESTAS_COMUNES)
Limite = Annotated[int, Query(ge=1, le=50, description="Máximo de resultados.")]


@router.get("/unidades", response_model=list[Unidad], summary="Unidades de negocio y si ya operan")
def unidades(_: UsuarioActual, servicios: ServiciosDep) -> list[Unidad]:
    with servicios.bd.transaccion() as con:
        return [Unidad.model_validate(u) for u in configuracion.unidades(con)]


@router.get("/tipos-pago", response_model=list[TipoPago], summary="Tipos de pago (catálogo fijo)")
def tipos_pago(_: UsuarioActual, servicios: ServiciosDep) -> list[TipoPago]:
    with servicios.bd.transaccion() as con:
        filas = con.execute("SELECT id, clave, nombre, activo FROM preboletas.tipos_pago ORDER BY id").fetchall()
    return [TipoPago.model_validate(f) for f in filas]


@router.get("/tipos-movimiento", response_model=list[TipoMovimiento], summary="Tipos de movimiento de la bitácora")
def tipos_movimiento(_: UsuarioActual, servicios: ServiciosDep) -> list[TipoMovimiento]:
    with servicios.bd.transaccion() as con:
        filas = con.execute(
            "SELECT clave, nombre, badge, color FROM preboletas.tipos_movimiento ORDER BY nombre"
        ).fetchall()
    return [TipoMovimiento.model_validate(f) for f in filas]


@router.get("/tipos-caja", response_model=list[TipoCaja], summary="Tipos de caja (y frutas permitidas)")
def tipos_caja(
    usuario: UsuarioActual,
    servicios: ServiciosDep,
    producto_codigo: str | None = Query(default=None, description="Solo las cajas permitidas para esta fruta."),
    incluir_inactivas: bool = Query(default=False, description="Solo Admin: también las desactivadas."),
) -> list[TipoCaja]:
    with servicios.bd.transaccion() as con:
        filas = configuracion.tipos_caja(
            con, producto_codigo=producto_codigo, incluir_inactivos=incluir_inactivas and usuario.es_admin
        )
    return [TipoCaja.model_validate(f) for f in filas]


@router.post("/tipos-caja", response_model=Creado, status_code=status.HTTP_201_CREATED, summary="Nueva caja (Admin)")
def crear_caja(datos: NuevaCaja, usuario: UsuarioActual, servicios: ServiciosDep) -> Creado:
    caja_id = en_transaccion(
        servicios,
        usuario,
        lambda op: configuracion.crear_caja(op, usuario, datos.nombre, datos.kg_por_caja, datos.orden),
    )
    return Creado(id=caja_id)


@router.patch("/tipos-caja/{caja_id}", response_model=Ok, summary="Cambiar o desactivar una caja (Admin)")
def cambiar_caja(caja_id: int, datos: CambioCaja, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    cambios = datos.model_dump(exclude_none=True)
    en_transaccion(servicios, usuario, lambda op: configuracion.cambiar_caja(op, usuario, caja_id, cambios))
    return Ok()


@router.put(
    "/tipos-caja/{caja_id}/frutas/{producto_codigo}", response_model=Ok, summary="Permitir la caja para una fruta"
)
def permitir_fruta(caja_id: int, producto_codigo: str, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    if not usuario.es_admin:
        raise AccesoDenegado("Solo un Admin administra los tipos de caja.")
    fruta = fruta_valida(servicios, producto_codigo)
    en_transaccion(servicios, usuario, lambda op: configuracion.permitir_fruta(op, usuario, caja_id, fruta))
    return Ok()


@router.delete("/tipos-caja/{caja_id}/frutas/{producto_codigo}", response_model=Ok, summary="Ya no permitir la caja")
def quitar_fruta(caja_id: int, producto_codigo: str, usuario: UsuarioActual, servicios: ServiciosDep) -> Ok:
    en_transaccion(servicios, usuario, lambda op: configuracion.quitar_fruta(op, usuario, caja_id, producto_codigo))
    return Ok()


# ----------------------------------------------------------------------
# Dataverse (en vivo; puede tardar 1-3 s)
# ----------------------------------------------------------------------
def _puede_buscar(usuario: UsuarioActual) -> None:
    if not (usuario.es_comprador or usuario.es_admin or usuario.tiene_permiso_en_alguna(PERMISO_PRECIOS)):
        raise AccesoDenegado("Buscan en D365 los compradores, los Admin y quien captura precios.")


@router.get("/proveedores", response_model=list[ProveedorD365], summary="Buscar proveedores de materia prima en D365")
def proveedores(
    usuario: UsuarioActual,
    servicios: ServiciosDep,
    buscar: str = Query(default="", max_length=100),
    limite: Limite = 10,
) -> list[ProveedorD365]:
    """Por nombre o cuenta. Siempre filtra empresa SIC y grupo MATPRIMA."""
    _puede_buscar(usuario)
    return [
        ProveedorD365(cuenta=p.cuenta, nombre=p.nombre, sitio=p.sitio)
        for p in servicios.dataverse.buscar_proveedores(buscar, limite)
    ]


@router.get("/frutas", response_model=list[FrutaD365], summary="Buscar frutas admitidas (conjunto MP) en D365")
def frutas(
    usuario: UsuarioActual,
    servicios: ServiciosDep,
    buscar: str = Query(default="", max_length=100),
    limite: Limite = 20,
) -> list[FrutaD365]:
    """Por nombre o número de artículo. Siempre filtra empresa sic y conjunto de producción MP."""
    _puede_buscar(usuario)
    return [FrutaD365(codigo=f.codigo, nombre=f.nombre) for f in servicios.dataverse.buscar_frutas(buscar, limite)]


@router.get("/colaboradores", response_model=list[ColaboradorBreve], summary="Colaboradores con rol en Preboletas")
def lista_colaboradores(
    _: UsuarioActual,
    servicios: ServiciosDep,
    rol: str | None = Query(default=None, description="Admin, Comprador o Basculista."),
    buscar: str | None = Query(default=None, max_length=100),
) -> list[ColaboradorBreve]:
    """Para elegir comprador o filtrar la bitácora. Solo nombres, correos y roles."""
    with servicios.bd.transaccion() as con:
        return [ColaboradorBreve.model_validate(c) for c in colaboradores.listar(con, rol=rol, buscar=buscar)]
