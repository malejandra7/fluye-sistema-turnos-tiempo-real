"""Motor de decisiones de Fluye.

Funciones puras (sin base de datos ni red): reciben datos y la hora actual,
devuelven decisiones. Así se pueden probar y ajustar sin levantar el servidor.

Principio: ningún número queda fijo. Los valores de `ajustes` solo sirven para
arrancar; en cuanto hay historial, el motor aprende de lo que realmente pasa.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from statistics import mean, median
from typing import Callable

AJUSTES_INICIALES: dict = {
    # Tiempos de atención: solo valores de arranque, luego se aprenden.
    "minutos_atencion_inicial": 8.0,
    "factor_preferencial_inicial": 1.5,
    # Cuántas atenciones reales hacen falta para confiar más en el historial
    # que en el valor inicial (a mitad de camino entre ambos).
    "muestras_confianza": 10,
    "ventana_historial": 60,
    # Un preferencial cuenta como si llevara estos minutos más esperando.
    # Muy alto = prioridad estricta; bajo = los generales no se quedan atrás.
    "bono_preferencial_min": 20,
    # Pausas (minutos esperados de cada una).
    "pausa_minutos": {"desayuno": 15, "almuerzo": 60, "activa": 5},
    "ventana_desayuno": ["08:30", "10:30"],
    "ventana_almuerzo": ["11:00", "15:00"],
    "max_en_pausa": 1,
    "descanso_sugerido_min": 90,
    "descanso_urgente_min": 150,
    # Operación.
    "hora_apertura": "08:00",
    "hora_cierre": "15:30",
    "modo_demo": True,
    "max_llamadas": 3,
    # Meta de servicio para recomendar dotación (Erlang C).
    "meta_espera_min": 15,
    "meta_nivel_servicio": 0.8,
}

TIPOS_PAUSA = ("desayuno", "almuerzo", "activa")


def _hora(texto: str) -> time:
    h, m = texto.split(":")
    return time(int(h), int(m))


def _min_entre(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60


# --------------------------------------------------------------------------
# Tiempos de atención aprendidos
# --------------------------------------------------------------------------

def _encoger(valores: list[float], previo: float, k: float) -> float:
    """Promedio robusto de `valores` acercado a `previo` cuando hay pocos datos.

    Con 0 datos devuelve `previo`; con k datos queda a mitad de camino;
    con muchos, manda el historial.
    """
    if not valores:
        return previo
    med = median(valores)
    recortados = [min(max(v, med / 4), med * 4) for v in valores]
    n = len(recortados)
    return (n * mean(recortados) + k * previo) / (n + k)


@dataclass
class ModeloTiempos:
    general: float
    preferencial: float
    por_clave: dict[tuple[int, str], float]

    def __call__(self, servicio_id: int, prioridad: str) -> float:
        base = self.preferencial if prioridad == "preferencial" else self.general
        return self.por_clave.get((servicio_id, prioridad), base)

    @property
    def factor_preferencial(self) -> float:
        return self.preferencial / self.general if self.general else 1.0


def modelo_tiempos(muestras: list[dict], aj: dict) -> ModeloTiempos:
    """Aprende cuánto dura una atención según el servicio y la prioridad.

    `muestras`: [{servicio_id, prioridad, minutos}], la más reciente al final.
    """
    k = aj["muestras_confianza"]
    muestras = muestras[-aj["ventana_historial"] * 5:]
    gen = [m["minutos"] for m in muestras if m["prioridad"] != "preferencial"]
    pref = [m["minutos"] for m in muestras if m["prioridad"] == "preferencial"]
    general = _encoger(gen[-aj["ventana_historial"]:], aj["minutos_atencion_inicial"], k)
    preferencial = _encoger(
        pref[-aj["ventana_historial"]:], general * aj["factor_preferencial_inicial"], k
    )
    grupos: dict[tuple[int, str], list[float]] = {}
    for m in muestras:
        grupos.setdefault((m["servicio_id"], m["prioridad"]), []).append(m["minutos"])
    por_clave = {}
    for (sid, prio), vals in grupos.items():
        base = preferencial if prio == "preferencial" else general
        por_clave[(sid, prio)] = _encoger(vals[-aj["ventana_historial"]:], base, k)
    return ModeloTiempos(general, preferencial, por_clave)


# --------------------------------------------------------------------------
# Orden de la fila
# --------------------------------------------------------------------------

def puntaje(ticket: dict, ahora: datetime, aj: dict) -> float:
    """Mayor puntaje = se atiende antes. Espera real + bono si es preferencial."""
    espera = _min_entre(ticket["creado_en"], ahora)
    if ticket["prioridad"] == "preferencial":
        espera += aj["bono_preferencial_min"]
    return espera


def ordenar_fila(tickets: list[dict], ahora: datetime, aj: dict) -> list[dict]:
    return sorted(tickets, key=lambda t: (-puntaje(t, ahora, aj), t["creado_en"]))


def puede_atender(agente: dict, ticket: dict) -> bool:
    servicios = agente.get("servicios") or []
    return not servicios or ticket["servicio_id"] in servicios


def siguiente_para(agente: dict, fila: list[dict], ahora: datetime, aj: dict) -> dict | None:
    for t in ordenar_fila(fila, ahora, aj):
        if puede_atender(agente, t):
            return t
    return None


# --------------------------------------------------------------------------
# Capacidad y espera estimada
# --------------------------------------------------------------------------

def libre_desde(agente: dict, ahora: datetime, tiempo: Callable, aj: dict) -> datetime | None:
    """Cuándo queda libre un agente. None si no está trabajando hoy."""
    estado = agente["estado"]
    if estado == "disponible":
        return ahora
    if estado == "atendiendo" and agente.get("ticket"):
        t = agente["ticket"]
        esperado = tiempo(t["servicio_id"], t["prioridad"])
        fin = t["inicio_en"] + timedelta(minutes=esperado)
        # Si ya se pasó del tiempo esperado, suponemos que le falta poco.
        return max(fin, ahora + timedelta(minutes=max(1.0, esperado * 0.2)))
    if estado == "pausa":
        dur = aj["pausa_minutos"].get(agente.get("pausa_tipo") or "activa", 10)
        fin = agente["estado_desde"] + timedelta(minutes=dur)
        return max(fin, ahora + timedelta(minutes=2))
    return None


def estimar_esperas(fila: list[dict], agentes: list[dict], ahora: datetime,
                    tiempo: Callable, aj: dict) -> dict[int, float | None]:
    """Minutos estimados hasta ser llamado, por ticket.

    Reparte la fila (en su orden real) entre los módulos según cuándo se
    libera cada uno. Un módulo en almuerzo cuenta desde que vuelve, así que
    cuando alguien sale a almorzar las esperas suben solas.
    """
    libres = {}
    for a in agentes:
        cuando = libre_desde(a, ahora, tiempo, aj)
        if cuando is not None:
            libres[a["id"]] = cuando
    por_id = {a["id"]: a for a in agentes}
    resultado: dict[int, float | None] = {}
    for t in ordenar_fila(fila, ahora, aj):
        aptos = [i for i in libres if puede_atender(por_id[i], t)]
        if not aptos:
            resultado[t["id"]] = None
            continue
        elegido = min(aptos, key=lambda i: libres[i])
        resultado[t["id"]] = max(0.0, _min_entre(ahora, libres[elegido]))
        libres[elegido] += timedelta(minutes=tiempo(t["servicio_id"], t["prioridad"]))
    return resultado


def resumen_capacidad(agentes: list[dict], tiempo: ModeloTiempos) -> dict:
    conectados = [a for a in agentes if a["estado"] != "desconectado"]
    activos = [a for a in conectados if a["estado"] in ("disponible", "atendiendo")]
    por_hora = len(activos) * 60 / tiempo.general if tiempo.general else 0
    return {
        "activos": len(activos),
        "conectados": len(conectados),
        "en_pausa": len(conectados) - len(activos),
        "personas_hora": round(por_hora, 1),
    }


# --------------------------------------------------------------------------
# Pausas recomendadas
# --------------------------------------------------------------------------

def _en_ventana(ahora: datetime, ventana: list[str]) -> bool:
    return _hora(ventana[0]) <= ahora.time() < _hora(ventana[1])


def _min_hasta(ahora: datetime, hhmm: str) -> float:
    fin = datetime.combine(ahora.date(), _hora(hhmm))
    return _min_entre(ahora, fin)


def recomendar_pausas(agentes: list[dict], ahora: datetime, en_fila: int, aj: dict) -> dict[int, dict]:
    """Sugiere a cada agente si es buen momento para una pausa, y cuál.

    Cada agente trae `trabajando_desde` (fin de su última pausa o inicio de
    jornada) y `pausas_hoy` (tipos de pausa ya tomados hoy).

    Reglas:
    - Nunca más de `max_en_pausa` personas fuera a la vez, salvo urgencias.
    - El turno se reparte con justicia: primero quien lleva más rato sin parar.
    - Si alguien supera `descanso_urgente_min`, se le recomienda aunque haya fila.
    """
    conectados = [a for a in agentes if a["estado"] != "desconectado"]
    en_pausa = sum(1 for a in conectados if a["estado"] == "pausa")
    activos = len(conectados) - en_pausa
    cupos = max(0, aj["max_en_pausa"] - en_pausa)
    fila_tranquila = en_fila <= max(1, activos)

    candidatos = []
    for a in conectados:
        if a["estado"] == "pausa":
            continue
        trabajado = _min_entre(a["trabajando_desde"], ahora)
        hechas = set(a.get("pausas_hoy") or [])
        tipo = None
        apremio = 0.0
        if "almuerzo" not in hechas and _en_ventana(ahora, aj["ventana_almuerzo"]):
            tipo = "almuerzo"
            restante = _min_hasta(ahora, aj["ventana_almuerzo"][1])
            apremio = 1.0 if restante <= aj["pausa_minutos"]["almuerzo"] else 0.5
        elif "desayuno" not in hechas and _en_ventana(ahora, aj["ventana_desayuno"]):
            tipo = "desayuno"
            restante = _min_hasta(ahora, aj["ventana_desayuno"][1])
            apremio = 1.0 if restante <= 20 else 0.5
        elif trabajado >= aj["descanso_sugerido_min"]:
            tipo = "activa"
            apremio = 0.5
        if trabajado >= aj["descanso_urgente_min"]:
            apremio = 2.0
            tipo = tipo or "activa"
        if tipo:
            candidatos.append((a, tipo, trabajado, apremio))

    # Quien lleve más tiempo sin parar (o tenga más apremio) va primero.
    candidatos.sort(key=lambda c: (-c[3], -c[2]))
    recomendaciones: dict[int, dict] = {}
    for posicion, (a, tipo, trabajado, apremio) in enumerate(candidatos):
        nombre = {"almuerzo": "almorzar", "desayuno": "desayunar", "activa": "una pausa activa"}[tipo]
        horas = f"{int(trabajado // 60)} h {int(trabajado % 60)} min"
        if apremio >= 2.0:
            recomendaciones[a["id"]] = {
                "nivel": "urgente", "tipo": tipo,
                "mensaje": f"Llevas {horas} sin parar. Tómate {nombre} ahora; el sistema reparte tu fila.",
            }
        elif posicion < cupos:
            extra = " La fila está tranquila: es buen momento." if fila_tranquila else ""
            recomendaciones[a["id"]] = {
                "nivel": "sugerida", "tipo": tipo,
                "mensaje": f"Te toca {nombre}. Llevas {horas} trabajando.{extra}",
            }
        else:
            antes = _cuantos_antes(posicion - cupos + 1)
            recomendaciones[a["id"]] = {
                "nivel": "info", "tipo": tipo,
                "mensaje": f"Tu turno para {nombre} viene después: {antes}.",
            }
    return recomendaciones


def _cuantos_antes(n: int) -> str:
    return "eres el siguiente" if n == 1 else f"hay {n - 1} antes que tú"


# --------------------------------------------------------------------------
# Pronóstico y dotación recomendada (Erlang C)
# --------------------------------------------------------------------------

def llegadas_por_hora(creaciones: list[datetime], hoy: datetime) -> dict[int, float]:
    """Promedio de llegadas por hora del día.

    Usa los días que coinciden con el día de la semana de hoy si hay al menos
    dos; si no, todos los días con datos.
    """
    if not creaciones:
        return {}
    mismos = [c for c in creaciones if c.weekday() == hoy.weekday() and c.date() != hoy.date()]
    base = mismos if len({c.date() for c in mismos}) >= 2 else creaciones
    dias = len({c.date() for c in base}) or 1
    conteo: dict[int, int] = {}
    for c in base:
        conteo[c.hour] = conteo.get(c.hour, 0) + 1
    return {h: n / dias for h, n in sorted(conteo.items())}


def erlang_c(trafico: float, agentes: int) -> float:
    """Probabilidad de que una persona tenga que esperar (fórmula Erlang C)."""
    if agentes <= trafico:
        return 1.0
    suma = 0.0
    termino = 1.0
    for k in range(agentes):
        if k > 0:
            termino *= trafico / k
        suma += termino
    ultimo = termino * trafico / agentes if agentes else 1.0
    ultimo *= agentes / (agentes - trafico)
    return ultimo / (suma + ultimo)


def nivel_servicio(llegadas_hora: float, aht_min: float, agentes: int, meta_min: float) -> float:
    """Fracción de personas atendidas antes de `meta_min` minutos."""
    trafico = llegadas_hora * aht_min / 60
    if agentes <= trafico:
        return 0.0
    c = erlang_c(trafico, agentes)
    return 1 - c * math.exp(-(agentes - trafico) * meta_min / aht_min)


def dotacion_recomendada(llegadas: dict[int, float], aht_min: float, aj: dict, maximo: int = 30) -> dict[int, int]:
    """Módulos necesarios por hora para cumplir la meta de espera."""
    salida = {}
    for hora, tasa in llegadas.items():
        n = 1
        while n < maximo and nivel_servicio(tasa, aht_min, n, aj["meta_espera_min"]) < aj["meta_nivel_servicio"]:
            n += 1
        salida[hora] = n if tasa > 0 else 0
    return salida


def dentro_de_horario(ahora: datetime, aj: dict) -> bool:
    if aj.get("modo_demo"):
        return True
    return _hora(aj["hora_apertura"]) <= ahora.time() < _hora(aj["hora_cierre"])


def minutos_para_cierre(ahora: datetime, aj: dict) -> float:
    return _min_hasta(ahora, aj["hora_cierre"])
