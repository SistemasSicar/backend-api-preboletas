"""Propuestas de precio, precios del día, tolerancias, implementación, cajas, permisos, avisos y bitácora."""

from __future__ import annotations

from datetime import date, timedelta

from app.trabajos.envio_notificaciones import enviar_pendientes
from tests.conftest import LIMON, PERSA, TEC, YUC, Escenario


# ----------------------------------------------------------------------
# Propuestas
# ----------------------------------------------------------------------
def test_autorizar_propuesta_y_autoautorizacion(esc: Escenario) -> None:
    esc.e.precio_dia(YUC, LIMON, "15", "rendimiento", esc.admin["id"])
    autorizador = esc.e.colaborador("Daniel Autoriza", ["Comprador"])
    esc.e.permiso(autorizador["id"], "autorizar_precios", YUC, esc.admin["id"])
    esc.e.permiso(esc.comprador["id"], "autorizar_precios", YUC, esc.admin["id"])
    pb = esc.crear_preboleta(precio_kg="16")
    prop = pb["propuesta_id"]
    # Nadie autoriza la suya (salvo un Admin).
    r = esc.e.cliente.post(f"/propuestas/{prop}/autorizar", json={}, headers=esc.comprador["h"])
    assert r.status_code == 403 and r.json()["codigo"] == "autoautorizacion"
    # Sin permiso en la unidad.
    r = esc.e.cliente.post(f"/propuestas/{prop}/autorizar", json={}, headers=esc.comprador2["h"])
    assert r.status_code == 403 and r.json()["codigo"] == "sin_permiso"
    lista = esc.e.cliente.get("/propuestas", params={"estado": "pendiente"}, headers=autorizador["h"]).json()
    assert lista[0]["puede_resolver"] and lista[0]["precio_vigente"] == 15
    r = esc.e.cliente.post(f"/propuestas/{prop}/autorizar", json={"comentario": "Ok"}, headers=autorizador["h"])
    assert r.status_code == 200, r.text
    d = esc.detalle(pb["id"])["preboleta"]
    assert d["precio_kg_efectivo"] == 16 and d["precio_personalizado"]
    # El comprador recibió el aviso de que se autorizó.
    avisos = esc.e.cliente.get("/notificaciones", headers=esc.comprador["h"]).json()
    assert any("autorizada" in a["mensaje"] for a in avisos)


def test_propuesta_igual_al_dia_se_aplica_sola(esc: Escenario) -> None:
    esc.e.precio_dia(YUC, LIMON, "15", "rendimiento", esc.admin["id"])
    pb = esc.crear_preboleta()
    r = esc.e.cliente.post(f"/preboletas/{pb['id']}/propuestas", json={"precio_kg": "15"}, headers=esc.comprador["h"])
    assert r.json() == {"propuesta_id": None}
    # Una propuesta nueva reemplaza a la anterior pendiente.
    p1 = esc.e.cliente.post(f"/preboletas/{pb['id']}/propuestas", json={"precio_kg": "17"}, headers=esc.comprador["h"])
    p2 = esc.e.cliente.post(f"/preboletas/{pb['id']}/propuestas", json={"precio_kg": "18"}, headers=esc.comprador["h"])
    estados = esc.e.todos("SELECT id, estado FROM preboletas.propuestas_precio ORDER BY id")
    assert [e["estado"] for e in estados] == ["cancelada", "pendiente"]
    assert estados[1]["id"] == p2.json()["propuesta_id"] != p1.json()["propuesta_id"]


def test_propuesta_pendiente_se_resuelve_sola_con_el_precio_del_dia(esc: Escenario) -> None:
    pb = esc.crear_preboleta(precio_kg="14")  # sin precio del día: pactado con autorización
    r = esc.e.cliente.put(
        f"/unidades/{YUC}/precios/{LIMON}", json={"precio_kg": "14", "tipo_pago_id": 2}, headers=esc.admin["h"]
    )
    assert r.status_code == 200, r.text
    estado = esc.e.uno("SELECT estado FROM preboletas.propuestas_precio WHERE id = %s", pb["propuesta_id"])["estado"]
    assert estado == "resuelta_sola"


