@echo off
rem FlashHead as a GPU server on its own, no Sapphire on this computer.
rem Other computers' Sapphires use it in Client only mode (README: One GPU,
rem several Sapphires). Everything it keeps lands in .\user\ beside this file:
rem the models (~8 GB), its certificate and the keys of the computers let in.
rem Inside a Sapphire install (user\plugins\flashhead) it uses that Sapphire's
rem user\ instead, the same folders as the plugin: never files in the plugin's
rem own folder, which its signature would refuse.
rem Run it from an Anaconda Prompt (Miniconda), in a short folder path.
rem From PowerShell, pairing strings lose their quotes: use the Anaconda Prompt.
rem
rem   flashhead-server.bat               run it (builds its conda env the first time, ~5 GB)
rem   flashhead-server.bat add NAME      let a computer in: prints its pairing string
rem   flashhead-server.bat remove NAME   shut one out
rem   flashhead-server.bat list          who is let in
rem
rem Port 5018 (set PORT to change it) is the door other computers use: allow it in the firewall.
rem No NVIDIA GPU here? set INSTALL_TEST=1 first: it fetches and imports everything, then says it can't render.
setlocal
pushd "%~dp0" || exit /b 1
set "PYTHONUTF8=1"
set "ENV=flashhead-server"
if not "%FLASHHEAD_ENV%"=="" set "ENV=%FLASHHEAD_ENV%"
if "%PORT%"=="" set "PORT=5018"
set "ROOT=%CD%"
if exist "..\..\..\sapphire.py" for %%I in ("..\..\..") do set "ROOT=%%~fI"
set "DOOR=%ROOT%\user\worker_door\flashhead"

where conda >nul 2>nul || (echo conda not found: run this from an Anaconda Prompt & exit /b 1)
rem Made only when conda lists no such env: a failed activate can also mean conda
rem isn't set up in this window, and conda create -y over an env wipes it.
conda env list | findstr /b /c:"%ENV% " >nul
if errorlevel 1 (
    echo Making the %ENV% environment, once...
    call conda create -y -n %ENV% python=3.10 || exit /b 1
)
call conda activate %ENV% || (echo conda could not activate %ENV%: run this from an Anaconda Prompt & exit /b 1)

rem The packages are the ones plugin.json lists, read from it: one list, never two.
rem What was installed is noted inside the env, so a remade env installs again.
if not exist "%ROOT%\user" mkdir "%ROOT%\user"
set "STAMP=%CONDA_PREFIX%\flashhead-requirements.txt"
set "NEW=%ROOT%\user\flashhead-requirements.new"
python -c "import json; print('\n'.join(json.load(open('plugin.json', encoding='utf-8'))['environment']['pip']))" > "%NEW%" || exit /b 1
fc /b "%NEW%" "%STAMP%" >nul 2>nul
if errorlevel 1 (
    echo Installing packages, the first time downloads ~5 GB...
    python -m pip install -r "%NEW%" || exit /b 1
    move /y "%NEW%" "%STAMP%" >nul
) else (
    del "%NEW%"
)

if /i "%~1"=="add" (python worker_door.py add "%DOOR%" %2 %PORT% & exit /b)
if /i "%~1"=="remove" (python worker_door.py remove "%DOOR%" %2 & exit /b)
if /i "%~1"=="list" (python worker_door.py list "%DOOR%" & exit /b)
if not "%~1"=="" (echo usage: flashhead-server.bat [add NAME ^| remove NAME ^| list] & exit /b 2)

set "SAPPHIRE_ROOT=%ROOT%"
set "SAPPHIRE_SERVICE_PORT=5017"
set "IT=false"
if "%INSTALL_TEST%"=="1" set "IT=true"
set "SAPPHIRE_PLUGIN_SETTINGS={"share": true, "share_port": %PORT%, "install_test": %IT%}"
echo FlashHead server: computers let in reach it on port %PORT% (flashhead-server.bat add NAME)
python server.py
