from datetime import datetime, timedelta

from app import motor
from app.motor import AJUSTES_INICIALES as AJ

AHORA = datetime(2026, 10, 5, 12, 0)


def ticket(id, hace_min, prioridad="general", servicio_id=1):
    return {"id": id, "creado_en": AHORA - timedelta(minutes=hace_min),
            "prioridad": prioridad, "servicio_id": servicio_id}


def agente(id, estado="disponible", **extra):
    base = {"id": id, "estado": estado, "estado_desde": AHORA, "servicios": [],
            "trabajando_desde": AHORA - timedelta(minutes=30), "pausas_hoy": []}
    base.update(extra)
    return base


# --- tiempos aprendidos ---------------------------------------------------

def test_sin_historial_usa_valores_iniciales():
    m = motor.modelo_tiempos([], AJ)
    assert m(1, "general") == 8
    assert m(1, "preferencial") == 12


def test_historial_desplaza_la_estimacion():
    muestras = [{"servicio_id": 1, "prioridad": "general", "minutos": 4}] * 200
    m = motor.modelo_tiempos(muestras, AJ)
    assert 4 <= m(1, "general") < 5


def test_preferencial_aprende_su_propio_tiempo():
    muestras = ([{"servicio_id": 1, "prioridad": "general", "minutos": 6}] * 60
                + [{"servicio_id": 1, "prioridad": "preferencial", "minutos": 15}] * 60)
    m = motor.modelo_tiempos(muestras, AJ)
    assert m(1, "preferencial") > 13
    assert m.factor_preferencial > 2


def test_un_caso_extremo_no_dispara_la_estimacion():
    muestras = [{"servicio_id": 1, "prioridad": "general", "minutos": 8}] * 30
    muestras.append({"servicio_id": 1, "prioridad": "general", "minutos": 300})
    assert motor.modelo_tiempos(muestras, AJ)(1, "general") < 10


# --- orden de la fila -----------------------------------------------------

def test_preferencial_recien_llegado_pasa_antes_que_general():
    fila = [ticket(1, 5), ticket(2, 1, "preferencial")]
    assert [t["id"] for t in motor.ordenar_fila(fila, AHORA, AJ)] == [2, 1]


def test_general_que_espera_mucho_no_queda_atrapado():
    fila = [ticket(1, 40), ticket(2, 1, "preferencial")]
    assert motor.ordenar_fila(fila, AHORA, AJ)[0]["id"] == 1


def test_agente_solo_recibe_servicios_que_atiende():
    fila = [ticket(1, 10, servicio_id=2), ticket(2, 5, servicio_id=1)]
    a = agente(1, servicios=[1])
    assert motor.siguiente_para(a, fila, AHORA, AJ)["id"] == 2


# --- espera estimada ------------------------------------------------------

def test_espera_sube_cuando_alguien_sale_a_almorzar():
    fila = [ticket(i, 10 - i) for i in range(1, 9)]
    m = motor.modelo_tiempos([], AJ)
    cuatro = [agente(i) for i in range(1, 5)]
    tres = cuatro[:3] + [agente(4, "pausa", pausa_tipo="almuerzo")]
    e4 = motor.estimar_esperas(fila, cuatro, AHORA, m, AJ)
    e3 = motor.estimar_esperas(fila, tres, AHORA, m, AJ)
    assert max(e3.values()) > max(e4.values())


def test_espera_cuenta_la_atencion_en_curso():
    m = motor.modelo_tiempos([], AJ)
    ocupado = agente(1, "atendiendo", ticket={
        "servicio_id": 1, "prioridad": "general", "inicio_en": AHORA - timedelta(minutes=3)})
    e = motor.estimar_esperas([ticket(9, 1)], [ocupado], AHORA, m, AJ)
    assert round(e[9]) == 5


def test_sin_modulos_la_espera_es_desconocida():
    m = motor.modelo_tiempos([], AJ)
    e = motor.estimar_esperas([ticket(1, 1)], [agente(1, "desconectado")], AHORA, m, AJ)
    assert e[1] is None


# --- pausas -----------------------------------------------------------------

def test_almuerzo_se_reparte_de_a_uno_y_primero_quien_mas_trabajo():
    agentes = [agente(1, trabajando_desde=AHORA - timedelta(minutes=60)),
               agente(2, trabajando_desde=AHORA - timedelta(minutes=120))]
    rec = motor.recomendar_pausas(agentes, AHORA, en_fila=3, aj=AJ)
    assert rec[2]["nivel"] == "sugerida" and rec[2]["tipo"] == "almuerzo"
    assert rec[1]["nivel"] == "info"


def test_no_sugiere_si_ya_hay_alguien_en_pausa():
    agentes = [agente(1, "pausa", pausa_tipo="almuerzo"),
               agente(2, trabajando_desde=AHORA - timedelta(minutes=100))]
    rec = motor.recomendar_pausas(agentes, AHORA, en_fila=3, aj=AJ)
    assert rec[2]["nivel"] == "info"


def test_cansancio_extremo_se_recomienda_igual():
    agentes = [agente(1, "pausa", pausa_tipo="almuerzo"),
               agente(2, trabajando_desde=AHORA - timedelta(minutes=200), pausas_hoy=["almuerzo"])]
    rec = motor.recomendar_pausas(agentes, AHORA, en_fila=10, aj=AJ)
    assert rec[2]["nivel"] == "urgente"


def test_sin_cansancio_ni_ventana_no_recomienda():
    tarde = datetime(2026, 10, 5, 15, 10)
    a = agente(1, trabajando_desde=tarde - timedelta(minutes=20), pausas_hoy=["almuerzo"])
    assert motor.recomendar_pausas([a], tarde, en_fila=0, aj=AJ) == {}


# --- dotación ---------------------------------------------------------------

def test_erlang_c_valores_conocidos():
    # Tráfico 2 erlangs con 3 agentes: P(espera) ≈ 0.444
    assert abs(motor.erlang_c(2, 3) - 0.4444) < 0.001


def test_hora_pico_pide_mas_modulos():
    dot = motor.dotacion_recomendada({9: 10, 12: 40}, 8, AJ)
    assert dot[12] > dot[9]
    assert motor.nivel_servicio(40, 8, dot[12], 15) >= 0.8


def test_pronostico_usa_mismo_dia_de_la_semana():
    lunes = [datetime(2026, 9, 28, 12, 5), datetime(2026, 9, 21, 12, 10), datetime(2026, 9, 21, 12, 30)]
    martes = [datetime(2026, 9, 29, 9, 0)] * 10
    llegadas = motor.llegadas_por_hora(lunes + martes, AHORA)  # AHORA es lunes
    assert llegadas == {12: 1.5}