def test_supera_el_tope_se_marca(esc: Escenario) -> None:
    esc.e.precio_dia(YUC, LIMON, "15", "pactado", esc.admin["id"])
    pb = esc.crear_preboleta(precio_kg="16")
    [prop] = esc.detalle(pb["id"])["propuestas"]
    assert prop["supera_tope"] and prop["estado"] == "pendiente"


def test_propuesta_por_carga(esc: Escenario) -> None:
    esc.e.precio_dia(YUC, LIMON, "15", "rendimiento", esc.admin["id"])
    pb = esc.crear_preboleta()
    rec = esc.entrada(pb["id"], 100)
    r = esc.e.cliente.post(f"/recepciones/{rec}/propuestas", json={"precio_kg": "13"}, headers=esc.comprador["h"])
    prop = r.json()["propuesta_id"]
    esc.e.cliente.post(f"/propuestas/{prop}/autorizar", json={}, headers=esc.admin["h"])
    carga = esc.detalle(pb["id"])["recepciones"][0]
    assert carga["precio_ajustado_kg"] == 13 and carga["precio_kg_efectivo"] == 13


# ----------------------------------------------------------------------
# Precios, tolerancias, implementación
# ----------------------------------------------------------------------
def test_precio_del_dia_requiere_permiso_y_5_dias(esc: Escenario) -> None:
    datos = {"precio_kg": "15", "tipo_pago_id": 1}
    r = esc.e.cliente.put(f"/unidades/{YUC}/precios/{LIMON}", json=datos, headers=esc.comprador["h"])
    assert r.status_code == 403
    esc.e.permiso(esc.comprador["id"], "precios_diarios", YUC, esc.admin["id"])
    viejo = (date.today() - timedelta(days=10)).isoformat()
    r = esc.e.cliente.put(
        f"/unidades/{YUC}/precios/{LIMON}", json={**datos, "fecha": viejo}, headers=esc.comprador["h"]
    )
    assert r.status_code == 403
    r = esc.e.cliente.put(f"/unidades/{YUC}/precios/{LIMON}", json={**datos, "fecha": viejo}, headers=esc.admin["h"])
    assert r.status_code == 200
    precios = esc.e.cliente.get(f"/unidades/{YUC}/precios", headers=esc.comprador["h"]).json()
    assert {f["producto_codigo"] for f in precios["frutas"]} == {LIMON, PERSA}  # todas las frutas MP
    limon = next(f for f in precios["frutas"] if f["producto_codigo"] == LIMON)
    assert limon["tolerancia_pct"] == 10 and precios["dataverse_disponible"]


def test_precios_hoy_resumen_por_unidad_sin_dataverse(esc: Escenario) -> None:
    esc.e.precio_dia(YUC, LIMON, "15", "rendimiento", esc.admin["id"])
    esc.e.precio_dia(TEC, LIMON, "12.5", "pactado", esc.admin["id"])
    esc.e.precio_dia(YUC, PERSA, "20", "corrida", esc.admin["id"], fecha=date.today() - timedelta(days=1))  # ayer
    esc.e.dataverse.caido = True  # el resumen no consulta Dataverse

    admin = esc.e.cliente.get("/precios-hoy", headers=esc.admin["h"])
    assert admin.status_code == 200, admin.text
    por_unidad = {u["unidad_negocio_id"]: u for u in admin.json()}
    assert len(por_unidad) == 4  # todas las unidades, aunque no tengan precio
    yuc = por_unidad[YUC]["frutas"]
    assert [(f["producto_codigo"], f["precio_kg"], f["tipo_pago_clave"]) for f in yuc] == [(LIMON, 15, "rendimiento")]
    assert por_unidad[TEC]["frutas"][0]["tipo_pago_clave"] == "pactado"
    assert all(not u["frutas"] for k, u in por_unidad.items() if k not in (YUC, TEC))

    # Con el permiso en una sola unidad, solo esa; sin permiso en ninguna, 403.
    esc.e.permiso(esc.comprador["id"], "precios_diarios", YUC, esc.admin["id"])
    comprador = esc.e.cliente.get("/precios-hoy", headers=esc.comprador["h"]).json()
    assert [u["unidad_negocio_id"] for u in comprador] == [YUC]
    r = esc.e.cliente.get("/precios-hoy", headers=esc.comprador2["h"])
    assert r.status_code == 403 and r.json()["codigo"] == "sin_permiso"


