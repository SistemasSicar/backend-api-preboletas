"""
Sincronización con CoreAPI EN SEGUNDO PLANO.

El login de Preboletas no depende de CoreAPI: la API reconoce al colaborador leyendo `core`
(ver servicios/colaboradores.cargar_usuario). Pero solo CoreAPI puede ESCRIBIR en `core`, y es
quien guarda el vínculo con la cuenta de Microsoft (entra_oid), el nombre y la copia de roles
(que Preboletas usa para saber a qué Admin y basculistas avisar). Por eso, después de cada login
se le pide a CoreAPI que lo haga, sin esperar su respuesta. Si CoreAPI está caída solo queda un
aviso en el log y se vuelve a intentar en el siguiente login.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from app.errores import ErrorApi
from app.integraciones.core_api import VerificadorAcceso
from app.seguridad.token import IdentidadToken

log = logging.getLogger(__name__)


class SincronizadorCore:
    def __init__(self, core: VerificadorAcceso, *, en_segundo_plano: bool = True) -> None:
        """`en_segundo_plano=False` ejecuta en el mismo hilo (lo usan las pruebas)."""
        self._core = core
        self._ejecutor = (
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="sincroniza-core") if en_segundo_plano else None
        )
        self._en_curso: set[str] = set()
        self._candado = threading.Lock()

    def programar(self, identidad: IdentidadToken) -> None:
        """Pide la sincronización de la cuenta. Nunca lanza errores ni hace esperar al login."""
        with self._candado:
            # Si la misma cuenta ya se está sincronizando (varias pestañas a la vez), no se repite.
            if identidad.entra_oid in self._en_curso:
                return
            self._en_curso.add(identidad.entra_oid)
        if self._ejecutor is None:
            self._sincronizar(identidad)
        else:
            self._ejecutor.submit(self._sincronizar, identidad)

    def _sincronizar(self, identidad: IdentidadToken) -> None:
        try:
            self._core.verificar(identidad)
        except ErrorApi as exc:
            log.warning(
                "No se sincronizó la cuenta %s con CoreAPI (%s): %s. Se reintenta en su siguiente login.",
                identidad.entra_oid,
                exc.codigo,
                exc.mensaje,
            )
        except Exception:
            log.exception("Error inesperado al sincronizar la cuenta %s con CoreAPI", identidad.entra_oid)
        finally:
            with self._candado:
                self._en_curso.discard(identidad.entra_oid)

    def cerrar(self) -> None:
        if self._ejecutor is not None:
            self._ejecutor.shutdown(wait=False, cancel_futures=True)
