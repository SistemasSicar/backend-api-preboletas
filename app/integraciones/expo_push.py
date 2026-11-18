"""
Envío de notificaciones push con el servicio de Expo (la app móvil usa Expo).

Expo responde un "ticket" por mensaje: `ok`, o `error` con un detalle. `DeviceNotRegistered`
significa que el token ya no sirve (app desinstalada o sesión cerrada): el dispositivo se da de baja.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

URL_EXPO = "https://exp.host/--/api/v2/push/send"

# Canal de Android con importancia alta que crea la app móvil (`defaultChannel` en su `app.config.ts`).
# Deben coincidir: si el canal no existe en el teléfono, Android usa el genérico y el aviso llega sin sonar.
CANAL_ANDROID = "avisos"


@dataclass(frozen=True)
class ResultadoPush:
    token: str
    ok: bool
    error: str | None = None

    @property
    def token_invalido(self) -> bool:
        return self.error == "DeviceNotRegistered"


class EnviadorPush(Protocol):
    def enviar(self, tokens: list[str], titulo: str, cuerpo: str, datos: dict[str, Any]) -> list[ResultadoPush]: ...


class ExpoPush:
    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def enviar(self, tokens: list[str], titulo: str, cuerpo: str, datos: dict[str, Any]) -> list[ResultadoPush]:
        """Un mensaje por token. Si Expo no responde, todos quedan con error (se reintentan después)."""
        mensajes = [
            {
                "to": t,
                "title": titulo,
                "body": cuerpo,
                "data": datos,
                "sound": "default",
                "channelId": CANAL_ANDROID,
                # Alta prioridad para que llegue aunque el teléfono esté en reposo (Doze).
                "priority": "high",
            }
            for t in tokens
        ]
        try:
            r = httpx.post(URL_EXPO, json=mensajes, timeout=self._timeout)
            r.raise_for_status()
            tickets: list[dict[str, Any]] = r.json().get("data", [])
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Expo no respondió: %s", exc)
            return [ResultadoPush(t, ok=False, error="expo_no_disponible") for t in tokens]

        resultados: list[ResultadoPush] = []
        for token, ticket in zip(tokens, tickets, strict=False):
            if ticket.get("status") == "ok":
                resultados.append(ResultadoPush(token, ok=True))
            else:
                detalle = (ticket.get("details") or {}).get("error") or ticket.get("message") or "error_expo"
                resultados.append(ResultadoPush(token, ok=False, error=str(detalle)))
        # Si Expo devolvió menos tickets que mensajes, los faltantes se reintentan.
        for token in tokens[len(tickets) :]:
            resultados.append(ResultadoPush(token, ok=False, error="sin_respuesta"))
        return resultados
