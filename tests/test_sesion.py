"""Sesión, salud, catálogos, WebSocket y página pública."""

from __future__ import annotations

from datetime import datetime, timedelta

from tests.conftest import LIMON, YUC, BaseDePrueba, Entorno, Escenario


def test_salud(entorno: Entorno) -> None:
    r = entorno.cliente.get("/salud")
    assert r.status_code == 200
    assert r.json()["base_datos"] is True


def test_yo_vincula_y_devuelve_perfil(esc: Escenario) -> None:
    esc.e.permiso(esc.comprador["id"], "precios_diarios", YUC, esc.admin["id"])
    r = esc.e.cliente.get("/yo", headers=esc.comprador["h"])
    assert r.status_code == 200, r.text
    yo = r.json()
    assert yo["colaborador_id"] == esc.comprador["id"]
    assert yo["roles"] == ["Comprador"]
    assert yo["unidad"]
    assert yo["permisos"] == [{"permiso": "precios_diarios", "unidad_negocio_id": YUC}]
    assert yo["canales"] == ["app"]
    assert len(esc.e.core.llamadas) == 1  # se pidió a CoreAPI vincular la cuenta


def test_sin_rol_de_preboletas(entorno: Entorno) -> None:
    r = entorno.cliente.get("/yo", headers=entorno.encabezado("6f1c2b1e-0000-4000-8000-0000000000aa", ["SuperAdmin"]))
    assert r.status_code == 403
    assert r.json()["codigo"] == "rol_insuficiente"


def test_sin_vinculacion(entorno: Entorno) -> None:
    r = entorno.cliente.get(
        "/preboletas", headers=entorno.encabezado("6f1c2b1e-0000-4000-8000-0000000000ff", ["Comprador"])
    )
    assert r.status_code == 403
    assert r.json()["codigo"] == "sin_vinculacion"


def test_sin_token(entorno: Entorno) -> None:
    r = entorno.cliente.get("/preboletas")
    assert r.status_code == 401
    assert r.json()["codigo"] == "no_autenticado"


def test_catalogos(esc: Escenario) -> None:
    h = esc.comprador["h"]
    assert {u["id"] for u in esc.e.cliente.get("/unidades", headers=h).json()} >= {14, 1}
    assert {t["clave"] for t in esc.e.cliente.get("/tipos-pago", headers=h).json()} >= {"rendimiento", "pactado"}
    cajas = esc.e.cliente.get("/tipos-caja", params={"producto_codigo": LIMON}, headers=h).json()
    assert [c["id"] for c in cajas] == [esc.caja_id]
    assert esc.e.cliente.get("/tipos-caja", params={"producto_codigo": "02"}, headers=h).json() == []
    provs = esc.e.cliente.get("/proveedores", params={"buscar": "citri"}, headers=h).json()
    assert provs == [{"cuenta": "MP2520", "nombre": "CITRICOS DEL SURESTE", "sitio": 14}]
    assert len(esc.e.cliente.get("/frutas", headers=h).json()) == 2
    # El basculista no busca en D365.
    assert esc.e.cliente.get("/proveedores", headers=esc.basculista["h"]).status_code == 403


def test_dataverse_caido_responde_503(esc: Escenario) -> None:
    esc.e.dataverse.caido = True
    r = esc.e.cliente.get("/proveedores", params={"buscar": "x"}, headers=esc.comprador["h"])
    assert r.status_code == 503
    assert r.json()["codigo"] == "dataverse_no_disponible"


def test_websocket_con_ticket(esc: Escenario) -> None:
    ticket = esc.e.cliente.post("/ws/ticket", headers=esc.comprador["h"]).json()["ticket"]
    with esc.e.cliente.websocket_connect("/ws") as ws:
        ws.send_json({"ticket": ticket})
        assert ws.receive_json() == {"evento": "conectado", "colaborador_id": esc.comprador["id"]}
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


