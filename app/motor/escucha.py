"""
Hilos de fondo que hablan con PostgreSQL fuera de las peticiones:

  * EscuchaPg: LISTEN a un canal (core_boletas o preboletas_eventos) en su propia conexión.
    Si la conexión se cae, se reconecta sola con espera creciente.
  * Barrido: cada 5 minutos repasa las boletas de las últimas 48 h por si se perdió algún aviso
    (la API estuvo apagada, se cayó la conexión). Toma el candado de sesión 72300001 para que
    corra UNA sola copia aunque haya varias instancias de la API.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable

from app.bd import BaseDatos
from app.motor.motor_boletas import procesar_boleta

log = logging.getLogger(__name__)

CANAL_BOLETAS = "core_boletas"
CANDADO_BARRIDO = 72300001
INTERVALO_BARRIDO_S = 300
VENTANA_BARRIDO = "48 hours"


class EscuchaPg(threading.Thread):
    def __init__(self, bd: BaseDatos, canal: str, al_recibir: Callable[[str], None], parar: threading.Event) -> None:
        super().__init__(name=f"escucha-{canal}", daemon=True)
        self._bd = bd
        self._canal = canal
        self._al_recibir = al_recibir
        self._parar = parar
        self.conectada = False

    def run(self) -> None:
        espera = 1.0
        while not self._parar.is_set():
            try:
                con = self._bd.conexion_dedicada()
                try:
                    con.execute(f"LISTEN {self._canal}")
                    self.conectada = True
                    espera = 1.0
                    log.info("Escuchando %s", self._canal)
                    while not self._parar.is_set():
                        # timeout corto para poder revisar si hay que parar.
                        for aviso in con.notifies(timeout=1.0):
                            try:
                                self._al_recibir(aviso.payload)
                            except Exception:
                                log.exception("Error procesando un aviso de %s: %s", self._canal, aviso.payload)
                finally:
                    self.conectada = False
                    con.close()
            except Exception as exc:
                log.warning("Escucha de %s caída (%s); reintento en %.0f s", self._canal, exc, espera)
                self._parar.wait(espera)
                espera = min(espera * 2, 60.0)


def procesar_aviso_boleta(bd: BaseDatos) -> Callable[[str], None]:
    """Lo que hace la escucha de core_boletas con cada aviso: pasar la boleta por el motor."""

    def procesar(carga: str) -> None:
        boleta_id = int(json.loads(carga)["boleta_id"])
        with bd.transaccion() as con:
            procesar_boleta(con, boleta_id)

    return procesar


def barrer(bd: BaseDatos) -> int:
    """
    Una pasada del barrido. Devuelve cuántas boletas revisó (0 si otra instancia tiene el candado).
    Cada boleta va en su propia transacción: un error en una no detiene las demás.
    """
    con = bd.conexion_dedicada()
    try:
        fila = con.execute("SELECT pg_try_advisory_lock(%s) AS ok", (CANDADO_BARRIDO,)).fetchone()
        if not fila or not fila["ok"]:
            return 0
        try:
            ids = [
                int(f["id"])
                for f in con.execute(
                    f"SELECT id FROM core.boletas WHERE actualizado_at > now() - interval '{VENTANA_BARRIDO}' "
                    "ORDER BY primera_pesada_at NULLS LAST, id"
                ).fetchall()
            ]
            for boleta_id in ids:
                try:
                    with bd.transaccion() as tx:
                        procesar_boleta(tx, boleta_id)
                except Exception:
                    log.exception("Barrido: error en la boleta %s", boleta_id)
            return len(ids)
        finally:
            con.execute("SELECT pg_advisory_unlock(%s)", (CANDADO_BARRIDO,))
    finally:
        con.close()


class Barrido(threading.Thread):
    def __init__(self, bd: BaseDatos, parar: threading.Event, intervalo: float = INTERVALO_BARRIDO_S) -> None:
        super().__init__(name="barrido-boletas", daemon=True)
        self._bd = bd
        self._parar = parar
        self._intervalo = intervalo

    def run(self) -> None:
        # Primera pasada poco después de arrancar: recupera lo que llegó con la API apagada.
        if self._parar.wait(5):
            return
        while not self._parar.is_set():
            try:
                n = barrer(self._bd)
                log.debug("Barrido: %s boletas revisadas", n)
            except Exception:
                log.exception("Barrido de boletas falló")
            self._parar.wait(self._intervalo)
