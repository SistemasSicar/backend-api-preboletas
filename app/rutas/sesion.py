"""Sesión: quién soy (vincula la cuenta) y el canal de tiempo real."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.dependencias import IdentidadActual, ServiciosDep, UsuarioActual
from app.errores import RESPUESTAS_COMUNES, AccesoDenegado
from app.esquemas.modelos import TicketWs, Yo
from app.servicios.colaboradores import cargar_usuario, perfil

log = logging.getLogger(__name__)
router = APIRouter(tags=["Sesión"])

# Códigos de cierre del WebSocket (rango privado 4000-4999).
WS_SIN_TICKET = 4401


@router.get(
    "/yo",
    response_model=Yo,
    summary="Quién soy (al iniciar sesión y al renovar el token)",
    responses=RESPUESTAS_COMUNES,
)
def yo(identidad: IdentidadActual, servicios: ServiciosDep) -> Yo:
    """
    Lo llama el panel (o la app) al iniciar sesión y al renovar el token. Reconoce al colaborador
    LEYENDO la base (por su cuenta de Microsoft o, si aún no se vincula, por su correo) y devuelve su
    perfil, roles, permisos por unidad y canales de aviso. No depende de CoreAPI: la vinculación y la
    copia de roles se le piden a CoreAPI en segundo plano, sin esperar su respuesta.
    403 `sin_vinculacion` si la cuenta no está dada de alta como colaborador.
    """
    with servicios.bd.transaccion() as con:
        usuario = cargar_usuario(con, identidad)
        if usuario is None:
            raise AccesoDenegado(
                "Tu cuenta no está dada de alta como colaborador de SICAR. Pide tu alta al administrador.",
                codigo="sin_vinculacion",
            )
        respuesta = Yo.model_validate(perfil(con, usuario))
    servicios.sincronizar_cuenta(identidad)
    return respuesta


@router.post("/ws/ticket", response_model=TicketWs, summary="Ticket para abrir el WebSocket de tiempo real")
def ticket_ws(usuario: UsuarioActual, servicios: ServiciosDep) -> TicketWs:
    """
    El servidor del panel lo pide con el token del usuario y se lo pasa al navegador, que lo manda
    en el PRIMER mensaje del WebSocket (`{"ticket": "..."}`). Sirve una vez y vence en 60 s.
    """
    ticket, vence = servicios.tickets.emitir(usuario)
    return TicketWs(ticket=ticket, vence_en_segundos=vence)


@router.websocket("/ws")
async def tiempo_real(ws: WebSocket) -> None:
    """
    Eventos de tiempo real según el rol: `{"evento": "preboleta.actualizada", "preboleta_id": 12, ...}`.
    El cliente vuelve a pedir el detalle por REST. Responde "pong" a "ping".
    """
    servicios = ws.app.state.servicios
    await ws.accept()
    try:
        primero = await asyncio.wait_for(ws.receive_text(), timeout=10)
        ticket = str(json.loads(primero).get("ticket", ""))
    except (TimeoutError, ValueError, AttributeError, WebSocketDisconnect):
        await _cerrar(ws, WS_SIN_TICKET, "Falta el ticket")
        return
    usuario = await run_in_threadpool(servicios.tickets.canjear, ticket)
    if usuario is None:
        await _cerrar(ws, WS_SIN_TICKET, "Ticket inválido o vencido")
        return
    clave = servicios.hub.registrar(ws, usuario)
    await ws.send_json({"evento": "conectado", "colaborador_id": usuario.colaborador_id})
    try:
        while True:
            mensaje = await ws.receive_text()
            if mensaje == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        servicios.hub.quitar(clave)


async def _cerrar(ws: WebSocket, codigo: int, razon: str) -> None:
    try:
        await ws.close(code=codigo, reason=razon)
    except RuntimeError:
        pass
