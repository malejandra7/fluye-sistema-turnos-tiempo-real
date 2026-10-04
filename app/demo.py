"""Modo demo pública (FLUYE_DEMO=1).

Al arrancar carga semanas de historial y pone dos módulos simulados a atender,
para que quien entre a la demo vea el sistema en movimiento. Las visitas pueden
pedir turnos en el kiosco y atender desde su propio módulo.
"""
from __future__ import annotations

import asyncio
import logging
import random
import sys
from pathlib import Path

from .central import Central, ErrorTurno

log = logging.getLogger("fluye.demo")
RITMO = 3.0          # un minuto simulado dura 20 segundos
LLEGADAS_HORA = 40   # personas por hora simulada
FILA_INICIAL = 5     # gente esperando desde el primer segundo
TOPE_FILA = 8        # no deja crecer la fila sin fin
MODULOS = [("Ana", "1"), ("Luis", "2")]
MOTIVOS = ["Adulto mayor", "Embarazo", "Discapacidad", "Niño en brazos"]


def preparar(central: Central, ruta_db: str):
    if central.conn.execute("SELECT 1 FROM tickets LIMIT 1").fetchone():
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import simulador
    simulador.historial(ruta_db, 20)


def _duracion(preferencial: bool) -> float:
    media = 11 if preferencial else 7
    return max(1.5, random.lognormvariate(0, 0.45) * media * 0.9)


async def _seguro(coro):
    try:
        return await coro
    except ErrorTurno:
        return None


async def _llega_alguien(central: Central):
    servicios = [s["id"] for s in central.servicios()]
    pref = random.random() < 0.22
    await _seguro(central.crear_ticket(
        str(random.randint(10_000_000, 1_299_999_999)), random.choice(servicios),
        "preferencial" if pref else "general", random.choice(MOTIVOS) if pref else None))


async def _llegadas(central: Central, seg: float):
    while True:
        await asyncio.sleep(random.expovariate(LLEGADAS_HORA / 60) * seg)
        if len(central._fila_hoy()) < TOPE_FILA:
            await _llega_alguien(central)


async def _modulo(central: Central, agente_id: int, seg: float):
    while True:
        estado = central.estado_agente(agente_id)
        if estado["agente"]["estado"] == "desconectado":
            await _seguro(central.entrar(estado["agente"]["nombre"], estado["agente"]["modulo"]))
            await _seguro(central.modo_auto(agente_id, True))
        actual = estado["actual"]
        if actual:
            await asyncio.sleep(_duracion(actual["prioridad"] == "preferencial") * seg)
            await _seguro(central.finalizar(agente_id))
            continue
        rec = estado["recomendacion"]
        if rec and rec["nivel"] in ("sugerida", "urgente"):
            await _seguro(central.pausa(agente_id, rec["tipo"]))
            await asyncio.sleep(estado["pausa_minutos"][rec["tipo"]] * seg)
            await _seguro(central.volver(agente_id))
            continue
        await asyncio.sleep(1)


async def ciclo(central: Central):
    seg = 60 / RITMO
    tareas = []
    for nombre, modulo in MODULOS:
        agente_id = await _seguro(central.entrar(nombre, modulo))
        if agente_id:
            await _seguro(central.modo_auto(agente_id, True))
            tareas.append(lambda a=agente_id: _modulo(central, a, seg))
    tareas.append(lambda: _llegadas(central, seg))
    for _ in range(FILA_INICIAL):
        await _llega_alguien(central)
    while True:
        try:
            async with asyncio.TaskGroup() as grupo:  # si una falla, se cancelan todas
                for crear in tareas:
                    grupo.create_task(crear())
        except asyncio.CancelledError:
            raise
        except Exception:  # la demo nunca debe tumbar el servidor
            log.exception("La simulación de la demo falló; se reinicia en 5 s.")
            await asyncio.sleep(5)
