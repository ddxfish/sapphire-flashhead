@echo off
rem Check a FlashHead server from this computer, the way a Sapphire would use it.
rem Run flashhead-server.bat once first (it makes the env this uses).
rem
rem   flashhead-test.bat                     asks for the pairing string
rem   flashhead-test.bat "PAIRING" [FACE]    the pairing string in quotes, a face image if you like
rem
rem The pairing string comes from the server's "add NAME". Without quotes the & in it breaks cmd.
setlocal
cd /d "%~dp0"
set "ENV=flashhead-server"
if not "%FLASHHEAD_ENV%"=="" set "ENV=%FLASHHEAD_ENV%"
call conda activate %ENV% || (echo No env yet: run flashhead-server.bat once first & exit /b 1)
python door_test.py %*
