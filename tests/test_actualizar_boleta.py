"""POST /recepciones/{id}/actualizar-boleta: releer en D365 (vía CoreAPI) una boleta con cambios perdidos."""

from __future__ import annotations

from typing import Any

from tests.conftest import YUC, Escenario


def _url(recepcion_id: int) -> str:
    return f"/recepciones/{recepcion_id}/actualizar-boleta"


def _carga_ligada(esc: Escenario) -> tuple[dict[str, Any], int, int, str]:
    """Preboleta con una carga ya ligada a su boleta (1ra pesada), sin 2da pesada."""
    pb = esc.crear_preboleta()
    rec = esc.entrada(pb["id"], 100)
    b = esc.e.boleta(YUC, "MP2520", bruto="9400")
    esc.e.procesar(b)
    folio = str(esc.e.uno("SELECT folio FROM core.boletas WHERE id = %s", b)["folio"])
    return pb, rec, b, folio


def test_recupera_la_segunda_pesada_perdida(esc: Escenario) -> None:
    pb, rec, b, folio = _carga_ligada(esc)

    # La 2da pesada ocurrió en D365, pero el aviso se perdió: CoreAPI la recupera al pedírsela.
    def core_relee(f: str) -> str:
        assert f == folio
        esc.e.actualizar_boleta(b, tara="7600")
        return "actualizada"

    esc.e.core.al_actualizar = core_relee
    r = esc.e.cliente.post(_url(rec), headers=esc.basculista["h"])
    assert r.status_code == 200, r.text
    assert r.json() == {
        "folio": folio,
        "resultado": "actualizada",
        "mensaje": f"Boleta {folio} actualizada con los datos de D365.",
    }
    # El motor la procesó al momento (sin esperar el aviso): la preboleta se cerró.
    assert esc.detalle(pb["id"])["preboleta"]["estado"] == "terminado"


def test_sin_cambios_en_core_igual_pasa_por_el_motor(esc: Escenario) -> None:
    """CoreAPI ya la tenía al día, pero esta API no procesó el aviso (estaba caída)."""
    pb, rec, b, folio = _carga_ligada(esc)
    esc.e.actualizar_boleta(b, tara="7600")  # Cambio en core sin pasar por el motor.
    assert esc.detalle(pb["id"])["preboleta"]["estado"] == "en_proceso"

    r = esc.e.cliente.post(_url(rec), headers=esc.comprador["h"])
    assert r.status_code == 200, r.text
    assert r.json()["mensaje"] == f"La boleta {folio} ya estaba al día con D365."
    assert esc.detalle(pb["id"])["preboleta"]["estado"] == "terminado"


def test_visibilidad(esc: Escenario) -> None:
    _, rec, _, _ = _carga_ligada(esc)
    # El basculista de otra unidad y otro comprador no ven la preboleta: 404.
    assert esc.e.cliente.post(_url(rec), headers=esc.basculista_tec["h"]).status_code == 404
    assert esc.e.cliente.post(_url(rec), headers=esc.comprador2["h"]).status_code == 404
    assert esc.e.cliente.post(_url(999999), headers=esc.admin["h"]).status_code == 404
    assert esc.e.core.boletas_actualizadas == []


def test_carga_sin_boleta(esc: Escenario) -> None:
    pb = esc.crear_preboleta()
    rec = esc.entrada(pb["id"], 100)  # En báscula, sin 1ra pesada.
    r = esc.e.cliente.post(_url(rec), headers=esc.basculista["h"])
    assert r.status_code == 400
    assert r.json()["codigo"] == "carga_sin_boleta"


def test_core_caido(esc: Escenario) -> None:
    _, rec, _, _ = _carga_ligada(esc)
    esc.e.core.caido = True
    r = esc.e.cliente.post(_url(rec), headers=esc.basculista["h"])
    assert r.status_code == 503
    assert r.json()["codigo"] == "core_no_disponible"
