"""
Límite de consultas por minuto y por IP a la página pública del QR (no requiere sesión, así
que se protege contra que alguien pruebe tokens al azar). Ventana deslizante en memoria.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class LimitadorPorMinuto:
    def __init__(self, limite: int, reloj: Callable[[], float] = time.monotonic) -> None:
        self._limite = limite
        self._reloj = reloj
        self._golpes: dict[str, deque[float]] = {}
        self._candado = threading.Lock()

    def permitir(self, clave: str) -> bool:
        ahora = self._reloj()
        with self._candado:
            golpes = self._golpes.setdefault(clave, deque())
            while golpes and golpes[0] <= ahora - 60:
                golpes.popleft()
            if len(golpes) >= self._limite:
                return False
            golpes.append(ahora)
            # Limpieza ocasional de IPs inactivas para que el diccionario no crezca sin fin.
            if len(self._golpes) > 10_000:
                for k in [k for k, g in self._golpes.items() if not g or g[-1] <= ahora - 60]:
                    del self._golpes[k]
            return True
