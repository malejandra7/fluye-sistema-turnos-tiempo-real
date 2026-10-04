from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.central import Central
from app.main import crear_app


class Reloj:
    def __init__(self):
        self.ahora = datetime(2026, 10, 5, 9, 0)

    def __call__(self):
        return self.ahora

    def avanzar(self, minutos):
        self.ahora += timedelta(minutes=minutos)


@pytest.fixture
def entorno(tmp_path):
    reloj = Reloj()
    central = Central(str(tmp_path / "t.db"), reloj=reloj)
    return TestClient(crear_app(central=central)), reloj


def turno(c, cedula, prioridad="general", servicio_id=1):
    r = c.post("/api/tickets", json={"cedula": cedula, "servicio_id": servicio_id, "prioridad": prioridad})
    assert r.status_code == 200, r.text
    return r.json()


def entrar(c, nombre, modulo, **extra):
    return c.post("/api/agentes/entrar", json={"nombre": nombre, "modulo": modulo, **extra}).json()["id"]


def test_flujo_completo_mide_el_tiempo_desde_que_se_toma(entorno):
    c, reloj = entorno
    t = turno(c, "51001")
    assert t["codigo"] == "A-001"
    a = entrar(c, "Ana", "1")
    c.post(f"/api/agentes/{a}/tomar", json={})
    reloj.avanzar(6)
    assert c.post(f"/api/agentes/{a}/finalizar").status_code == 200
    panel = c.get("/api/panel").json()
    assert panel["kpis"]["atendidos"] == 1
    assert panel["kpis"]["atencion_promedio_min"] == 6.0
    assert c.get(f"/api/tickets/{t['token']}").json()["estado"] == "finalizado"


def test_dos_modulos_no_pueden_tomar_el_mismo_turno(entorno):
    c, _ = entorno
    t = turno(c, "51001")
    a, b = entrar(c, "Ana", "1"), entrar(c, "Beto", "2")
    fila = c.get(f"/api/agentes/{a}").json()["fila"]
    assert c.post(f"/api/agentes/{a}/tomar", json={"ticket_id": fila[0]["id"]}).status_code == 200
    r = c.post(f"/api/agentes/{b}/tomar", json={"ticket_id": fila[0]["id"]})
    assert r.status_code == 409 and "otro módulo" in r.json()["error"]
    # Y deja de aparecerle a los demás:
    assert c.get(f"/api/agentes/{b}").json()["fila"] == []


def test_modo_automatico_asigna_al_llegar_y_al_finalizar(entorno):
    c, _ = entorno
    a = entrar(c, "Ana", "1")
    c.post(f"/api/agentes/{a}/auto", json={"activo": True})
    primero = turno(c, "51001")
    assert primero["espera_min"] == 0 and "módulo 1" in primero["aviso"]
    turno(c, "51002")
    assert c.get(f"/api/agentes/{a}").json()["actual"]["codigo"] == "A-001"
    c.post(f"/api/agentes/{a}/finalizar")
    assert c.get(f"/api/agentes/{a}").json()["actual"]["codigo"] == "A-002"


def test_preferencial_pasa_primero_y_tarda_mas_en_la_estimacion(entorno):
    c, _ = entorno
    turno(c, "51001")
    turno(c, "51002", "preferencial")
    a = entrar(c, "Ana", "1")
    estado = c.get(f"/api/agentes/{a}").json()
    assert estado["fila"][0]["codigo"] == "PA-001"
    assert estado["fila"][0]["esperado_min"] > estado["fila"][1]["esperado_min"]


def test_almuerzo_baja_capacidad_y_sube_la_espera(entorno):
    c, _ = entorno
    a, b = entrar(c, "Ana", "1"), entrar(c, "Beto", "2")
    for i in range(6):
        turno(c, f"5200{i}")
    antes = c.get("/api/publico").json()
    c.post(f"/api/agentes/{b}/pausa", json={"tipo": "almuerzo"})
    despues = c.get("/api/publico").json()
    assert despues["capacidad"]["activos"] == 1
    assert despues["espera_nuevo_min"] > antes["espera_nuevo_min"]
    # Los compañeros ven la capacidad pero no quién está en pausa.
    equipo = c.get(f"/api/agentes/{a}").json()["equipo"]
    assert equipo == {**equipo, "activos": 1, "en_pausa": 1}
    assert "nombre" not in str(equipo)


def test_no_se_puede_salir_a_pausa_atendiendo(entorno):
    c, _ = entorno
    turno(c, "51001")
    a = entrar(c, "Ana", "1")
    c.post(f"/api/agentes/{a}/tomar", json={})
    assert c.post(f"/api/agentes/{a}/pausa", json={"tipo": "activa"}).status_code == 409


def test_cedula_invalida_o_repetida(entorno):
    c, _ = entorno
    assert c.post("/api/tickets", json={"cedula": "12a", "servicio_id": 1}).status_code == 409
    turno(c, "51001")
    r = c.post("/api/tickets", json={"cedula": "51001", "servicio_id": 1})
    assert r.status_code == 409 and "A-001" in r.json()["error"]


def test_devolver_y_transferir_conserva_el_lugar(entorno):
    c, _ = entorno
    turno(c, "51001")
    turno(c, "51002")
    a = entrar(c, "Ana", "1")
    c.post(f"/api/agentes/{a}/tomar", json={})
    c.post(f"/api/agentes/{a}/devolver", json={"servicio_id": 2})
    fila = c.get(f"/api/agentes/{a}").json()["fila"]
    assert fila[0]["codigo"] == "A-001" and fila[0]["servicio_id"] == 2


def test_recomienda_pausa_tras_trabajar_mucho(entorno):
    c, reloj = entorno
    reloj.ahora = datetime(2026, 10, 5, 15, 0)
    a = entrar(c, "Ana", "1")
    c.post(f"/api/agentes/{a}/pausa", json={"tipo": "almuerzo"})
    c.post(f"/api/agentes/{a}/volver")
    reloj.avanzar(100)
    rec = c.get(f"/api/agentes/{a}").json()["recomendacion"]
    assert rec["tipo"] == "activa"


def test_ajustes_editables_cambian_el_calculo(entorno):
    c, _ = entorno
    entrar(c, "Ana", "1")
    turno(c, "51001")
    antes = c.get("/api/publico").json()["espera_nuevo_min"]
    c.put("/api/ajustes", json={"minutos_atencion_inicial": 20})
    assert c.get("/api/publico").json()["espera_nuevo_min"] > antes


def test_historial_csv(entorno):
    c, _ = entorno
    turno(c, "51001")
    r = c.get("/api/historial.csv", params={"desde": "2026-10-01", "hasta": "2026-10-06"})
    assert r.text.splitlines()[1].startswith("A-001,51001")


def test_qr_y_paginas(entorno):
    c, _ = entorno
    t = turno(c, "51001")
    assert c.get(f"/api/qr/{t['token']}.svg").text.startswith("<?xml")
    for ruta in ("/", "/kiosco", "/modulo", "/pantalla", "/panel", f"/t/{t['token']}"):
        assert c.get(ruta).status_code == 200, ruta


def test_tiempo_real_avisa_llamados(entorno):
    c, _ = entorno
    turno(c, "51001")
    a = entrar(c, "Ana", "1")
    with c.websocket_connect("/ws") as ws:
        c.post(f"/api/agentes/{a}/tomar", json={})
        assert ws.receive_json() == {"t": "llamado", "codigo": "A-001", "modulo": "1"}
