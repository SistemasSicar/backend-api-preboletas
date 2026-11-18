"""
Acceso a PostgreSQL con un pool de conexiones (psycopg 3).

Se usa la versión SÍNCRONA de psycopg, igual que CoreAPI: FastAPI ejecuta las rutas
`def` en un grupo de hilos, el volumen es bajo y así se evitan los problemas del
bucle de eventos de asyncio en Windows. Las escuchas (LISTEN) corren en hilos propios.

Cada operación corre dentro de una transacción: `with bd.transaccion() as con:`
confirma al salir sin errores y revierte si ocurre una excepción.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

Conexion = Connection[DictRow]
NOMBRE_APLICACION = "preboletas-api"


class BaseDatos:
    def __init__(self, url: str, minimo: int = 1, maximo: int = 10, *, rol: str | None = None) -> None:
        """
        `rol`: si se indica, cada conexión hace `SET ROLE` al arrancar. Lo usan las pruebas
        para conectarse como administrador pero operar con los permisos reales de preboletas_api.
        """
        self._url = url
        self._kwargs: dict[str, Any] = {"row_factory": dict_row, "application_name": NOMBRE_APLICACION}
        if rol:
            self._kwargs["options"] = f"-c role={rol}"
        self._pool: ConnectionPool[Conexion] = ConnectionPool(
            url,
            min_size=minimo,
            max_size=maximo,
            open=False,
            kwargs=self._kwargs,
            # Tiempo máximo esperando una conexión libre antes de fallar (503).
            timeout=10,
            name="preboletas",
        )

    def abrir(self) -> None:
        # wait=False: la API arranca aunque la base no esté lista; /salud lo reporta.
        self._pool.open(wait=False)

    def cerrar(self) -> None:
        self._pool.close()

    @contextmanager
    def transaccion(self) -> Iterator[Conexion]:
        """Conexión con transacción: commit al terminar, rollback si hay excepción."""
        with self._pool.connection() as con:
            yield con

    def conexion_dedicada(self) -> Conexion:
        """
        Conexión fuera del pool, en autocommit. La usan los hilos que escuchan avisos
        (LISTEN) o que toman candados de sesión; el que la pide la cierra.
        """
        con: Conexion = psycopg.connect(self._url, autocommit=True, **self._kwargs)
        return con

    def disponible(self) -> bool:
        """True si la base responde (para /salud)."""
        try:
            with self.transaccion() as con:
                con.execute("SELECT 1")
            return True
        except Exception:
            log.exception("La base de datos no respondió")
            return False
