@echo off
rem Check a FlashHead server from this computer, the way a Sapphire would use it.
rem Run flashhead-server.bat once first (it makes the env this uses).
rem
rem   flashhead-test.bat                     asks for the pairing string
rem   flashhead-test.bat "PAIRING" [FACE]    the pairing string in quotes, a face image if you like
rem
rem The pairing string comes from the server's "add NAME". Without quotes the & in it breaks cmd.
rem From PowerShell the quotes are lost on the way: run it with no argument and paste when asked.
setlocal
pushd "%~dp0" || exit /b 1
set "PYTHONUTF8=1"
set "ENV=flashhead-server"
if not "%FLASHHEAD_ENV%"=="" set "ENV=%FLASHHEAD_ENV%"
call conda activate %ENV% || (echo No env yet: run flashhead-server.bat once first & exit /b 1)
python door_test.py %*
