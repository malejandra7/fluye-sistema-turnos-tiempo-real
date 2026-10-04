@echo off
chcp 65001 >nul
title Fluye - turnos
cd /d "%~dp0"
set "FLUYE_DIR=%~dp0"

echo.
echo   Iniciando Fluye...
echo.

rem ---- 1. Buscar Python 3.10 o mas reciente ----
set PY=
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set PY=py -3
if defined PY goto python_listo

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set PY=python
if defined PY goto python_listo

for %%V in (313 312 311 310) do (
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
)
if defined PY goto python_listo

rem ---- 2. Si no hay Python, instalarlo solo ----
echo   No encontre Python. Lo voy a instalar, esto pasa solo la primera vez.
echo   Puede tardar unos minutos...
echo.
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto python_listo

echo.
echo   No pude instalar Python automaticamente.
echo   Se abrira la pagina de descarga: instala Python, marca la casilla
echo   "Add Python to PATH" y vuelve a abrir este archivo.
start "" https://www.python.org/downloads/
pause
exit /b 1

:python_listo
rem ---- 3. Preparar el proyecto, solo la primera vez ----
if not exist ".venv\Scripts\python.exe" (
  echo   Preparando Fluye, solo la primera vez...
  %PY% -m venv .venv
  if errorlevel 1 goto error
)
if not exist ".venv\listo.txt" (
  echo   Instalando componentes, solo la primera vez. Necesita internet...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 goto error
  echo listo> ".venv\listo.txt"
)

rem ---- 4. Acceso directo en el escritorio, solo la primera vez ----
if not exist ".venv\acceso.txt" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$w = New-Object -ComObject WScript.Shell; $d = [Environment]::GetFolderPath('Desktop'); $s = $w.CreateShortcut((Join-Path $d 'Fluye.lnk')); $s.TargetPath = (Join-Path $env:FLUYE_DIR 'Iniciar Fluye.bat'); $s.WorkingDirectory = $env:FLUYE_DIR; $s.IconLocation = (Join-Path $env:FLUYE_DIR 'static\icono.ico'); $s.Save()" >nul 2>nul
  echo listo> ".venv\acceso.txt"
  echo   Cree un acceso directo "Fluye" en tu escritorio.
)

rem ---- 5. Arrancar ----
".venv\Scripts\python.exe" iniciar.py %*
if errorlevel 1 goto error
exit /b 0

:error
echo.
echo   Algo fallo. Toma una foto de esta ventana y enviala para revisarlo.
pause
exit /b 1
