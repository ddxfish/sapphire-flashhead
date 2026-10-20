@echo off
rem FlashHead as a GPU server on its own, no Sapphire on this computer.
rem Other computers' Sapphires use it in Client only mode (README: One GPU,
rem several Sapphires). Everything it keeps lands in .\user\ beside this file:
rem the models (~8 GB), its certificate and the keys of the computers let in.
rem Run it from an Anaconda Prompt (Miniconda), in a short folder path.
rem
rem   flashhead-server.bat               run it (builds its conda env the first time, ~5 GB)
rem   flashhead-server.bat add NAME      let a computer in: prints its pairing string
rem   flashhead-server.bat remove NAME   shut one out
rem   flashhead-server.bat list          who is let in
rem
rem Port 5018 (set PORT to change it) is the door other computers use: allow it in the firewall.
setlocal
cd /d "%~dp0"
set "ENV=flashhead-server"
if not "%FLASHHEAD_ENV%"=="" set "ENV=%FLASHHEAD_ENV%"
if "%PORT%"=="" set "PORT=5018"
set "DOOR=user\worker_door\flashhead"

where conda >nul 2>nul || (echo conda not found: run this from an Anaconda Prompt & exit /b 1)
call conda activate %ENV% 2>nul
if errorlevel 1 (
    echo Making the %ENV% environment, once...
    call conda create -y -n %ENV% python=3.10 || exit /b 1
    call conda activate %ENV% || exit /b 1
)

rem The packages are the ones plugin.json lists, read from it: one list, never two.
if not exist user mkdir user
python -c "import json; print('\n'.join(json.load(open('plugin.json'))['environment']['pip']))" > user\requirements.new
fc /b user\requirements.new user\requirements.txt >nul 2>nul
if errorlevel 1 (
    echo Installing packages, the first time downloads ~5 GB...
    python -m pip install -r user\requirements.new || exit /b 1
    move /y user\requirements.new user\requirements.txt >nul
) else (
    del user\requirements.new
)

if /i "%~1"=="add" (python worker_door.py add %DOOR% %2 %PORT% & exit /b)
if /i "%~1"=="remove" (python worker_door.py remove %DOOR% %2 & exit /b)
if /i "%~1"=="list" (python worker_door.py list %DOOR% & exit /b)
if not "%~1"=="" (echo usage: flashhead-server.bat [add NAME ^| remove NAME ^| list] & exit /b 2)

set "SAPPHIRE_ROOT=%CD%"
set "SAPPHIRE_SERVICE_PORT=5017"
set "SAPPHIRE_PLUGIN_SETTINGS={"share": true, "share_port": %PORT%}"
echo FlashHead server: computers let in reach it on port %PORT% (flashhead-server.bat add NAME)
python server.py
