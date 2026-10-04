#!/bin/bash
# Doble clic en Mac para abrir Fluye. Con --demo abre la versión de demostración.
cd "$(dirname "$0")"
echo
echo "  Iniciando Fluye..."
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "  Necesitas Python 3.10 o más reciente. Se abrirá la página de descarga:"
  echo "  instálalo y vuelve a abrir este archivo."
  open "https://www.python.org/downloads/macos/"
  read -p "  Presiona Enter para cerrar..."
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "  Preparando Fluye, solo la primera vez..."
  "$PY" -m venv .venv || { read -p "  Algo falló. Presiona Enter..."; exit 1; }
fi
if [ ! -f .venv/listo.txt ]; then
  echo "  Instalando componentes, solo la primera vez. Necesita internet..."
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt || { read -p "  Algo falló. Presiona Enter..."; exit 1; }
  echo listo > .venv/listo.txt
fi
.venv/bin/python iniciar.py "$@"
