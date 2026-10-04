"""Servidor web de Fluye: API, pantallas y canal en tiempo real."""
from __future__ import annotations

import asyncio
import io
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .central import Central, ErrorTurno

ESTATICOS = Path(__file__).resolve().parent.parent / "static"
_FECHA_SIN_ZONA = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?")


def _con_zona(valor, zona: str):
    """Agrega la zona horaria del servidor a las fechas, para que los navegadores
    de otras zonas calculen bien los cronómetros."""
    if isinstance(valor, str) and _FECHA_SIN_ZONA.fullmatch(valor):
        return valor + zona
    if isinstance(valor, dict):
        return {k: _con_zona(v, zona) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_con_zona(v, zona) for v in valor]
    return valor


class RespuestaConZona(JSONResponse):
    def render(self, contenido) -> bytes:
        z = datetime.now().astimezone().strftime("%z")
        return super().render(_con_zona(contenido, f"{z[:3]}:{z[3:]}"))


class Canal:
    """Conexiones WebSocket abiertas. Cada cambio se avisa a todas."""

    def __init__(self):
        self.conexiones: set[WebSocket] = set()

    async def enviar(self, mensaje: dict):
        caidas = []
        for ws in list(self.conexiones):
            try:
                await ws.send_json(mensaje)
            except Exception:
                caidas.append(ws)
        for ws in caidas:
            self.conexiones.discard(ws)


class NuevoTicket(BaseModel):
    cedula: str
    servicio_id: int
    prioridad: str = "general"
    motivo: str | None = None
    nombre: str | None = None


class Entrada(BaseModel):
    nombre: str
    modulo: str
    servicios: list[int] = []


class Tomar(BaseModel):
    ticket_id: int | None = None


class Pausa(BaseModel):
    tipo: str


class Auto(BaseModel):
    activo: bool


class Devolver(BaseModel):
    servicio_id: int | None = None


class Servicio(BaseModel):
    id: int | None = None
    nombre: str
    prefijo: str
    activo: bool = True


def crear_app(ruta_db: str | None = None, central: Central | None = None,
              demo: bool | None = None) -> FastAPI:
    demo = bool(os.environ.get("FLUYE_DEMO")) if demo is None else demo
    ruta_db = ruta_db or os.environ.get("FLUYE_DB", "fluye.db")
    central = central or Central(ruta_db)

    @asynccontextmanager
    async def ciclo_de_vida(_app):
        tarea = None
        if demo:
            from . import demo as modo_demo
            modo_demo.preparar(central, ruta_db)
            tarea = asyncio.create_task(modo_demo.ciclo(central))
        yield
        if tarea:
            tarea.cancel()

    app = FastAPI(title="Fluye", description="Gestión de turnos en tiempo real",
                  default_response_class=RespuestaConZona, lifespan=ciclo_de_vida)
    canal = Canal()
    central.avisar = canal.enviar
    app.state.central = central
    pin = os.environ.get("FLUYE_PIN_GESTOR")

    def gestor(x_pin: str | None = Header(default=None)):
        if pin and x_pin != pin:
            raise HTTPException(401, "PIN del gestor incorrecto.")

    def editable():
        if demo:
            raise HTTPException(403, "En la demo pública los ajustes son de solo lectura.")

    @app.exception_handler(ErrorTurno)
    async def _error_turno(_: Request, exc: ErrorTurno):
        return JSONResponse({"error": str(exc)}, status_code=409)

    # --- páginas -------------------------------------------------------
    paginas = {"/": "index.html", "/kiosco": "kiosco.html", "/modulo": "modulo.html",
               "/pantalla": "pantalla.html", "/panel": "panel.html"}
    for ruta, archivo in paginas.items():
        app.add_api_route(ruta, (lambda a=archivo: FileResponse(ESTATICOS / a)), include_in_schema=False)

    @app.get("/t/{token}", include_in_schema=False)
    def pagina_seguimiento(token: str):
        return FileResponse(ESTATICOS / "seguir.html")

    app.mount("/static", StaticFiles(directory=ESTATICOS), name="static")

    # --- kiosco y público ----------------------------------------------
    @app.get("/api/servicios")
    def servicios():
        return central.servicios()

    @app.post("/api/tickets")
    async def crear_ticket(datos: NuevoTicket):
        return await central.crear_ticket(**datos.model_dump())

    @app.get("/api/tickets/{token}")
    def seguimiento(token: str):
        return central.seguimiento(token)

    @app.get("/api/qr/{token}.svg")
    def qr(token: str, request: Request):
        url = str(request.base_url).rstrip("/") + f"/t/{token}"
        imagen = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
        salida = io.BytesIO()
        imagen.save(salida)
        return Response(salida.getvalue(), media_type="image/svg+xml")

    @app.get("/api/publico")
    def publico():
        return {**central.estado_publico(), "demo": demo}

    # --- módulos ---------------------------------------------------------
    @app.post("/api/agentes/entrar")
    async def entrar(datos: Entrada):
        return {"id": await central.entrar(datos.nombre, datos.modulo, datos.servicios)}

    @app.get("/api/agentes/{agente_id}")
    def estado_agente(agente_id: int):
        return central.estado_agente(agente_id)

    @app.post("/api/agentes/{agente_id}/tomar")
    async def tomar(agente_id: int, datos: Tomar):
        return await central.tomar(agente_id, datos.ticket_id)

    @app.post("/api/agentes/{agente_id}/devolver")
    async def devolver(agente_id: int, datos: Devolver):
        await central.devolver(agente_id, datos.servicio_id)
        return {"ok": True}

    @app.post("/api/agentes/{agente_id}/pausa")
    async def pausa(agente_id: int, datos: Pausa):
        await central.pausa(agente_id, datos.tipo)
        return {"ok": True}

    @app.post("/api/agentes/{agente_id}/auto")
    async def auto(agente_id: int, datos: Auto):
        await central.modo_auto(agente_id, datos.activo)
        return {"ok": True}

    for accion in ("finalizar", "ausente", "rellamar", "volver", "salir"):
        async def _accion(agente_id: int, _a=accion):
            await getattr(central, _a)(agente_id)
            return {"ok": True}
        app.add_api_route(f"/api/agentes/{{agente_id}}/{accion}", _accion, methods=["POST"],
                          name=accion)

    # --- gestor ------------------------------------------------------------
    @app.get("/api/panel", dependencies=[Depends(gestor)])
    def panel():
        return central.estado_panel()

    @app.put("/api/ajustes", dependencies=[Depends(gestor), Depends(editable)])
    async def ajustes(cambios: dict):
        await central.guardar_ajustes(cambios)
        return central.ajustes()

    @app.post("/api/servicios", dependencies=[Depends(gestor), Depends(editable)])
    async def guardar_servicio(datos: Servicio):
        await central.guardar_servicio(datos.nombre, datos.prefijo, datos.id, datos.activo)
        return central.servicios(todos=True)

    @app.get("/api/historial.csv", dependencies=[Depends(gestor)])
    def historial(desde: str | None = None, hasta: str | None = None):
        hoy = date.today()
        desde = desde or (hoy - timedelta(days=30)).isoformat()
        hasta = hasta or (hoy + timedelta(days=1)).isoformat()
        return Response(central.historial_csv(desde, hasta), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="turnos_{desde}_{hasta}.csv"'})

    # --- tiempo real ---------------------------------------------------------
    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        canal.conexiones.add(socket)
        try:
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            canal.conexiones.discard(socket)

    return app
