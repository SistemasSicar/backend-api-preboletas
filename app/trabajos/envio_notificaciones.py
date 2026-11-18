"""
Envío de los avisos por correo y push (los de la campana `app` nacen ya enviados).

Cada 30 segundos toma los pendientes con FOR UPDATE SKIP LOCKED (varias instancias no se pisan):
  * correo: aún no hay buzón de notificaciones (pendiente conocido, CLAUDE.md §9) → `fallida`
    con `correo_no_configurado`.
  * push: a todos los teléfonos activos de la persona, por Expo. Sin teléfonos → `fallida`
    (`sin_dispositivos`). Si Expo falla, se reintenta hasta 5 veces. Un token que Expo da por
    no registrado se da de baja (`token_invalido`).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.bd import BaseDatos, Conexion
from app.integraciones.expo_push import EnviadorPush

log = logging.getLogger(__name__)

INTERVALO_S = 30
MAX_INTENTOS = 5
LOTE = 50
TITULO_PUSH = "Preboletas"


def enviar_pendientes(bd: BaseDatos, push: EnviadorPush) -> int:
    """Una pasada. Devuelve cuántos avisos procesó."""
    with bd.transaccion() as con:
        pendientes = con.execute(
            """
            SELECT id, destinatario_id, canal, mensaje, intentos, preboleta_id, incidencia_id, propuesta_id
              FROM preboletas.notificaciones
             WHERE estado = 'pendiente' AND canal IN ('correo', 'push')
             ORDER BY creado_at
             LIMIT %s
             FOR UPDATE SKIP LOCKED
            """,
            (LOTE,),
        ).fetchall()
        for n in pendientes:
            if n["canal"] == "correo":
                _fallida(con, n["id"], "correo_no_configurado")
            else:
                _enviar_push(con, push, dict(n))
        return len(pendientes)


def _fallida(con: Conexion, notificacion_id: int, error: str) -> None:
    con.execute(
        "UPDATE preboletas.notificaciones SET estado = 'fallida', intentos = intentos + 1, ultimo_error = %s "
        "WHERE id = %s",
        (error, notificacion_id),
    )


def _enviar_push(con: Conexion, push: EnviadorPush, n: dict[str, Any]) -> None:
    tokens = [
        str(f["token"])
        for f in con.execute(
            "SELECT token FROM preboletas.dispositivos_push WHERE colaborador_id = %s AND activo",
            (n["destinatario_id"],),
        ).fetchall()
    ]
    if not tokens:
        _fallida(con, n["id"], "sin_dispositivos")
        return
    datos = {k: n[k] for k in ("preboleta_id", "incidencia_id", "propuesta_id") if n[k] is not None}
    resultados = push.enviar(tokens, TITULO_PUSH, n["mensaje"], datos)
    for r in resultados:
        if r.token_invalido:
            con.execute(
                "UPDATE preboletas.dispositivos_push SET activo = false, desactivado_at = now(), "
                "motivo_baja = 'token_invalido' WHERE token = %s AND activo",
                (r.token,),
            )
    if any(r.ok for r in resultados):
        con.execute(
            "UPDATE preboletas.notificaciones SET estado = 'enviada', enviada_at = now(), intentos = intentos + 1, "
            "ultimo_error = NULL WHERE id = %s",
            (n["id"],),
        )
        return
    error = next((r.error for r in resultados if r.error), "error_expo") or "error_expo"
    if all(r.token_invalido for r in resultados):
        # Ya no queda ningún teléfono válido: no tiene caso reintentar.
        _fallida(con, n["id"], "sin_dispositivos")
    elif n["intentos"] + 1 >= MAX_INTENTOS:
        _fallida(con, n["id"], error)
    else:
        con.execute(
            "UPDATE preboletas.notificaciones SET intentos = intentos + 1, ultimo_error = %s WHERE id = %s",
            (error, n["id"]),
        )


class EnvioNotificaciones(threading.Thread):
    def __init__(self, bd: BaseDatos, push: EnviadorPush, parar: threading.Event) -> None:
        super().__init__(name="envio-notificaciones", daemon=True)
        self._bd = bd
        self._push = push
        self._parar = parar

    def run(self) -> None:
        while not self._parar.wait(INTERVALO_S):
            try:
                enviar_pendientes(self._bd, self._push)
            except Exception:
                log.exception("Envío de notificaciones falló")
