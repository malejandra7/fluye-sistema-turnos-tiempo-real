"""Central de turnos: todas las operaciones que cambian el estado.

Cada operación corre bajo un mismo candado, así dos módulos nunca pueden
tomar el mismo turno. Después de cada cambio se avisa a todas las pantallas.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from . import db, motor


class ErrorTurno(Exception):
    """Error de negocio con un mensaje listo para mostrar."""


EN_CURSO = ("espera", "atendiendo")


class Central:
    def __init__(self, ruta_db: str, reloj: Callable[[], datetime] = datetime.now):
        self.conn = db.conectar(ruta_db)
        self.reloj = reloj
        self.candado = asyncio.Lock()
        self.avisar: Callable[[dict], Awaitable[None]] | None = None

    # ------------------------------------------------------------------
    # utilidades
    # ------------------------------------------------------------------

    def ahora(self) -> datetime:
        return self.reloj().replace(microsecond=0)

    def _hoy(self) -> str:
        return self.ahora().date().isoformat()

    def _evento(self, tipo: str, agente_id=None, ticket_id=None, detalle=None):
        self.conn.execute(
            "INSERT INTO eventos(ts, tipo, agente_id, ticket_id, detalle) VALUES (?,?,?,?,?)",
            (db.iso(self.ahora()), tipo, agente_id, ticket_id, detalle),
        )

    async def _notificar(self, mensaje: dict | None = None):
        if self.avisar:
            await self.avisar(mensaje or {"t": "cambio"})

    def ajustes(self) -> dict:
        return db.leer_ajustes(self.conn)

    def _agente(self, agente_id: int):
        fila = self.conn.execute("SELECT * FROM agentes WHERE id=?", (agente_id,)).fetchone()
        if not fila:
            raise ErrorTurno("Ese módulo no existe. Vuelve a entrar.")
        return fila

    def _ticket_actual(self, agente_id: int):
        return self.conn.execute(
            "SELECT * FROM tickets WHERE agente_id=? AND estado='atendiendo'", (agente_id,)
        ).fetchone()

    def _fila_hoy(self) -> list[dict]:
        filas = self.conn.execute(
            "SELECT * FROM tickets WHERE estado='espera' AND creado_en >= ? ORDER BY creado_en",
            (self._hoy(),),
        ).fetchall()
        return [self._ticket_dict(f) for f in filas]

    @staticmethod
    def _ticket_dict(f) -> dict:
        d = dict(f)
        for campo in ("creado_en", "inicio_en", "fin_en"):
            d[campo] = db.fecha(d[campo])
        return d

    def _agentes_motor(self) -> list[dict]:
        """Agentes en el formato que espera el motor, con su atención en curso."""
        hoy = self._hoy()
        pausas: dict[int, list[str]] = {}
        for e in self.conn.execute(
            "SELECT agente_id, detalle FROM eventos WHERE tipo='pausa_inicio' AND ts >= ?", (hoy,)
        ):
            pausas.setdefault(e["agente_id"], []).append(e["detalle"])
        salida = []
        for a in self.conn.execute("SELECT * FROM agentes ORDER BY modulo"):
            d = dict(a)
            d["servicios"] = json.loads(d["servicios"])
            d["estado_desde"] = db.fecha(d["estado_desde"])
            d["trabajando_desde"] = db.fecha(d["trabajando_desde"]) or d["estado_desde"]
            d["pausas_hoy"] = pausas.get(a["id"], [])
            actual = self._ticket_actual(a["id"])
            d["ticket"] = self._ticket_dict(actual) if actual else None
            salida.append(d)
        return salida

    def modelo(self) -> motor.ModeloTiempos:
        aj = self.ajustes()
        filas = self.conn.execute(
            "SELECT servicio_id, prioridad, inicio_en, fin_en FROM tickets "
            "WHERE estado='finalizado' AND inicio_en IS NOT NULL ORDER BY fin_en DESC LIMIT ?",
            (aj["ventana_historial"] * 5,),
        ).fetchall()
        muestras = [
            {"servicio_id": f["servicio_id"], "prioridad": f["prioridad"],
             "minutos": (db.fecha(f["fin_en"]) - db.fecha(f["inicio_en"])).total_seconds() / 60}
            for f in reversed(filas)
        ]
        return motor.modelo_tiempos(muestras, aj)

    # ------------------------------------------------------------------
    # kiosco
    # ------------------------------------------------------------------

    async def crear_ticket(self, cedula: str, servicio_id: int, prioridad: str = "general",
                           motivo: str | None = None, nombre: str | None = None) -> dict:
        cedula = re.sub(r"[\s.\-]", "", cedula or "")
        if not re.fullmatch(r"\d{5,12}", cedula):
            raise ErrorTurno("La cédula debe tener entre 5 y 12 números.")
        if prioridad not in ("general", "preferencial"):
            raise ErrorTurno("Prioridad no válida.")
        async with self.candado:
            aj = self.ajustes()
            ahora = self.ahora()
            if not motor.dentro_de_horario(ahora, aj):
                raise ErrorTurno(f"Atendemos de {aj['hora_apertura']} a {aj['hora_cierre']}.")
            servicio = self.conn.execute(
                "SELECT * FROM servicios WHERE id=? AND activo=1", (servicio_id,)
            ).fetchone()
            if not servicio:
                raise ErrorTurno("Ese servicio no está disponible.")
            previo = self.conn.execute(
                "SELECT codigo FROM tickets WHERE cedula=? AND estado IN ('espera','atendiendo') "
                "AND creado_en >= ?", (cedula, self._hoy()),
            ).fetchone()
            if previo:
                raise ErrorTurno(f"Esta cédula ya tiene el turno {previo['codigo']} activo.")
            prefijo = ("P" if prioridad == "preferencial" else "") + servicio["prefijo"]
            n = self.conn.execute(
                "SELECT COUNT(*) FROM tickets WHERE codigo LIKE ? AND creado_en >= ?",
                (f"{prefijo}-%", self._hoy()),
            ).fetchone()[0] + 1
            codigo = f"{prefijo}-{n:03d}"
            token = secrets.token_urlsafe(8)
            cur = self.conn.execute(
                "INSERT INTO tickets(codigo, token, cedula, nombre, servicio_id, prioridad, motivo, creado_en) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (codigo, token, cedula, (nombre or "").strip() or None, servicio_id, prioridad,
                 motivo if prioridad == "preferencial" else None, db.iso(ahora)),
            )
            self._evento("ticket", ticket_id=cur.lastrowid, detalle=prioridad)
            llamados = await self._despachar()
        for ll in llamados:
            await self._notificar(ll)
        await self._notificar()
        seguimiento = self.seguimiento(token)
        cierre = motor.minutos_para_cierre(self.ahora(), aj)
        aviso = None
        if seguimiento["estado"] == "atendiendo":
            # Un módulo automático lo tomó en el acto.
            seguimiento["espera_min"] = 0
            seguimiento["posicion"] = 1
            aviso = f"¡Pasa ya al módulo {seguimiento['modulo']}!"
        elif seguimiento["espera_min"] is None:
            aviso = "En este momento no hay módulos atendiendo; te llamaremos apenas se abra uno."
        elif not aj["modo_demo"] and seguimiento["espera_min"] > cierre:
            aviso = "La espera estimada pasa la hora de cierre: puede que no alcancemos a atenderte hoy."
        return {**seguimiento, "token": token, "aviso": aviso, "servicio": servicio["nombre"]}

    # ------------------------------------------------------------------
    # módulos
    # ------------------------------------------------------------------

    async def entrar(self, nombre: str, modulo: str, servicios: list[int] | None = None) -> int:
        nombre = (nombre or "").strip()
        modulo = str(modulo or "").strip()
        if not nombre or not modulo:
            raise ErrorTurno("Escribe tu nombre y el número de módulo.")
        async with self.candado:
            ahora = db.iso(self.ahora())
            fila = self.conn.execute("SELECT * FROM agentes WHERE nombre=?", (nombre,)).fetchone()
            ocupado = self.conn.execute(
                "SELECT nombre FROM agentes WHERE modulo=? AND nombre<>? AND estado<>'desconectado'",
                (modulo, nombre),
            ).fetchone()
            if ocupado:
                raise ErrorTurno(f"El módulo {modulo} ya está abierto por {ocupado['nombre']}.")
            servicios_txt = json.dumps(servicios or [])
            if fila:
                agente_id = fila["id"]
                inicio_jornada = fila["trabajando_desde"]
                if not inicio_jornada or not inicio_jornada.startswith(self._hoy()) \
                        or fila["estado"] == "desconectado":
                    inicio_jornada = ahora
                estado = "atendiendo" if self._ticket_actual(agente_id) else (
                    fila["estado"] if fila["estado"] == "pausa" else "disponible")
                self.conn.execute(
                    "UPDATE agentes SET modulo=?, servicios=?, estado=?, trabajando_desde=?, "
                    "estado_desde=CASE WHEN estado=? THEN estado_desde ELSE ? END WHERE id=?",
                    (modulo, servicios_txt, estado, inicio_jornada, estado, ahora, agente_id),
                )
            else:
                agente_id = self.conn.execute(
                    "INSERT INTO agentes(nombre, modulo, estado, estado_desde, trabajando_desde, servicios) "
                    "VALUES (?,?,?,?,?,?)",
                    (nombre, modulo, "disponible", ahora, ahora, servicios_txt),
                ).lastrowid
            self._evento("entrada", agente_id, detalle=modulo)
            llamados = await self._despachar()
        for ll in llamados:
            await self._notificar(ll)
        await self._notificar()
        return agente_id

    async def salir(self, agente_id: int):
        async with self.candado:
            self._agente(agente_id)
            if self._ticket_actual(agente_id):
                raise ErrorTurno("Finaliza o devuelve el turno que estás atendiendo antes de salir.")
            self._cambiar_estado(agente_id, "desconectado")
            self._evento("salida", agente_id)
        await self._notificar()

    def _cambiar_estado(self, agente_id: int, estado: str, pausa_tipo: str | None = None):
        self.conn.execute(
            "UPDATE agentes SET estado=?, pausa_tipo=?, estado_desde=? WHERE id=?",
            (estado, pausa_tipo, db.iso(self.ahora()), agente_id),
        )

    def _tomar(self, agente_id: int, ticket_id: int | None) -> dict:
        """Asigna un turno al agente. Sin `ticket_id`, el que le corresponde por orden."""
        agente = self._agente(agente_id)
        if agente["estado"] == "desconectado":
            raise ErrorTurno("Primero abre tu módulo.")
        if agente["estado"] == "pausa":
            raise ErrorTurno("Estás en pausa. Marca «Volver» para seguir atendiendo.")
        if self._ticket_actual(agente_id):
            raise ErrorTurno("Ya estás atendiendo un turno.")
        aj = self.ajustes()
        fila = self._fila_hoy()
        datos_agente = {"servicios": json.loads(agente["servicios"])}
        if ticket_id is None:
            elegido = motor.siguiente_para(datos_agente, fila, self.ahora(), aj)
            if not elegido:
                raise ErrorTurno("No hay personas esperando para tus servicios.")
            ticket_id = elegido["id"]
        ahora = db.iso(self.ahora())
        # La condición estado='espera' hace la toma atómica: si otro módulo
        # lo tomó un instante antes, no se actualiza nada.
        cur = self.conn.execute(
            "UPDATE tickets SET estado='atendiendo', agente_id=?, modulo=?, inicio_en=?, llamadas=1 "
            "WHERE id=? AND estado='espera'",
            (agente_id, agente["modulo"], ahora, ticket_id),
        )
        if cur.rowcount == 0:
            raise ErrorTurno("Ese turno ya lo tomó otro módulo.")
        self._cambiar_estado(agente_id, "atendiendo")
        self._evento("tomar", agente_id, ticket_id)
        t = self.conn.execute("SELECT codigo, modulo FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return {"t": "llamado", "codigo": t["codigo"], "modulo": t["modulo"]}

    async def tomar(self, agente_id: int, ticket_id: int | None = None) -> dict:
        async with self.candado:
            llamado = self._tomar(agente_id, ticket_id)
        await self._notificar(llamado)
        await self._notificar()
        return llamado

    async def _cerrar_atencion(self, agente_id: int, estado: str, tipo_evento: str):
        async with self.candado:
            actual = self._ticket_actual(agente_id)
            if not actual:
                raise ErrorTurno("No estás atendiendo ningún turno.")
            self.conn.execute(
                "UPDATE tickets SET estado=?, fin_en=? WHERE id=?",
                (estado, db.iso(self.ahora()), actual["id"]),
            )
            self._cambiar_estado(agente_id, "disponible")
            self._evento(tipo_evento, agente_id, actual["id"])
            llamados = await self._despachar()
        for ll in llamados:
            await self._notificar(ll)
        await self._notificar()

    async def finalizar(self, agente_id: int):
        await self._cerrar_atencion(agente_id, "finalizado", "finalizar")

    async def ausente(self, agente_id: int):
        await self._cerrar_atencion(agente_id, "ausente", "ausente")

    async def rellamar(self, agente_id: int) -> dict:
        async with self.candado:
            actual = self._ticket_actual(agente_id)
            if not actual:
                raise ErrorTurno("No estás atendiendo ningún turno.")
            if actual["llamadas"] >= self.ajustes()["max_llamadas"]:
                raise ErrorTurno("Ya se llamó el máximo de veces. Márcalo como «No se presentó».")
            self.conn.execute("UPDATE tickets SET llamadas = llamadas + 1 WHERE id=?", (actual["id"],))
            self._evento("rellamar", agente_id, actual["id"])
        llamado = {"t": "llamado", "codigo": actual["codigo"], "modulo": actual["modulo"]}
        await self._notificar(llamado)
        await self._notificar()
        return llamado

    async def devolver(self, agente_id: int, servicio_id: int | None = None):
        """Devuelve el turno a la fila, conservando su lugar. Con `servicio_id`, lo transfiere."""
        async with self.candado:
            actual = self._ticket_actual(agente_id)
            if not actual:
                raise ErrorTurno("No estás atendiendo ningún turno.")
            nuevo_servicio = servicio_id or actual["servicio_id"]
            self.conn.execute(
                "UPDATE tickets SET estado='espera', agente_id=NULL, modulo=NULL, inicio_en=NULL, "
                "llamadas=0, servicio_id=?, transferido_de=? WHERE id=?",
                (nuevo_servicio, actual["servicio_id"] if servicio_id else actual["transferido_de"],
                 actual["id"]),
            )
            self._cambiar_estado(agente_id, "disponible")
            self._evento("transferir" if servicio_id else "devolver", agente_id, actual["id"],
                         str(nuevo_servicio))
            llamados = await self._despachar(excluir=agente_id)
        for ll in llamados:
            await self._notificar(ll)
        await self._notificar()

    async def pausa(self, agente_id: int, tipo: str):
        if tipo not in motor.TIPOS_PAUSA:
            raise ErrorTurno("Tipo de pausa no válido.")
        async with self.candado:
            agente = self._agente(agente_id)
            if self._ticket_actual(agente_id):
                raise ErrorTurno("Termina la atención actual antes de salir a pausa.")
            if agente["estado"] == "desconectado":
                raise ErrorTurno("Primero abre tu módulo.")
            self._cambiar_estado(agente_id, "pausa", tipo)
            self._evento("pausa_inicio", agente_id, detalle=tipo)
        await self._notificar()

    async def volver(self, agente_id: int):
        async with self.candado:
            agente = self._agente(agente_id)
            if agente["estado"] != "pausa":
                raise ErrorTurno("No estás en pausa.")
            ahora = db.iso(self.ahora())
            self._cambiar_estado(agente_id, "disponible")
            self.conn.execute("UPDATE agentes SET trabajando_desde=? WHERE id=?", (ahora, agente_id))
            self._evento("pausa_fin", agente_id, detalle=agente["pausa_tipo"])
            llamados = await self._despachar()
        for ll in llamados:
            await self._notificar(ll)
        await self._notificar()

    async def modo_auto(self, agente_id: int, activo: bool):
        async with self.candado:
            self._agente(agente_id)
            self.conn.execute("UPDATE agentes SET modo_auto=? WHERE id=?", (int(activo), agente_id))
            self._evento("modo_auto", agente_id, detalle="si" if activo else "no")
            llamados = await self._despachar()
        for ll in llamados:
            await self._notificar(ll)
        await self._notificar()

    async def _despachar(self, excluir: int | None = None) -> list[dict]:
        """Asigna turnos a los módulos en modo automático que estén libres.

        Primero recibe quien lleva más tiempo esperando sin atender, para
        repartir la carga de forma pareja. Se llama con el candado tomado.
        """
        llamados = []
        libres = self.conn.execute(
            "SELECT id FROM agentes WHERE modo_auto=1 AND estado='disponible' ORDER BY estado_desde"
        ).fetchall()
        for a in libres:
            if a["id"] == excluir:
                continue
            try:
                llamados.append(self._tomar(a["id"], None))
            except ErrorTurno:
                continue
        return llamados

    async def despachar(self):
        async with self.candado:
            llamados = await self._despachar()
        for ll in llamados:
            await self._notificar(ll)
        if llamados:
            await self._notificar()

    # ------------------------------------------------------------------
    # gestor
    # ------------------------------------------------------------------

    async def guardar_ajustes(self, cambios: dict):
        async with self.candado:
            db.guardar_ajustes(self.conn, cambios)
        await self._notificar()

    async def guardar_servicio(self, nombre: str, prefijo: str, servicio_id: int | None = None,
                               activo: bool = True):
        nombre, prefijo = (nombre or "").strip(), (prefijo or "").strip().upper()
        if not nombre or not re.fullmatch(r"[A-Z]{1,2}", prefijo) or prefijo.startswith("P"):
            raise ErrorTurno("El servicio necesita nombre y un prefijo de 1 o 2 letras (sin empezar por P).")
        async with self.candado:
            if servicio_id:
                self.conn.execute("UPDATE servicios SET nombre=?, prefijo=?, activo=? WHERE id=?",
                                  (nombre, prefijo, int(activo), servicio_id))
            else:
                self.conn.execute("INSERT INTO servicios(nombre, prefijo) VALUES (?,?)", (nombre, prefijo))
        await self._notificar()

    # ------------------------------------------------------------------
    # vistas (solo lectura)
    # ------------------------------------------------------------------

    def servicios(self, todos: bool = False) -> list[dict]:
        sql = "SELECT * FROM servicios" + ("" if todos else " WHERE activo=1") + " ORDER BY id"
        return [dict(s) for s in self.conn.execute(sql)]

    def _calculos(self):
        aj = self.ajustes()
        ahora = self.ahora()
        modelo = self.modelo()
        fila = self._fila_hoy()
        agentes = self._agentes_motor()
        esperas = motor.estimar_esperas(fila, agentes, ahora, modelo, aj)
        return aj, ahora, modelo, fila, agentes, esperas

    def _espera_nuevo(self, fila, agentes, ahora, modelo, aj) -> float | None:
        nuevo = {"id": -1, "creado_en": ahora, "prioridad": "general", "servicio_id": None}
        agentes_libres = [{**a, "servicios": []} for a in agentes]
        return motor.estimar_esperas(fila + [nuevo], agentes_libres, ahora, modelo, aj)[-1]

    def estado_publico(self) -> dict:
        aj, ahora, modelo, fila, agentes, esperas = self._calculos()
        llamados = self.conn.execute(
            "SELECT codigo, modulo, estado, inicio_en FROM tickets WHERE inicio_en >= ? "
            "AND estado IN ('atendiendo','finalizado') ORDER BY inicio_en DESC LIMIT 8",
            (self._hoy(),),
        ).fetchall()
        return {
            "hora": db.iso(ahora),
            "llamados": [dict(l) for l in llamados],
            "en_espera": len(fila),
            "espera_nuevo_min": _redondear(self._espera_nuevo(fila, agentes, ahora, modelo, aj)),
            "capacidad": motor.resumen_capacidad(agentes, modelo),
        }

    def seguimiento(self, token: str) -> dict:
        t = self.conn.execute(
            "SELECT t.*, s.nombre AS servicio FROM tickets t JOIN servicios s ON s.id=t.servicio_id "
            "WHERE token=?", (token,)
        ).fetchone()
        if not t:
            raise ErrorTurno("No encontramos ese turno.")
        aj, ahora, modelo, fila, agentes, esperas = self._calculos()
        orden = [x["id"] for x in motor.ordenar_fila(fila, ahora, aj)]
        return {
            "codigo": t["codigo"],
            "servicio": t["servicio"],
            "prioridad": t["prioridad"],
            "estado": t["estado"],
            "modulo": t["modulo"],
            "creado_en": t["creado_en"],
            "posicion": orden.index(t["id"]) + 1 if t["id"] in orden else None,
            "espera_min": _redondear(esperas.get(t["id"])) if t["estado"] == "espera" else None,
            "capacidad": motor.resumen_capacidad(agentes, modelo),
        }

    def estado_agente(self, agente_id: int) -> dict:
        aj, ahora, modelo, fila, agentes, esperas = self._calculos()
        yo = next((a for a in agentes if a["id"] == agente_id), None)
        if not yo:
            raise ErrorTurno("Ese módulo no existe. Vuelve a entrar.")
        nombres = {s["id"]: s["nombre"] for s in self.servicios(todos=True)}
        recomendaciones = motor.recomendar_pausas(agentes, ahora, len(fila), aj)
        orden = motor.ordenar_fila(fila, ahora, aj)
        siguiente = motor.siguiente_para(yo, fila, ahora, aj)
        mis = self.conn.execute(
            "SELECT inicio_en, fin_en FROM tickets WHERE agente_id=? AND estado='finalizado' AND fin_en >= ?",
            (agente_id, self._hoy()),
        ).fetchall()
        duraciones = [(db.fecha(m["fin_en"]) - db.fecha(m["inicio_en"])).total_seconds() / 60 for m in mis]
        actual = None
        if yo["ticket"]:
            t = yo["ticket"]
            actual = {
                "id": t["id"], "codigo": t["codigo"], "cedula": t["cedula"], "nombre": t["nombre"],
                "servicio": nombres.get(t["servicio_id"]), "prioridad": t["prioridad"],
                "motivo": t["motivo"], "inicio_en": db.iso(t["inicio_en"]), "llamadas": t["llamadas"],
                "esperado_min": round(modelo(t["servicio_id"], t["prioridad"]), 1),
                "espero_min": round((t["inicio_en"] - t["creado_en"]).total_seconds() / 60),
            }
        return {
            "hora": db.iso(ahora),
            "agente": {
                "id": yo["id"], "nombre": yo["nombre"], "modulo": yo["modulo"], "estado": yo["estado"],
                "pausa_tipo": yo["pausa_tipo"], "estado_desde": db.iso(yo["estado_desde"]),
                "trabajando_desde": db.iso(yo["trabajando_desde"]), "modo_auto": bool(yo["modo_auto"]),
                "servicios": yo["servicios"], "pausas_hoy": yo["pausas_hoy"],
            },
            "actual": actual,
            "fila": [
                {
                    "id": t["id"], "codigo": t["codigo"], "servicio": nombres.get(t["servicio_id"]),
                    "servicio_id": t["servicio_id"], "prioridad": t["prioridad"], "motivo": t["motivo"],
                    "creado_en": db.iso(t["creado_en"]),
                    "esperado_min": round(modelo(t["servicio_id"], t["prioridad"]), 1),
                    "apto": motor.puede_atender(yo, t),
                    "sugerido": bool(siguiente and siguiente["id"] == t["id"]),
                }
                for t in orden
            ],
            "equipo": motor.resumen_capacidad(agentes, modelo),
            "espera_nuevo_min": _redondear(self._espera_nuevo(fila, agentes, ahora, modelo, aj)),
            "recomendacion": recomendaciones.get(agente_id),
            "mis_numeros": {
                "atendidos": len(duraciones),
                "promedio_min": round(sum(duraciones) / len(duraciones), 1) if duraciones else None,
            },
            "pausa_minutos": aj["pausa_minutos"],
            "max_llamadas": aj["max_llamadas"],
            "servicios": self.servicios(),
        }

    def estado_panel(self) -> dict:
        aj, ahora, modelo, fila, agentes, esperas = self._calculos()
        hoy = self._hoy()
        tickets = [self._ticket_dict(t) for t in self.conn.execute(
            "SELECT * FROM tickets WHERE creado_en >= ?", (hoy,))]
        atendidos = [t for t in tickets if t["estado"] == "finalizado"]
        esperas_reales = [(t["inicio_en"] - t["creado_en"]).total_seconds() / 60
                          for t in tickets if t["inicio_en"]]
        duraciones = [(t["fin_en"] - t["inicio_en"]).total_seconds() / 60 for t in atendidos]

        por_hora: dict[int, dict] = {}
        for t in tickets:
            h = por_hora.setdefault(t["creado_en"].hour, {"llegadas": 0, "atendidos": 0, "esperas": []})
            h["llegadas"] += 1
            if t["inicio_en"]:
                h["esperas"].append((t["inicio_en"] - t["creado_en"]).total_seconds() / 60)
        for t in atendidos:
            por_hora.setdefault(t["fin_en"].hour, {"llegadas": 0, "atendidos": 0, "esperas": []})
            por_hora[t["fin_en"].hour]["atendidos"] += 1

        desde = (ahora - timedelta(days=56)).date().isoformat()
        creaciones = [db.fecha(f[0]) for f in self.conn.execute(
            "SELECT creado_en FROM tickets WHERE creado_en >= ?", (desde,))]
        llegadas = motor.llegadas_por_hora(creaciones, ahora)
        dotacion = motor.dotacion_recomendada(llegadas, modelo.general, aj)

        nombres = {s["id"]: s["nombre"] for s in self.servicios(todos=True)}
        atendidos_por_agente: dict[int, list[float]] = {}
        for t in atendidos:
            atendidos_por_agente.setdefault(t["agente_id"], []).append(
                (t["fin_en"] - t["inicio_en"]).total_seconds() / 60)
        recomendaciones = motor.recomendar_pausas(agentes, ahora, len(fila), aj)
        meta = aj["meta_espera_min"]

        return {
            "hora": db.iso(ahora),
            "kpis": {
                "en_espera": len(fila),
                "atendidos": len(atendidos),
                "ausentes": sum(1 for t in tickets if t["estado"] == "ausente"),
                "espera_promedio_min": _promedio(esperas_reales),
                "espera_max_min": round(max(esperas_reales), 1) if esperas_reales else None,
                "atencion_promedio_min": _promedio(duraciones),
                "dentro_meta_pct": round(100 * sum(1 for e in esperas_reales if e <= meta) / len(esperas_reales))
                if esperas_reales else None,
                "espera_nuevo_min": _redondear(self._espera_nuevo(fila, agentes, ahora, modelo, aj)),
            },
            "capacidad": motor.resumen_capacidad(agentes, modelo),
            "agentes": [
                {
                    "id": a["id"], "nombre": a["nombre"], "modulo": a["modulo"], "estado": a["estado"],
                    "pausa_tipo": a["pausa_tipo"], "estado_desde": db.iso(a["estado_desde"]),
                    "trabajando_desde": db.iso(a["trabajando_desde"]), "modo_auto": bool(a["modo_auto"]),
                    "atendidos": len(atendidos_por_agente.get(a["id"], [])),
                    "promedio_min": _promedio(atendidos_por_agente.get(a["id"], [])),
                    "turno": a["ticket"]["codigo"] if a["ticket"] else None,
                    "recomendacion": recomendaciones.get(a["id"]),
                    "pausas_hoy": a["pausas_hoy"],
                }
                for a in agentes
            ],
            "tiempos": {
                "general_min": round(modelo.general, 1),
                "preferencial_min": round(modelo.preferencial, 1),
                "factor_preferencial": round(modelo.factor_preferencial, 2),
                "por_servicio": [
                    {"servicio": nombres.get(sid, sid), "prioridad": prio, "minutos": round(v, 1)}
                    for (sid, prio), v in sorted(modelo.por_clave.items())
                ],
                "muestras": len(duraciones),
            },
            "por_hora": [
                {"hora": h, "llegadas": d["llegadas"], "atendidos": d["atendidos"],
                 "espera_promedio_min": _promedio(d["esperas"]),
                 "pronostico": round(llegadas.get(h, 0), 1), "modulos_recomendados": dotacion.get(h)}
                for h, d in sorted({**{h: {"llegadas": 0, "atendidos": 0, "esperas": []} for h in llegadas},
                                    **por_hora}.items())
            ],
            "fila": [
                {"codigo": t["codigo"], "servicio": nombres.get(t["servicio_id"]), "prioridad": t["prioridad"],
                 "motivo": t["motivo"], "creado_en": db.iso(t["creado_en"]),
                 "espera_min": _redondear(esperas.get(t["id"]))}
                for t in motor.ordenar_fila(fila, ahora, aj)
            ],
            "ajustes": aj,
            "servicios": self.servicios(todos=True),
        }

    def historial_csv(self, desde: str, hasta: str) -> str:
        filas = self.conn.execute(
            "SELECT t.codigo, t.cedula, s.nombre AS servicio, t.prioridad, t.motivo, t.estado, "
            "t.creado_en, t.inicio_en, t.fin_en, a.nombre AS agente, t.modulo, t.llamadas "
            "FROM tickets t JOIN servicios s ON s.id=t.servicio_id LEFT JOIN agentes a ON a.id=t.agente_id "
            "WHERE t.creado_en >= ? AND t.creado_en < ? ORDER BY t.creado_en", (desde, hasta),
        ).fetchall()
        columnas = ["codigo", "cedula", "servicio", "prioridad", "motivo", "estado", "creado_en",
                    "inicio_en", "fin_en", "agente", "modulo", "llamadas", "espera_min", "atencion_min"]
        lineas = [",".join(columnas)]
        for f in filas:
            d = dict(f)
            c, i, fi = db.fecha(d["creado_en"]), db.fecha(d["inicio_en"]), db.fecha(d["fin_en"])
            d["espera_min"] = round((i - c).total_seconds() / 60, 1) if i else ""
            d["atencion_min"] = round((fi - i).total_seconds() / 60, 1) if i and fi else ""
            lineas.append(",".join(_csv(d[k]) for k in columnas))
        return "\n".join(lineas) + "\n"


def _redondear(v):
    return None if v is None else round(v)


def _promedio(valores):
    return round(sum(valores) / len(valores), 1) if valores else None


def _csv(v) -> str:
    texto = "" if v is None else str(v)
    return f'"{texto}"' if any(c in texto for c in ',"\n') else texto