def test_tolerancia_solo_admin_y_no_cambia_preboletas_creadas(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    r = esc.e.cliente.put(
        f"/unidades/{YUC}/tolerancias/{LIMON}", json={"tolerancia_pct": "20"}, headers=esc.comprador["h"]
    )
    assert r.status_code == 403
    r = esc.e.cliente.put(f"/unidades/{YUC}/tolerancias/{LIMON}", json={"tolerancia_pct": "20"}, headers=esc.admin["h"])
    assert r.status_code == 200
    assert esc.detalle(pb["id"])["preboleta"]["tolerancia_pct"] == 10
    nueva = esc.crear_preboleta()
    assert esc.detalle(nueva["id"])["preboleta"]["cajas_maximas"] == 120


def test_implementacion_en_hora_de_mexico(esc: Escenario) -> None:
    r = esc.e.cliente.put(
        "/unidades/1/implementacion", json={"inicio_operacion_at": "2026-11-11T08:00:00"}, headers=esc.admin["h"]
    )
    assert r.status_code == 200, r.text
    fila = esc.e.uno(
        "SELECT to_char(inicio_operacion_at AT TIME ZONE 'UTC', 'HH24:MI') AS utc FROM preboletas.unidades_config "
        "WHERE unidad_negocio_id = 1"
    )
    assert fila["utc"] == "14:00"  # 08:00 en México (UTC-6)
    r = esc.e.cliente.put("/unidades/1/implementacion", json={"inicio_operacion_at": None}, headers=esc.admin["h"])
    unidades = {u["id"]: u for u in esc.e.cliente.get("/unidades", headers=esc.admin["h"]).json()}
    assert unidades[1]["opera"] is False and unidades[YUC]["opera"] is True


# ----------------------------------------------------------------------
# Cajas, permisos y canales (Admin)
# ----------------------------------------------------------------------
def test_cajas_por_fruta(esc: Escenario) -> None:
    r = esc.e.cliente.post("/tipos-caja", json={"nombre": "Caja grande", "kg_por_caja": "20"}, headers=esc.admin["h"])
    assert r.status_code == 201
    caja = r.json()["id"]
    assert esc.e.cliente.put(f"/tipos-caja/{caja}/frutas/{PERSA}", headers=esc.admin["h"]).status_code == 200
    assert (
        esc.e.cliente.post(
            "/tipos-caja", json={"nombre": "Caja grande", "kg_por_caja": "20"}, headers=esc.admin["h"]
        ).status_code
        == 409
    )
    # No se puede quitar una caja que ya usa una preboleta.
    esc.crear_preboleta()
    r = esc.e.cliente.delete(f"/tipos-caja/{esc.caja_id}/frutas/{LIMON}", headers=esc.admin["h"])
    assert r.status_code == 409 and r.json()["codigo"] == "caja_en_uso"
    assert esc.e.cliente.delete(f"/tipos-caja/{caja}/frutas/{PERSA}", headers=esc.admin["h"]).status_code == 200
    assert esc.e.cliente.patch(f"/tipos-caja/{caja}", json={"activo": False}, headers=esc.admin["h"]).status_code == 200
    assert (
        esc.e.cliente.post(
            "/tipos-caja", json={"nombre": "X", "kg_por_caja": "1"}, headers=esc.comprador["h"]
        ).status_code
        == 403
    )


def test_permisos_y_canales(esc: Escenario) -> None:
    ruta = f"/colaboradores/{esc.comprador['id']}/permisos/autorizar_precios/{YUC}"
    assert esc.e.cliente.put(ruta, headers=esc.comprador["h"]).status_code == 403
    assert esc.e.cliente.put(ruta, headers=esc.admin["h"]).status_code == 200
    r = esc.e.cliente.put(
        f"/colaboradores/{esc.comprador['id']}/canales", json={"correo": True, "push": True}, headers=esc.admin["h"]
    )
    assert r.status_code == 200
    lista = {c["id"]: c for c in esc.e.cliente.get("/admin/colaboradores", headers=esc.admin["h"]).json()}
    assert lista[esc.comprador["id"]]["canales"] == ["correo", "push"]
    assert lista[esc.comprador["id"]]["permisos"] == [{"permiso": "autorizar_precios", "unidad_negocio_id": YUC}]
    assert esc.e.cliente.delete(ruta, headers=esc.admin["h"]).status_code == 200
    assert "permiso_colaborador" in {m["tipo"] for m in esc.e.todos("SELECT tipo FROM preboletas.movimientos")}


# ----------------------------------------------------------------------
# Avisos: campana, correo y push
# ----------------------------------------------------------------------
def test_envio_de_avisos(esc: Escenario) -> None:
    esc.e.cliente.put(
        f"/colaboradores/{esc.comprador['id']}/canales", json={"correo": True, "push": True}, headers=esc.admin["h"]
    )
    r = esc.e.cliente.post(
        "/dispositivos", json={"token": "ExponentPushToken[abc]", "plataforma": "android"}, headers=esc.comprador["h"]
    )
    assert r.status_code == 200
    pb = esc.crear_preboleta()
    esc.entrada(pb["id"], 10)  # faltante → avisa al comprador
    assert esc.e.cliente.get("/notificaciones/sin-leer", headers=esc.comprador["h"]).json()["total"] >= 1
    assert enviar_pendientes(esc.e.servicios.bd, esc.e.push) >= 2
    filas = {
        f["canal"]: f
        for f in esc.e.todos(
            "SELECT canal, estado, ultimo_error FROM preboletas.notificaciones WHERE destinatario_id = %s",
            esc.comprador["id"],
        )
    }
    assert filas["push"]["estado"] == "enviada"
    assert (filas["correo"]["estado"], filas["correo"]["ultimo_error"]) == ("fallida", "correo_no_configurado")
    assert esc.e.push.enviados[0][0] == ["ExponentPushToken[abc]"]
    avisos = esc.e.cliente.get("/notificaciones", headers=esc.comprador["h"]).json()
    assert {e["canal"] for e in avisos[0]["entregas"]} == {"correo", "push"}
    r = esc.e.cliente.post("/notificaciones/leidas", json={}, headers=esc.comprador["h"])
    assert r.json()["marcados"] >= 1
    assert esc.e.cliente.get("/notificaciones/sin-leer", headers=esc.comprador["h"]).json()["total"] == 0
    assert esc.e.cliente.get("/notificaciones/envios", headers=esc.comprador["h"]).status_code == 403


def test_token_push_invalido_se_da_de_baja(esc: Escenario) -> None:
    esc.e.cliente.put(
        f"/colaboradores/{esc.comprador['id']}/canales", json={"correo": False, "push": True}, headers=esc.admin["h"]
    )
    esc.e.cliente.post("/dispositivos", json={"token": "ExponentPushToken[viejo]"}, headers=esc.comprador["h"])
    esc.e.push.invalidos.add("ExponentPushToken[viejo]")
    pb = esc.crear_preboleta()
    esc.entrada(pb["id"], 10)
    enviar_pendientes(esc.e.servicios.bd, esc.e.push)
    disp = esc.e.uno("SELECT activo, motivo_baja FROM preboletas.dispositivos_push")
    assert (disp["activo"], disp["motivo_baja"]) == (False, "token_invalido")


# ----------------------------------------------------------------------
# Bitácora
# ----------------------------------------------------------------------
def test_bitacora_visible_por_rol(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    otra = esc.crear_preboleta(esc.admin, comprador_id=esc.comprador2["id"])
    mios = esc.e.cliente.get("/movimientos", headers=esc.comprador["h"]).json()
    assert {m["preboleta_id"] for m in mios["elementos"]} == {pb["id"]}
    todos = esc.e.cliente.get("/movimientos", headers=esc.admin["h"]).json()
    assert {m["preboleta_id"] for m in todos["elementos"]} >= {pb["id"], otra["id"]}
    filtrado = esc.e.cliente.get(
        "/movimientos", params={"tipos": ["preboleta_creada"], "preboleta_id": otra["id"]}, headers=esc.admin["h"]
    ).json()
    assert filtrado["total"] == 1 and filtrado["elementos"][0]["quien"] == "Eduardo Admin"
    assert (
        esc.e.cliente.get(
            "/movimientos", params={"desde": "2030-01-02", "hasta": "2030-01-01"}, headers=esc.admin["h"]
        ).status_code
        == 400
    )