def test_websocket_sin_ticket_valido(esc: Escenario) -> None:
    with esc.e.cliente.websocket_connect("/ws") as ws:
        ws.send_json({"ticket": "inventado"})
        mensaje = ws.receive()
        assert mensaje["type"] == "websocket.close"
        assert mensaje["code"] == 4401


def test_pagina_publica_del_qr(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    token = esc.detalle(pb["id"])["preboleta"]["qr_token"]
    r = esc.e.cliente.get(f"/publico/qr/{token}")
    assert r.status_code == 200
    publico = r.json()
    assert publico["folio"] == pb["folio"]
    # Nada de cajas, kilos, precios ni comprador.
    assert set(publico) == {"folio", "proveedor_nombre", "unidad", "producto_nombre", "fecha_llegada", "cerrada"}
    assert esc.e.cliente.get("/publico/qr/no-es-uuid").status_code == 404


def test_pagina_publica_limita_por_minuto(esc: Escenario) -> None:
    for _ in range(5):  # el límite de pruebas es 5
        esc.e.cliente.get("/publico/qr/00000000-0000-0000-0000-000000000000")
    r = esc.e.cliente.get("/publico/qr/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "60"


def test_catalogo_de_movimientos_en_sicar(bd_prueba: BaseDePrueba) -> None:
    """La base real debe tener todos los tipos de movimiento que escribe la API (llave foránea)."""
    import pytest

    from app.main import TIPOS_MOVIMIENTO_USADOS

    if not bd_prueba.catalogos_de_sicar:
        pytest.skip("No se pudo leer la base sicar para revisar su catálogo.")
    assert sorted(TIPOS_MOVIMIENTO_USADOS - bd_prueba.claves_sicar) == []


def test_limite_publico_usa_la_ip_reenviada_por_el_panel(esc: Escenario) -> None:
    ruta = "/publico/qr/00000000-0000-0000-0000-000000000000"
    for _ in range(5):
        esc.e.cliente.get(ruta, headers={"X-Forwarded-For": "10.0.0.1"})
    assert esc.e.cliente.get(ruta, headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 429
    assert esc.e.cliente.get(ruta, headers={"X-Forwarded-For": "10.0.0.2, 1.1.1.1"}).status_code == 404


def test_resumen_de_pendientes(esc: Escenario) -> None:
    pb = esc.crear_preboleta(precio_kg="12")  # propuesta pendiente (sin precio del día)
    esc.entrada(pb["id"], 10)  # faltante pendiente
    b = esc.e.boleta(YUC, "MP3000")
    esc.e.procesar(b)  # proveedor distinto: por conciliar
    admin = esc.e.cliente.get("/resumen", headers=esc.admin["h"]).json()
    assert admin["incidencias"] == 1 and admin["conciliaciones"] == 1 and admin["propuestas"] == 1
    assert admin["avisos_sin_leer"] >= 1
    # El comprador no autoriza su propia propuesta ni tiene el permiso.
    comprador = esc.e.cliente.get("/resumen", headers=esc.comprador["h"]).json()
    assert comprador["propuestas"] == 0 and comprador["incidencias"] == 1
    basculista = esc.e.cliente.get("/resumen", headers=esc.basculista_tec["h"]).json()
    assert basculista == {
        "incidencias": 0,
        "conciliaciones": 0,
        "propuestas": 0,
        "avisos_sin_leer": 0,
        "kg_recibidos_hoy": 0,
    }


def test_resumen_kg_recibidos_hoy(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    esc.entrada(pb["id"], 100)
    b = esc.e.boleta(YUC, "MP2520", bruto="9400")
    esc.e.procesar(b)  # 1ra pesada: aún no cuenta
    assert esc.e.cliente.get("/resumen", headers=esc.admin["h"]).json()["kg_recibidos_hoy"] == 0
    esc.e.actualizar_boleta(b, tara="7600")  # 2da pesada: neto 1800 kg
    esc.e.procesar(b)
    for quien in (esc.admin, esc.comprador, esc.basculista):
        assert esc.e.cliente.get("/resumen", headers=quien["h"]).json()["kg_recibidos_hoy"] == 1800
    # Otra unidad y otro comprador no la ven.
    for quien in (esc.basculista_tec, esc.comprador2):
        assert esc.e.cliente.get("/resumen", headers=quien["h"]).json()["kg_recibidos_hoy"] == 0
    # Lo que entró otro día no cuenta.
    hace_dos_dias = datetime.now().astimezone() - timedelta(days=2)
    esc.e.actualizar_boleta(b, primera_pesada_at=hace_dos_dias - timedelta(hours=1), segunda_pesada_at=hace_dos_dias)
    assert esc.e.cliente.get("/resumen", headers=esc.admin["h"]).json()["kg_recibidos_hoy"] == 0


def test_precio_del_dia_para_capturar(esc: Escenario) -> None:
    ruta = f"/unidades/{YUC}/precio-dia/{LIMON}"
    vacio = esc.e.cliente.get(ruta, headers=esc.comprador["h"]).json()
    assert vacio["precio_kg"] is None and vacio["tolerancia_pct"] == 10
    esc.e.precio_dia(YUC, LIMON, "15.5", "pactado", esc.admin["id"])
    dia = esc.e.cliente.get(ruta, headers=esc.comprador["h"]).json()
    assert (dia["precio_kg"], dia["tipo_pago_clave"]) == (15.5, "pactado")
    assert esc.e.cliente.get(ruta, headers=esc.basculista["h"]).status_code == 403


# ----------------------------------------------------------------------
# El login no depende de CoreAPI
# ----------------------------------------------------------------------
def test_yo_con_core_caido_deja_entrar_a_quien_ya_estaba_vinculado(esc: Escenario) -> None:
    esc.e.core.caido = True
    r = esc.e.cliente.get("/yo", headers=esc.comprador["h"])
    assert r.status_code == 200, r.text
    assert r.json()["colaborador_id"] == esc.comprador["id"]
    assert len(esc.e.core.llamadas) == 1  # sí se intentó sincronizar, pero el login no esperó


def test_dado_de_alta_sin_vincular_entra_con_core_caido(esc: Escenario) -> None:
    """Alguien dado de alta que nunca ha entrado se reconoce por su correo, leyendo la base."""
    esc.e.core.caido = True
    nuevo = esc.e.colaborador("Paola Nueva", ["Comprador"], vinculado=False)
    r = esc.e.cliente.get("/yo", headers=nuevo["h"])
    assert r.status_code == 200, r.text
    yo = r.json()
    assert yo["colaborador_id"] == nuevo["id"]
    assert yo["nombre"] == "Paola Nueva"  # la base aún no tiene nombre: se usa el del token
    # Y opera como cualquiera, aunque CoreAPI nunca haya guardado su vínculo.
    assert esc.e.cliente.get("/preboletas", headers=nuevo["h"]).status_code == 200
    assert esc.e.uno("SELECT entra_oid FROM core.colaboradores WHERE id = %s", nuevo["id"])["entra_oid"] is None


def test_correo_ligado_a_otra_cuenta_no_entra(esc: Escenario) -> None:
    """Si el colaborador ya está ligado a OTRA cuenta de Microsoft, el correo no basta (como en CoreAPI)."""
    otra_cuenta = "6f1c2b1e-0000-4000-8000-0000000000bb"
    h = esc.e.encabezado(otra_cuenta, ["Comprador"], upn=esc.comprador["correo"])
    r = esc.e.cliente.get("/yo", headers=h)
    assert r.status_code == 403 and r.json()["codigo"] == "sin_vinculacion"
    assert esc.e.core.llamadas == []  # no se sincroniza a quien no entra


def test_sin_alta_no_entra(entorno: Entorno) -> None:
    h = entorno.encabezado("6f1c2b1e-0000-4000-8000-0000000000cc", ["Admin"], upn="nadie@sicar.com.mx")
    r = entorno.cliente.get("/yo", headers=h)
    assert r.status_code == 403 and r.json()["codigo"] == "sin_vinculacion"
