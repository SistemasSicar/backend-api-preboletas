"""Alta de preboletas, precio al crear, visibilidad por rol y operaciones de báscula."""

from __future__ import annotations

from tests.conftest import LIMON, PERSA, TEC, YUC, Escenario


# ----------------------------------------------------------------------
# Alta
# ----------------------------------------------------------------------
def test_crear_en_kilos_calcula_cajas_y_rango(esc: Escenario) -> None:
    pb = esc.crear_preboleta(tipo_negociacion="kilos", cantidad="1800")  # 18 kg por caja → 100 cajas
    d = esc.detalle(pb["id"])["preboleta"]
    assert pb["folio"] == f"PB-{pb['id']:04d}"
    assert (d["cajas_negociadas"], d["cajas_minimas"], d["cajas_maximas"]) == (100, 90, 110)
    assert d["kg_negociados"] == 1800
    assert d["estado"] == "pendiente"
    assert d["proveedor_nombre"] == "CITRICOS DEL SURESTE"  # tomado de Dataverse, no del usuario
    assert d["producto_nombre"] == "LIMON MEXICANO"
    assert d["comprador_id"] == esc.comprador["id"]
    assert d["qr_url"].startswith("https://preboletas.prueba/qr/")
    movs = [m["tipo"] for m in esc.detalle(pb["id"])["movimientos"]]
    assert "preboleta_creada" in movs


def test_unidad_distinta_al_sitio_del_proveedor_queda_registrada(esc: Escenario) -> None:
    esc.e.tolerancia(TEC, LIMON, "10", esc.admin["id"])
    pb = esc.crear_preboleta(unidad_negocio_id=TEC)
    d = esc.detalle(pb["id"])
    assert d["preboleta"]["proveedor_unidad_id"] == YUC
    assert "unidad_preboleta" in [m["tipo"] for m in d["movimientos"]]


def test_caja_no_permitida_y_sin_tolerancia(esc: Escenario) -> None:
    datos = {
        "proveedor_cuenta": "MP2520",
        "producto_codigo": PERSA,
        "unidad_negocio_id": YUC,
        "tipo_caja_id": esc.caja_id,
        "tipo_negociacion": "cajas",
        "cantidad": 10,
    }
    r = esc.e.cliente.post("/preboletas", json=datos, headers=esc.comprador["h"])
    assert r.status_code == 400 and r.json()["codigo"] == "caja_no_permitida"
    caja_persa = esc.e.caja("Caja persa", "20", frutas=(PERSA,))
    r = esc.e.cliente.post("/preboletas", json={**datos, "tipo_caja_id": caja_persa}, headers=esc.comprador["h"])
    assert r.status_code == 400 and r.json()["codigo"] == "sin_tolerancia"


def test_proveedor_o_fruta_inexistentes(esc: Escenario) -> None:
    base = {"unidad_negocio_id": YUC, "tipo_caja_id": esc.caja_id, "tipo_negociacion": "cajas", "cantidad": 10}
    r = esc.e.cliente.post(
        "/preboletas",
        json={**base, "proveedor_cuenta": "NOEXISTE", "producto_codigo": LIMON},
        headers=esc.comprador["h"],
    )
    assert r.json()["codigo"] == "proveedor_inexistente"
    r = esc.e.cliente.post(
        "/preboletas", json={**base, "proveedor_cuenta": "MP2520", "producto_codigo": "99"}, headers=esc.comprador["h"]
    )
    assert r.json()["codigo"] == "fruta_inexistente"


def test_dataverse_caido_no_guarda(esc: Escenario) -> None:
    esc.e.dataverse.caido = True
    r = esc.e.cliente.post(
        "/preboletas",
        json={
            "proveedor_cuenta": "MP2520",
            "producto_codigo": LIMON,
            "unidad_negocio_id": YUC,
            "tipo_caja_id": esc.caja_id,
            "tipo_negociacion": "cajas",
            "cantidad": 10,
        },
        headers=esc.comprador["h"],
    )
    assert r.status_code == 503
    assert esc.e.uno("SELECT count(*) AS n FROM preboletas.preboletas")["n"] == 0


def test_basculista_no_crea_y_comprador_no_crea_para_otro(esc: Escenario) -> None:
    datos = {
        "proveedor_cuenta": "MP2520",
        "producto_codigo": LIMON,
        "unidad_negocio_id": YUC,
        "tipo_caja_id": esc.caja_id,
        "tipo_negociacion": "cajas",
        "cantidad": 10,
    }
    assert esc.e.cliente.post("/preboletas", json=datos, headers=esc.basculista["h"]).status_code == 403
    r = esc.e.cliente.post(
        "/preboletas", json={**datos, "comprador_id": esc.comprador2["id"]}, headers=esc.comprador["h"]
    )
    assert r.status_code == 403
    # El Admin sí, para un comprador.
    pb = esc.crear_preboleta(esc.admin, comprador_id=esc.comprador2["id"])
    assert esc.detalle(pb["id"])["preboleta"]["comprador_id"] == esc.comprador2["id"]


