"""Arranca Fluye y abre el navegador. Lo usan «Iniciar Fluye.bat» y «Iniciar Fluye.command».

    python iniciar.py          # uso normal (datos en fluye.db)
    python iniciar.py --demo   # datos de ejemplo y gente llegando sola (datos en demo.db)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
PUERTO = 8000


def ip_local() -> str:
    """IP de este computador en la red (no envía nada a internet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def ya_corriendo(puerto: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", puerto)) == 0


def esperar_y_abrir(url: str, puerto: int, demo: bool):
    for _ in range(60):
        if ya_corriendo(puerto):
            break
        time.sleep(0.5)
    webbrowser.open(url)
    if demo:
        import simulador
        try:
            asyncio.run(simulador.vivo(f"http://127.0.0.1:{puerto}", ritmo=20, modulos=3, minutos=100000))
        except Exception as e:  # la demostración nunca debe tumbar el servidor
            print("La simulación se detuvo:", e)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--demo", action="store_true")
    p.add_argument("--puerto", type=int, default=PUERTO)
    a = p.parse_args()

    os.chdir(CARPETA)
    sys.path.insert(0, str(CARPETA))
    ip = ip_local()
    url = f"http://{ip}:{a.puerto}/"

    if ya_corriendo(a.puerto):
        print("Fluye ya está abierto. Abriendo el navegador...")
        webbrowser.open(url)
        return

    base = "demo.db" if a.demo else "fluye.db"
    os.environ["FLUYE_DB"] = str(CARPETA / base)
    if a.demo and not (CARPETA / base).exists():
        print("Preparando datos de ejemplo (20 días)...")
        import simulador
        simulador.historial(str(CARPETA / base), 20)

    print()
    print("  ============================================")
    print("   FLUYE está funcionando" + ("  (DEMOSTRACIÓN)" if a.demo else ""))
    print("  ============================================")
    print()
    print(f"   En este computador:  http://localhost:{a.puerto}/")
    print(f"   En otros equipos de la misma red (módulos, kiosco, TV):")
    print(f"      Kiosco:    {url}kiosco")
    print(f"      Módulo:    {url}modulo")
    print(f"      Pantalla:  {url}pantalla")
    print(f"      Panel:     {url}panel")
    print()
    print("   NO cierres esta ventana: si la cierras, Fluye se apaga.")
    print("   (Puedes minimizarla.)")
    print()

    threading.Thread(target=esperar_y_abrir, args=(url, a.puerto, a.demo), daemon=True).start()

    import uvicorn
    from app.main import crear_app
    uvicorn.run(crear_app(), host="0.0.0.0", port=a.puerto, log_level="warning")


if __name__ == "__main__":
    main()
