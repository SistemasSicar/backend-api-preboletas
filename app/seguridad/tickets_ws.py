"""
Tickets de corta duración para abrir el WebSocket de tiempo real.

El navegador nunca recibe el token de Microsoft (vive en cookies cifradas del panel).
Por eso el servidor del panel pide aquí un ticket con ese token y se lo pasa al navegador,
que lo manda en el PRIMER mensaje del WebSocket (nunca en la URL, para que no quede en logs).

Los tickets viven en memoria: sirven una sola vez y vencen a los 60 segundos.
Esto supone un solo proceso de la API (uvicorn con 1 worker), que es como se despliega.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable

from app.usuario import Usuario

VIGENCIA_SEGUNDOS = 60


class TicketsWs:
    def __init__(self, reloj: Callable[[], float] = time.monotonic) -> None:
        self._reloj = reloj
        self._tickets: dict[str, tuple[Usuario, float]] = {}
        self._candado = threading.Lock()

    def emitir(self, usuario: Usuario) -> tuple[str, int]:
        """Devuelve (ticket, segundos de vigencia)."""
        ticket = secrets.token_urlsafe(32)
        with self._candado:
            self._limpiar()
            self._tickets[ticket] = (usuario, self._reloj() + VIGENCIA_SEGUNDOS)
        return ticket, VIGENCIA_SEGUNDOS

    def canjear(self, ticket: str) -> Usuario | None:
        """Usuario del ticket si es válido; el ticket se borra al usarlo (un solo uso)."""
        with self._candado:
            dato = self._tickets.pop(ticket, None)
        if dato is None:
            return None
        usuario, vence = dato
        return usuario if self._reloj() <= vence else None

    def _limpiar(self) -> None:
        ahora = self._reloj()
        for t in [t for t, (_, vence) in self._tickets.items() if vence < ahora]:
            del self._tickets[t]