# ----------------------------------------------------------------------
# Precio al crear
# ----------------------------------------------------------------------
def test_sin_precio_del_dia_con_precio_nace_pactado_con_propuesta(esc: Escenario) -> None:
    pb = esc.crear_preboleta(precio_kg="12.5")
    assert pb["propuesta_id"] is not None
    prop = esc.e.uno("SELECT * FROM preboletas.propuestas_precio WHERE id = %s", pb["propuesta_id"])
    tipo = esc.e.uno("SELECT clave FROM preboletas.tipos_pago WHERE id = %s", prop["tipo_pago_propuesto_id"])
    assert tipo["clave"] == "pactado"
    assert prop["estado"] == "pendiente"
    # Mientras no se autorice, no tiene precio.
    assert esc.detalle(pb["id"])["preboleta"]["precio_kg_efectivo"] is None


def test_sin_precio_toma_el_del_dia_cuando_se_define(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    assert pb["propuesta_id"] is None
    esc.e.permiso(esc.comprador["id"], "precios_diarios", YUC, esc.admin["id"])
    r = esc.e.cliente.put(
        f"/unidades/{YUC}/precios/{LIMON}", json={"precio_kg": "15", "tipo_pago_id": 1}, headers=esc.comprador["h"]
    )
    assert r.status_code == 200, r.text
    d = esc.detalle(pb["id"])["preboleta"]
    assert d["precio_kg_efectivo"] == 15 and d["tipo_pago_clave"] == "rendimiento"
    assert d["total_estimado"] == 15 * 100 * 18


def test_con_precio_del_dia_igual_no_crea_propuesta_y_distinto_si(esc: Escenario) -> None:
    esc.e.precio_dia(YUC, LIMON, "15", "rendimiento", esc.admin["id"])
    assert esc.crear_preboleta(precio_kg="15")["propuesta_id"] is None
    assert esc.crear_preboleta(precio_kg="16")["propuesta_id"] is not None


# ----------------------------------------------------------------------
# Visibilidad
# ----------------------------------------------------------------------
def test_visibilidad_por_rol(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    ruta = f"/preboletas/{pb['id']}"
    assert esc.e.cliente.get(ruta, headers=esc.comprador["h"]).status_code == 200
    assert esc.e.cliente.get(ruta, headers=esc.basculista["h"]).status_code == 200  # su unidad
    assert esc.e.cliente.get(ruta, headers=esc.comprador2["h"]).status_code == 404  # no es suya
    assert esc.e.cliente.get(ruta, headers=esc.basculista_tec["h"]).status_code == 404  # otra unidad
    assert esc.e.cliente.get("/preboletas", headers=esc.comprador2["h"]).json()["total"] == 0
    assert esc.e.cliente.get("/preboletas", headers=esc.basculista["h"]).json()["total"] == 1


def test_lista_por_omision_es_hoy_mas_atrasadas(esc: Escenario) -> None:
    hoy = esc.crear_preboleta()
    ayer = esc.crear_preboleta(fecha_llegada="2020-01-01")
    manana = esc.crear_preboleta(fecha_llegada="2099-01-01")
    lista = esc.e.cliente.get("/preboletas", headers=esc.comprador["h"]).json()
    ids = {p["id"] for p in lista["elementos"]}
    assert ids == {hoy["id"], ayer["id"]}
    assert lista["atrasadas"] == 1 and lista["sin_filtros"]
    filtrada = esc.e.cliente.get("/preboletas", params={"desde": "2099-01-01"}, headers=esc.comprador["h"]).json()
    assert [p["id"] for p in filtrada["elementos"]] == [manana["id"]]


def test_lista_por_omision_mas_recientes_primero(esc: Escenario) -> None:
    """La recién creada sale arriba; las atrasadas, al final."""
    atrasada = esc.crear_preboleta(fecha_llegada="2020-01-01")
    primera = esc.crear_preboleta()
    segunda = esc.crear_preboleta()
    lista = esc.e.cliente.get("/preboletas", headers=esc.comprador["h"]).json()
    assert [p["id"] for p in lista["elementos"]] == [segunda["id"], primera["id"], atrasada["id"]]


# ----------------------------------------------------------------------
# Báscula
# ----------------------------------------------------------------------
def test_entrada_ocupa_la_bascula(esc: Escenario) -> None:
    pb1, pb2 = esc.crear_preboleta(), esc.crear_preboleta()
    rec = esc.entrada(pb1["id"], 100)
    d = esc.detalle(pb1["id"])
    assert d["preboleta"]["estado"] == "en_bascula"
    assert d["bascula"]["es_esta_preboleta"]
    basculas = {b["unidad_negocio_id"]: b for b in esc.e.cliente.get("/basculas", headers=esc.comprador2["h"]).json()}
    assert basculas[YUC]["estatus"] == "OCUPADO"
    assert basculas[YUC]["detalle_visible"] is False  # no es su camión
    assert basculas[YUC]["preboleta_id"] is None
    r = esc.e.cliente.post(f"/preboletas/{pb2['id']}/recepciones", json={"cajas": 5}, headers=esc.basculista["h"])
    assert r.status_code == 409 and r.json()["codigo"] == "bascula_ocupada"
    assert rec > 0


def test_basculista_de_otra_unidad_no_opera(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    r = esc.e.cliente.post(f"/preboletas/{pb['id']}/recepciones", json={"cajas": 5}, headers=esc.basculista_tec["h"])
    assert r.status_code == 404  # ni siquiera la ve


def test_faltante_en_una_carga_y_se_cancela_con_multiples(esc: Escenario) -> None:
    pb = esc.crear_preboleta()  # 100 cajas, mínimo 90
    esc.entrada(pb["id"], 50)
    inc = esc.e.uno("SELECT * FROM preboletas.incidencias WHERE preboleta_id = %s", pb["id"])
    assert (inc["tipo"], inc["estado"], inc["cajas_limite"], inc["cajas_diferencia"]) == (
        "faltante",
        "pendiente",
        90,
        40,
    )
    # Avisó al comprador (campana) aunque no actuó.
    avisos = esc.e.cliente.get("/notificaciones", headers=esc.comprador["h"]).json()
    assert any("Faltante" in a["mensaje"] for a in avisos)
    r = esc.e.cliente.patch(
        f"/preboletas/{pb['id']}/multiples-cargas", json={"activar": True}, headers=esc.comprador["h"]
    )
    assert r.status_code == 200
    assert esc.e.uno("SELECT estado FROM preboletas.incidencias WHERE id = %s", inc["id"])["estado"] == "cancelada"


def test_sobre_entrega_y_resolucion_por_otro_comprador(esc: Escenario) -> None:
    pb = esc.crear_preboleta()  # máximo 110
    esc.entrada(pb["id"], 120)
    inc = esc.e.uno("SELECT * FROM preboletas.incidencias WHERE preboleta_id = %s", pb["id"])
    assert inc["tipo"] == "sobre_entrega" and inc["cajas_diferencia"] == 10
    # Cualquier comprador, de cualquier unidad, la ve y la resuelve.
    lista = esc.e.cliente.get("/incidencias", headers=esc.comprador2["h"]).json()
    assert [i["id"] for i in lista] == [inc["id"]] and lista[0]["preboleta_visible"] is False
    r = esc.e.cliente.post(
        f"/incidencias/{inc['id']}/autorizar", json={"comentario": "Se acepta"}, headers=esc.comprador2["h"]
    )
    assert r.status_code == 200
    r = esc.e.cliente.post(f"/incidencias/{inc['id']}/rechazar", json={}, headers=esc.comprador2["h"])
    assert r.status_code == 409 and r.json()["codigo"] == "incidencia_resuelta"
    assert esc.e.cliente.get("/incidencias", headers=esc.basculista["h"]).status_code == 403


def test_cancelar_entrada_libera_y_reevalua(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    rec = esc.entrada(pb["id"], 50)  # nace faltante
    r = esc.e.cliente.post(f"/recepciones/{rec}/cancelar", json={"motivo": "Se regresó"}, headers=esc.basculista["h"])
    assert r.status_code == 200, r.text
    d = esc.detalle(pb["id"])
    assert d["preboleta"]["estado"] == "pendiente" and d["preboleta"]["cajas_acumuladas"] == 0
    assert d["recepciones"][0]["cancelada"]
    assert esc.e.uno("SELECT estado FROM preboletas.incidencias")["estado"] == "cancelada"
    assert (
        esc.e.uno("SELECT recepcion_id FROM preboletas.slots_bascula WHERE unidad_negocio_id = %s", YUC)["recepcion_id"]
        is None
    )
    # Ya no está en báscula: no se puede volver a cancelar.
    r = esc.e.cliente.post(f"/recepciones/{rec}/cancelar", json={"motivo": "otra"}, headers=esc.basculista["h"])
    assert r.status_code == 409


def test_corregir_cajas_guarda_las_originales(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    rec = esc.entrada(pb["id"], 120)  # sobre-entrega
    r = esc.e.cliente.post(
        f"/recepciones/{rec}/corregir-cajas",
        json={"cajas": 100, "motivo": "Se contaron mal"},
        headers=esc.basculista["h"],
    )
    assert r.status_code == 200, r.text
    fila = esc.e.uno("SELECT cajas_declaradas, cajas_originales FROM preboletas.recepciones WHERE id = %s", rec)
    assert (fila["cajas_declaradas"], fila["cajas_originales"]) == (100, 120)
    assert esc.e.uno("SELECT estado FROM preboletas.incidencias")["estado"] == "cancelada"
    r = esc.e.cliente.post(
        f"/recepciones/{rec}/corregir-cajas", json={"cajas": 100, "motivo": "igual"}, headers=esc.basculista["h"]
    )
    assert r.status_code == 400  # mismas cajas
