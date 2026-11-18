"""Pruebas sin base de datos: filtros OData, cálculos, token, tickets, límite y reparto de eventos."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import Configuracion
from app.errores import NoAutenticado
from app.integraciones.dataverse import FRUTAS, PROVEEDORES, a_proveedor, filtro_busqueda, filtro_clave, literal_odata
from app.seguridad.limitador import LimitadorPorMinuto
from app.seguridad.tickets_ws import TicketsWs
from app.seguridad.token import ValidadorToken
from app.servicios.comun import folio
from app.servicios.preboletas import calcular_cajas
from app.tiempo_real.hub import puede_recibir
from app.usuario import Usuario
from tests.conftest import LlavesDePrueba, configuracion, firmar_token


def _usuario(roles: set[str], *, id_: int = 7, unidad: int = 14) -> Usuario:
    return Usuario(id_, "Prueba", "p@sicar.com.mx", unidad, frozenset(roles))


# ----------------------------------------------------------------------
# Dataverse: filtros siempre con empresa y grupo, y texto escapado
# ----------------------------------------------------------------------
def test_literal_odata_duplica_comillas() -> None:
    assert literal_odata("O'Brien") == "'O''Brien'"
    assert literal_odata("x') or (1 eq 1") == "'x'') or (1 eq 1'"


def test_filtro_busqueda_proveedores_lleva_empresa_y_grupo() -> None:
    f = filtro_busqueda(PROVEEDORES, "citri")
    assert "contains(mserp_vendororganizationname,'citri')" in f
    assert "contains(mserp_vendoraccountnumber,'citri')" in f
    assert "mserp_dataareaid eq 'SIC'" in f
    assert "mserp_vendorgroupid eq 'MATPRIMA'" in f


def test_filtro_frutas_usa_empresa_en_minusculas_y_conjunto_mp() -> None:
    f = filtro_clave(FRUTAS, "01")
    assert f == "mserp_itemnumber eq '01' and mserp_dataareaid eq 'sic' and mserp_productionpoolid eq 'MP'"
    # Sin texto: todo el catálogo, pero nunca sin el filtro fijo.
    assert filtro_busqueda(FRUTAS, "  ") == "mserp_dataareaid eq 'sic' and mserp_productionpoolid eq 'MP'"


def test_validacion_por_codigo_escapa_texto() -> None:
    assert "eq 'MP''1'" in filtro_clave(PROVEEDORES, "MP'1")


def test_sitio_del_proveedor() -> None:
    con_sitio = a_proveedor(
        {"mserp_vendoraccountnumber": "MP1", "mserp_vendororganizationname": "X", "mserp_defaultpurchasesiteid": "14"}
    )
    sin_sitio = a_proveedor(
        {"mserp_vendoraccountnumber": "MP2", "mserp_vendororganizationname": "Y", "mserp_defaultpurchasesiteid": ""}
    )
    assert con_sitio.sitio == 14
    assert sin_sitio.sitio is None


# ----------------------------------------------------------------------
# Cálculos
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tipo", "cantidad", "kg", "esperado"),
    [
        ("kilos", "1800", "18", 100),
        ("kilos", "27", "18", 2),  # 1.5 → 2 (mitades hacia arriba, igual que round() de PostgreSQL)
        ("kilos", "26.99", "18", 1),
        ("cajas", "250", "20", 250),
    ],
)
def test_calcular_cajas(tipo: str, cantidad: str, kg: str, esperado: int) -> None:
    assert calcular_cajas(tipo, Decimal(cantidad), Decimal(kg)) == esperado


def test_folio() -> None:
    assert folio(7) == "PB-0007"
    assert folio(12345) == "PB-12345"


# ----------------------------------------------------------------------
# Token
# ----------------------------------------------------------------------
def _validador() -> ValidadorToken:
    cfg: Configuracion = configuracion("postgresql://x@localhost/x")
    return ValidadorToken(cfg, LlavesDePrueba())


OID = "6f1c2b1e-0000-4000-8000-000000000001"


def test_token_valido() -> None:
    identidad = _validador().validar(firmar_token(OID, ["Comprador", "Otro"]))
    assert identidad.entra_oid == OID
    assert identidad.roles == ("Comprador", "Otro")


@pytest.mark.parametrize(
    ("cambios", "codigo"),
    [
        ({"aud": "api://otra-app"}, "token_audiencia"),
        ({"exp": 1000, "iat": 900}, "token_expirado"),
        ({"tid": "otro-tenant"}, "token_invalido"),
        ({"scp": "User.Read"}, "token_sin_permiso"),
        ({"oid": "no-es-uuid"}, "token_invalido"),
    ],
)
def test_token_rechazado(cambios: dict[str, object], codigo: str) -> None:
    with pytest.raises(NoAutenticado) as exc:
        _validador().validar(firmar_token(OID, ["Admin"], **cambios))
    assert exc.value.codigo == codigo


# ----------------------------------------------------------------------
# Tickets del WebSocket y límite de la página pública
# ----------------------------------------------------------------------
def test_ticket_sirve_una_vez_y_vence() -> None:
    reloj = [0.0]
    tickets = TicketsWs(reloj=lambda: reloj[0])
    u = _usuario({"Admin"})
    t, _ = tickets.emitir(u)
    assert tickets.canjear(t) == u
    assert tickets.canjear(t) is None  # un solo uso
    t2, _ = tickets.emitir(u)
    reloj[0] = 61
    assert tickets.canjear(t2) is None  # vencido


def test_limitador_por_minuto() -> None:
    reloj = [0.0]
    lim = LimitadorPorMinuto(2, reloj=lambda: reloj[0])
    assert lim.permitir("ip") and lim.permitir("ip")
    assert not lim.permitir("ip")
    assert lim.permitir("otra-ip")
    reloj[0] = 61
    assert lim.permitir("ip")


# ----------------------------------------------------------------------
# Reparto de eventos por rol
# ----------------------------------------------------------------------
def test_reparto_de_eventos() -> None:
    admin = _usuario({"Admin"})
    comprador = _usuario({"Comprador"}, id_=5)
    basculista = _usuario({"Basculista"}, id_=6, unidad=14)
    pb_ajena = {"evento": "preboleta.actualizada", "unidad_negocio_id": 1, "comprador_id": 99}
    assert puede_recibir(admin, pb_ajena)
    assert not puede_recibir(comprador, pb_ajena)
    assert not puede_recibir(basculista, pb_ajena)
    assert puede_recibir(comprador, {**pb_ajena, "comprador_id": 5})
    assert puede_recibir(basculista, {**pb_ajena, "unidad_negocio_id": 14})
    # Incidencias: los compradores de todas las unidades.
    assert puede_recibir(comprador, {"evento": "incidencia.creada", "unidad_negocio_id": 1})
    # Avisos personales: solo su destinatario, ni siquiera el Admin.
    aviso = {"evento": "notificacion.nueva", "destinatario_id": 5}
    assert puede_recibir(comprador, aviso)
    assert not puede_recibir(admin, aviso)
    # Báscula: a todos.
    assert puede_recibir(comprador, {"evento": "slot.actualizado", "unidad_negocio_id": 4})


def test_permisos_del_usuario() -> None:
    u = Usuario(1, "X", "x@x.mx", 14, frozenset({"Comprador"}), frozenset({("autorizar_precios", 14)}))
    assert u.tiene_permiso("autorizar_precios", 14)
    assert not u.tiene_permiso("autorizar_precios", 1)
    assert not u.puede_operar_bascula(14)
    assert u.puede_conciliar("sin_preboleta", 1)
    assert not u.puede_conciliar("cambio_proveedor", 14)
    assert _usuario({"Admin"}).tiene_permiso("precios_diarios", 4)


def test_sincronizador_core_nunca_hace_fallar_el_login() -> None:
    from app.errores import ServicioNoDisponible
    from app.integraciones.sincronizador_core import SincronizadorCore
    from app.seguridad.token import IdentidadToken

    class CoreCaido:
        llamadas = 0

        def verificar(self, identidad: IdentidadToken) -> int:
            CoreCaido.llamadas += 1
            raise ServicioNoDisponible("CoreAPI no responde.", codigo="core_no_disponible")

    sincronizador = SincronizadorCore(CoreCaido(), en_segundo_plano=False)
    identidad = IdentidadToken(OID, "X", "x@sicar.com.mx", ("Admin",))
    sincronizador.programar(identidad)  # no lanza
    sincronizador.programar(identidad)  # se puede volver a intentar
    assert CoreCaido.llamadas == 2


# ----------------------------------------------------------------------
# Push de Expo: cada mensaje va al canal "avisos" de la app, con prioridad alta
# ----------------------------------------------------------------------
def test_push_expo_usa_canal_avisos_y_prioridad_alta(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.integraciones import expo_push

    enviados: list[dict[str, object]] = []

    def post_falso(url: str, *, json: list[dict[str, object]], timeout: float) -> httpx.Response:
        enviados.extend(json)
        return httpx.Response(200, json={"data": [{"status": "ok"} for _ in json]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post_falso)
    tokens = ["ExponentPushToken[uno]", "ExponentPushToken[dos]"]
    resultados = expo_push.ExpoPush().enviar(tokens, "Título", "Cuerpo", {"preboleta_id": 5})

    assert [r.ok for r in resultados] == [True, True]
    assert [m["to"] for m in enviados] == tokens
    for mensaje in enviados:
        assert mensaje["channelId"] == "avisos" == expo_push.CANAL_ANDROID
        assert mensaje["priority"] == "high"
        assert mensaje["data"] == {"preboleta_id": 5}
