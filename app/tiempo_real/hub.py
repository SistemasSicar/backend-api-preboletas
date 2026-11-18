"""
Reparto de eventos de tiempo real por WebSocket.

La API publica `NOTIFY preboletas_eventos` dentro de sus transacciones; un hilo escucha ese canal
y entrega cada evento aquí (desde otro hilo, por eso `call_soon_threadsafe`). El hub lo manda
solo a quien le corresponde según su rol. Los eventos llevan ids, no datos: el panel vuelve a
pedir lo que necesite por REST.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any

from fastapi import WebSocket

from app.usuario import Usuario

log = logging.getLogger(__name__)

# Eventos que no revelan nada de una preboleta en particular: van a todos.
EVENTOS_GENERALES = frozenset({"slot.actualizado", "precio_diario.actualizado", "unidad.actualizada"})


def puede_recibir(usuario: Usuario, evento: dict[str, Any]) -> bool:
    """Misma visibilidad que la API REST."""
    destinatario = evento.get("destinatario_id")
    if destinatario is not None:
        return bool(destinatario == usuario.colaborador_id)
    if usuario.es_admin:
        return True
    nombre = str(evento.get("evento", ""))
    if nombre in EVENTOS_GENERALES:
        return True
    if usuario.es_basculista and evento.get("unidad_negocio_id") == usuario.unidad_negocio_id:
        return True
    if usuario.es_comprador:
        if evento.get("comprador_id") == usuario.colaborador_id:
            return True
        # Incidencias, conciliaciones y propuestas se ven (y resuelven) entre compradores de todas las unidades.
        if nombre.startswith(("incidencia.", "propuesta.")):
            return True
    return False


class HubTiempoReal:
    def __init__(self) -> None:
        self._conexiones: dict[int, tuple[WebSocket, Usuario]] = {}
        self._ids = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None

    def fijar_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def conectados(self) -> int:
        return len(self._conexiones)

    def registrar(self, ws: WebSocket, usuario: Usuario) -> int:
        clave = next(self._ids)
        self._conexiones[clave] = (ws, usuario)
        return clave

    def quitar(self, clave: int) -> None:
        self._conexiones.pop(clave, None)

    def publicar_desde_hilo(self, carga: str) -> None:
        """Lo llama el hilo de LISTEN: pasa el evento al bucle de asyncio de la API."""
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self.repartir, carga)

    def repartir(self, carga: str) -> None:
        try:
            evento: dict[str, Any] = json.loads(carga)
        except ValueError:
            log.warning("Evento de tiempo real inválido: %s", carga)
            return
        for clave, (ws, usuario) in list(self._conexiones.items()):
            if puede_recibir(usuario, evento):
                asyncio.ensure_future(self._enviar(clave, ws, evento))

    async def _enviar(self, clave: int, ws: WebSocket, evento: dict[str, Any]) -> None:
        try:
            await ws.send_json(evento)
        except Exception:
            # Conexión cerrada a medias: se olvida; el cliente se reconecta solo.
            self.quitar(clave)
