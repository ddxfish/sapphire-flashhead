@echo off
rem How fast this GPU renders her face (bench.py). Run flashhead-server.bat once first:
rem it makes the env and downloads the models this uses.
rem
rem   flashhead-bench.bat                     eager, compiled, sdpa
rem   flashhead-bench.bat compiled autotune   only these
rem
rem Inside Sapphire: set FLASHHEAD_ENV=sapphire-plugin-flashhead first.
setlocal
pushd "%~dp0" || exit /b 1
set "PYTHONUTF8=1"
set "ENV=flashhead-server"
if not "%FLASHHEAD_ENV%"=="" set "ENV=%FLASHHEAD_ENV%"
call conda activate %ENV% || (echo No env yet: run flashhead-server.bat once first & exit /b 1)
python bench.py %*
