"""Motor de boletas (vinculación con D365), cierre automático y conciliación."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.motor.escucha import barrer
from tests.conftest import TEC, YUC, Escenario


def _incidencias(esc: Escenario, tipo: str) -> list[dict[str, object]]:
    return esc.e.todos("SELECT * FROM preboletas.incidencias WHERE tipo = %s ORDER BY id", tipo)


def test_primera_pesada_liga_y_segunda_cierra(esc: Escenario) -> None:
    pb = esc.crear_preboleta()  # 100 cajas de 18 kg
    rec = esc.entrada(pb["id"], 100)
    b = esc.e.boleta(YUC, "MP2520", bruto="9400")
    esc.e.procesar(b)
    r = esc.e.uno("SELECT boleta_id, vinculada_por_id FROM preboletas.recepciones WHERE id = %s", rec)
    assert r["boleta_id"] == b and r["vinculada_por_id"] is None  # automática
    assert (
        esc.e.uno("SELECT recepcion_id FROM preboletas.slots_bascula WHERE unidad_negocio_id = %s", YUC)["recepcion_id"]
        is None
    )  # báscula libre
    assert esc.detalle(pb["id"])["preboleta"]["estado"] == "en_proceso"

    esc.e.actualizar_boleta(b, tara="7600")  # 2da pesada: neto 1800 kg
    esc.e.procesar(b)
    d = esc.detalle(pb["id"])
    assert d["preboleta"]["estado"] == "terminado"
    assert d["recepciones"][0]["cajas_reales"] == 100
    tipos = [m["tipo"] for m in d["movimientos"]]
    assert tipos.count("salida_bascula") == 1 and "preboleta_cerrada" in tipos

    # Idempotente: volver a pasar la boleta (barrido) no duplica nada.
    esc.e.procesar(b)
    assert barrer(esc.e.servicios.bd) >= 1
    assert esc.e.uno("SELECT count(*) AS n FROM preboletas.movimientos WHERE tipo = 'salida_bascula'")["n"] == 1


def test_proveedor_distinto_crea_incidencia_y_libera(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    esc.entrada(pb["id"], 100)
    b = esc.e.boleta(YUC, "MP3000")
    esc.e.procesar(b)
    [inc] = _incidencias(esc, "proveedor_distinto")
    assert inc["proveedor_esperado"] == "MP2520" and inc["proveedor_boleta"] == "MP3000"
    assert (
        esc.e.uno("SELECT recepcion_id FROM preboletas.slots_bascula WHERE unidad_negocio_id = %s", YUC)["recepcion_id"]
        is None
    )
    # El basculista de la unidad la ve en conciliación; el de otra unidad no.
    pend = esc.e.cliente.get("/conciliaciones", headers=esc.basculista["h"]).json()["pendientes"]
    assert [p["incidencia_id"] for p in pend] == [inc["id"]] and pend[0]["puede_resolver"]
    assert esc.e.cliente.get("/conciliaciones", headers=esc.basculista_tec["h"]).json()["pendientes"] == []
    # Recibieron aviso el basculista de la unidad, el comprador y el Admin.
    destinatarios = {
        f["destinatario_id"]
        for f in esc.e.todos(
            "SELECT destinatario_id FROM preboletas.notificaciones WHERE incidencia_id = %s AND canal = 'app'",
            inc["id"],
        )
    }
    assert destinatarios == {esc.basculista["id"], esc.comprador["id"], esc.admin["id"]}


def test_sin_preboleta_y_descartar(esc: Escenario) -> None:
    b = esc.e.boleta(YUC, "MP3000")
    esc.e.procesar(b)
    [inc] = _incidencias(esc, "sin_preboleta")
    r = esc.e.cliente.post(
        f"/conciliaciones/{inc['id']}/descartar", json={"comentario": " "}, headers=esc.comprador["h"]
    )
    assert r.status_code == 422  # comentario obligatorio
    r = esc.e.cliente.post(
        f"/conciliaciones/{inc['id']}/descartar", json={"comentario": "Compra directa"}, headers=esc.comprador["h"]
    )
    assert r.status_code == 200
    assert esc.e.uno("SELECT estado FROM preboletas.incidencias WHERE id = %s", inc["id"])["estado"] == "descartada"
    resueltas = esc.e.cliente.get("/conciliaciones", headers=esc.admin["h"]).json()["resueltas"]
    assert resueltas[0]["estado"] == "descartada"


def test_conciliar_con_carga_nueva_y_cerrar(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    b = esc.e.boleta(YUC, "MP2520", bruto="9400", tara="7600")  # llegó completa y sin entrada en báscula
    esc.e.procesar(b)
    [inc] = _incidencias(esc, "sin_preboleta")
    detalle = esc.e.cliente.get(f"/conciliaciones/{inc['id']}", headers=esc.comprador["h"]).json()
    assert detalle["preboletas"][0]["id"] == pb["id"] and detalle["preboletas"][0]["mismo_proveedor"]
    r = esc.e.cliente.post(
        f"/conciliaciones/{inc['id']}/vincular",
        json={"preboleta_id": pb["id"], "cajas_nueva_carga": 100, "comentario": "Entró sin registro"},
        headers=esc.comprador["h"],
    )
    assert r.status_code == 200, r.text
    d = esc.detalle(pb["id"])
    assert d["preboleta"]["estado"] == "terminado"  # el motor completó la salida y el cierre
    assert d["recepciones"][0]["boleta_id"] == b
    # Ya resuelta: no se puede volver a conciliar.
    r = esc.e.cliente.post(f"/conciliaciones/{inc['id']}/descartar", json={"comentario": "x"}, headers=esc.admin["h"])
    assert r.status_code == 409


def test_cambio_de_proveedor_desliga_y_regresa(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    rec = esc.entrada(pb["id"], 100)
    b = esc.e.boleta(YUC, "MP2520")
    esc.e.procesar(b)
    esc.e.actualizar_boleta(b, proveedor_cuenta="MP3000")
    esc.e.procesar(b)
    assert esc.e.uno("SELECT boleta_id FROM preboletas.recepciones WHERE id = %s", rec)["boleta_id"] is None
    [inc] = _incidencias(esc, "cambio_proveedor")
    assert inc["estado"] == "pendiente"
    # Un comprador no concilia un cambio de proveedor; el basculista de la unidad sí podría.
    pend = esc.e.cliente.get("/conciliaciones", headers=esc.comprador["h"]).json()["pendientes"]
    assert pend[0]["puede_resolver"] is False
    # D365 regresa el proveedor original: se vuelve a ligar sola.
    esc.e.actualizar_boleta(b, proveedor_cuenta="MP2520")
    esc.e.procesar(b)
    assert esc.e.uno("SELECT boleta_id FROM preboletas.recepciones WHERE id = %s", rec)["boleta_id"] == b
    assert esc.e.uno("SELECT estado FROM preboletas.incidencias WHERE id = %s", inc["id"])["estado"] == "cancelada"


def test_boleta_cancelada_en_d365_reabre(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    rec = esc.entrada(pb["id"], 100)
    b = esc.e.boleta(YUC, "MP2520")
    esc.e.procesar(b)
    esc.e.actualizar_boleta(b, tara="7600")
    esc.e.procesar(b)
    assert esc.detalle(pb["id"])["preboleta"]["estado"] == "terminado"
    esc.e.actualizar_boleta(b, estado_d365=200000005)
    esc.e.procesar(b)
    r = esc.e.uno(
        "SELECT cancelada_at, cancelada_por_id, motivo_cancelacion FROM preboletas.recepciones WHERE id = %s", rec
    )
    assert r["cancelada_at"] is not None and r["cancelada_por_id"] is None  # la canceló el sistema
    d = esc.detalle(pb["id"])["preboleta"]
    assert d["estado"] == "pendiente" and d["cajas_acumuladas"] == 0 and d["cerrada_at"] is None


def test_unidad_sin_implementacion_se_ignora(esc: Escenario) -> None:
    b = esc.e.boleta(TEC, "MP4000")
    esc.e.procesar(b)
    assert esc.e.todos("SELECT * FROM preboletas.incidencias") == []
    # Boleta con 1ra pesada anterior a la implementación: también se ignora.
    viejo = esc.e.boleta(YUC, "MP3000", primera_pesada_at=datetime.now().astimezone() - timedelta(days=30))
    esc.e.procesar(viejo)
    assert esc.e.todos("SELECT * FROM preboletas.incidencias") == []


def test_camion_que_entro_despues_de_la_pesada_no_se_liga(esc: Escenario) -> None:
    """Una boleta atrasada (ej. la recupera el barrido) no se lleva al camión que entró después."""
    pb = esc.crear_preboleta()
    esc.entrada(pb["id"], 100)
    b = esc.e.boleta(YUC, "MP2520", primera_pesada_at=datetime.now().astimezone() - timedelta(hours=2))
    esc.e.procesar(b)
    assert [i["tipo"] for i in esc.e.todos("SELECT tipo FROM preboletas.incidencias")] == ["sin_preboleta"]
    assert esc.detalle(pb["id"])["preboleta"]["estado"] == "en_bascula"


def test_cierre_manual(esc: Escenario) -> None:
    pb = esc.crear_preboleta(multiples_cargas=True)
    esc.entrada(pb["id"], 40)
    b = esc.e.boleta(YUC, "MP2520")
    esc.e.procesar(b)
    ruta = f"/preboletas/{pb['id']}/cierre-manual"
    r = esc.e.cliente.post(ruta, json={"observacion": "Ya no entregará"}, headers=esc.comprador["h"])
    assert r.status_code == 409 and r.json()["codigo"] == "cierre_bloqueado"  # sin 2da pesada
    esc.e.actualizar_boleta(b, tara="8300")
    esc.e.procesar(b)
    assert esc.detalle(pb["id"])["preboleta"]["estado"] == "en_proceso"  # múltiples: espera más cargas
    assert esc.e.cliente.post(ruta, json={"observacion": "x"}, headers=esc.comprador2["h"]).status_code == 404
    r = esc.e.cliente.post(ruta, json={"observacion": "Ya no entregará"}, headers=esc.comprador["h"])
    assert r.status_code == 200, r.text
    d = esc.detalle(pb["id"])
    assert d["preboleta"]["estado"] == "terminado_con_incidencias"
    assert {(i["tipo"], i["estado"]) for i in d["incidencias"]} == {
        ("cierre_manual", "registrada"),
        ("faltante", "pendiente"),
    }
    # Una carga nueva la reabre y cancela el cierre a mano y el faltante.
    esc.entrada(pb["id"], 60)
    d = esc.detalle(pb["id"])
    assert d["preboleta"]["estado"] == "en_bascula"
    assert {i["estado"] for i in d["incidencias"]} == {"cancelada"}
