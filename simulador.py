"""Simulador para demostraciones y pruebas de carga.

    python simulador.py historial --dias 20     # llena semanas pasadas (pronóstico y tiempos aprendidos)
    python simulador.py vivo --ritmo 30         # personas llegan y módulos automáticos atienden, en vivo

El modo `vivo` acelera el reloj: --ritmo 30 significa que un minuto simulado dura 2 segundos.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import secrets
from datetime import date, datetime, timedelta

import httpx

from app import db

# Llegadas por hora en un día típico: la punta es al mediodía.
LLEGADAS_HORA = {8: 12, 9: 18, 10: 22, 11: 30, 12: 40, 13: 34, 14: 22, 15: 8}
EQUIPO = [("Ana", "1"), ("Luis", "2"), ("Marta", "3"), ("Jorge", "4")]
MOTIVOS = ["Adulto mayor", "Embarazo", "Discapacidad", "Niño en brazos"]


def duracion_atencion(preferencial: bool) -> float:
    media = 11 if preferencial else 7
    return max(1.5, random.lognormvariate(0, 0.45) * media * 0.9)


def poisson(media: float) -> int:
    """Cantidad de llegadas en un intervalo (proceso de Poisson)."""
    n, t = 0, random.expovariate(1)
    while t < media:
        n += 1
        t += random.expovariate(1)
    return n


def cedula() -> str:
    return str(random.randint(10_000_000, 1_299_999_999))


def historial(ruta: str, dias: int):
    conn = db.conectar(ruta)
    prefijos = {s["id"]: s["prefijo"] for s in conn.execute("SELECT id, prefijo FROM servicios WHERE activo=1")}
    servicios = list(prefijos)
    ids = []
    for nombre, modulo in EQUIPO:
        conn.execute(
            "INSERT OR IGNORE INTO agentes(nombre, modulo, estado, estado_desde) VALUES (?,?,?,?)",
            (nombre, modulo, "desconectado", db.iso(datetime.now())),
        )
        ids.append(conn.execute("SELECT id FROM agentes WHERE nombre=?", (nombre,)).fetchone()[0])

    dia = date.today()
    hechos = 0
    conn.execute("BEGIN")
    while hechos < dias:
        dia -= timedelta(days=1)
        if dia.weekday() >= 5:
            continue
        hechos += 1
        llegadas = []
        for hora, n in LLEGADAS_HORA.items():
            minutos = 30 if hora == 15 else 60
            for _ in range(poisson(n * minutos / 60)):
                llegadas.append(datetime.combine(dia, datetime.min.time()) + timedelta(hours=hora, minutes=random.uniform(0, minutos)))
        llegadas.sort()
        # Cola simple con 4 módulos: el primero que se libera atiende al siguiente.
        libres = [datetime.combine(dia, datetime.min.time()) + timedelta(hours=8)] * len(ids)
        contador: dict[str, int] = {}
        for llegada in llegadas:
            pref = random.random() < 0.22
            servicio = random.choice(servicios)
            i = min(range(len(libres)), key=lambda k: libres[k])
            inicio = max(llegada, libres[i])
            fin = inicio + timedelta(minutes=duracion_atencion(pref))
            libres[i] = fin
            prefijo = ("P" if pref else "") + prefijos[servicio]
            contador[prefijo] = contador.get(prefijo, 0) + 1
            ausente = random.random() < 0.03
            conn.execute(
                "INSERT INTO tickets(codigo, token, cedula, servicio_id, prioridad, motivo, estado, creado_en, "
                "inicio_en, fin_en, agente_id, modulo, llamadas) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (f"{prefijo}-{contador[prefijo]:03d}", secrets.token_urlsafe(8), cedula(), servicio,
                 "preferencial" if pref else "general", random.choice(MOTIVOS) if pref else None,
                 "ausente" if ausente else "finalizado", db.iso(llegada), db.iso(inicio), db.iso(fin),
                 ids[i], EQUIPO[i][1]),
            )
    conn.execute("COMMIT")
    total = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    print(f"Historial listo: {dias} días hábiles, {total} turnos en {ruta}.")


async def vivo(url: str, ritmo: float, modulos: int, minutos: float):
    """Llegan personas según la curva del día y módulos en modo automático las atienden."""
    seg = 60 / ritmo  # segundos reales por minuto simulado
    async with httpx.AsyncClient(base_url=url, timeout=10) as c:
        servicios = [s["id"] for s in (await c.get("/api/servicios")).json()]
        agentes = []
        for nombre, modulo in EQUIPO[:modulos]:
            r = await c.post("/api/agentes/entrar", json={"nombre": nombre, "modulo": modulo})
            if r.status_code != 200:
                print("No pude abrir", nombre, r.json())
                continue
            a = r.json()["id"]
            await c.post(f"/api/agentes/{a}/auto", json={"activo": True})
            agentes.append(a)
        print(f"{len(agentes)} módulos automáticos abiertos. Ctrl+C para detener.")

        async def llegadas():
            fin = asyncio.get_event_loop().time() + minutos * seg
            while asyncio.get_event_loop().time() < fin:
                tasa = LLEGADAS_HORA.get(datetime.now().hour, 20) / 60
                await asyncio.sleep(random.expovariate(tasa) * seg)
                pref = random.random() < 0.22
                datos = {"cedula": cedula(), "servicio_id": random.choice(servicios),
                         "prioridad": "preferencial" if pref else "general",
                         "motivo": random.choice(MOTIVOS) if pref else None}
                r = await c.post("/api/tickets", json=datos)
                if r.status_code == 200:
                    t = r.json()
                    print(f"llega {t['codigo']:>7} · espera estimada {t['espera_min']} min")

        async def modulo(a: int):
            while True:
                estado = (await c.get(f"/api/agentes/{a}")).json()
                actual = estado["actual"]
                if actual:
                    await asyncio.sleep(duracion_atencion(actual["prioridad"] == "preferencial") * seg)
                    await c.post(f"/api/agentes/{a}/finalizar")
                    continue
                rec = estado["recomendacion"]
                if rec and rec["nivel"] in ("sugerida", "urgente"):
                    await c.post(f"/api/agentes/{a}/pausa", json={"tipo": rec["tipo"]})
                    print(f"módulo {estado['agente']['modulo']} sale a {rec['tipo']}")
                    await asyncio.sleep(estado["pausa_minutos"][rec["tipo"]] * seg)
                    await c.post(f"/api/agentes/{a}/volver")
                    continue
                await asyncio.sleep(1)

        tareas = [asyncio.create_task(modulo(a)) for a in agentes]
        try:
            await llegadas()
        finally:
            for t in tareas:
                t.cancel()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="modo", required=True)
    h = sub.add_parser("historial")
    h.add_argument("--db", default="fluye.db")
    h.add_argument("--dias", type=int, default=20)
    v = sub.add_parser("vivo")
    v.add_argument("--url", default="http://localhost:8000")
    v.add_argument("--ritmo", type=float, default=30)
    v.add_argument("--modulos", type=int, default=3)
    v.add_argument("--minutos", type=float, default=240, help="minutos simulados de llegadas")
    a = p.parse_args()
    if a.modo == "historial":
        historial(a.db, a.dias)
    else:
        asyncio.run(vivo(a.url, a.ritmo, a.modulos, a.minutos))


if __name__ == "__main__":
    main()
