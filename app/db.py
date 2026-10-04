"""Almacenamiento en SQLite. Un solo archivo, sin servidor de base de datos."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .motor import AJUSTES_INICIALES

ESQUEMA = """
CREATE TABLE IF NOT EXISTS servicios (
    id INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL,
    prefijo TEXT NOT NULL,
    activo INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS agentes (
    id INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL UNIQUE,
    modulo TEXT NOT NULL,
    estado TEXT NOT NULL DEFAULT 'desconectado',
    pausa_tipo TEXT,
    estado_desde TEXT NOT NULL,
    trabajando_desde TEXT,
    modo_auto INTEGER NOT NULL DEFAULT 0,
    servicios TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    codigo TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    cedula TEXT NOT NULL,
    nombre TEXT,
    servicio_id INTEGER NOT NULL REFERENCES servicios(id),
    prioridad TEXT NOT NULL DEFAULT 'general',
    motivo TEXT,
    estado TEXT NOT NULL DEFAULT 'espera',
    creado_en TEXT NOT NULL,
    inicio_en TEXT,
    fin_en TEXT,
    agente_id INTEGER REFERENCES agentes(id),
    modulo TEXT,
    llamadas INTEGER NOT NULL DEFAULT 0,
    transferido_de INTEGER
);
CREATE INDEX IF NOT EXISTS ix_tickets_estado ON tickets(estado, creado_en);
CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    tipo TEXT NOT NULL,
    agente_id INTEGER,
    ticket_id INTEGER,
    detalle TEXT
);
CREATE INDEX IF NOT EXISTS ix_eventos_ts ON eventos(ts);
CREATE TABLE IF NOT EXISTS ajustes (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
"""

SERVICIOS_INICIALES = [
    ("Trámites generales", "A"),
    ("Pagos", "B"),
    ("Información", "C"),
]


def conectar(ruta: str) -> sqlite3.Connection:
    conn = sqlite3.connect(ruta, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(ESQUEMA)
    if not conn.execute("SELECT 1 FROM servicios LIMIT 1").fetchone():
        conn.executemany("INSERT INTO servicios(nombre, prefijo) VALUES (?, ?)", SERVICIOS_INICIALES)
    return conn


def leer_ajustes(conn: sqlite3.Connection) -> dict:
    aj = dict(AJUSTES_INICIALES)
    for fila in conn.execute("SELECT clave, valor FROM ajustes"):
        if fila["clave"] in aj:
            aj[fila["clave"]] = json.loads(fila["valor"])
    return aj


def guardar_ajustes(conn: sqlite3.Connection, cambios: dict) -> None:
    for clave, valor in cambios.items():
        if clave in AJUSTES_INICIALES:
            conn.execute(
                "INSERT INTO ajustes(clave, valor) VALUES (?, ?) "
                "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
                (clave, json.dumps(valor)),
            )


def fecha(texto: str | None) -> datetime | None:
    return datetime.fromisoformat(texto) if texto else None


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")
