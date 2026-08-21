@echo off
setlocal

set "ANI365_BASH=bash.exe"
where "%ANI365_BASH%" >nul 2>nul && goto run
if defined SCOOP if exist "%SCOOP%\apps\git\current\bin\bash.exe" set "ANI365_BASH=%SCOOP%\apps\git\current\bin\bash.exe" && goto run
if exist "%USERPROFILE%\scoop\apps\git\current\bin\bash.exe" set "ANI365_BASH=%USERPROFILE%\scoop\apps\git\current\bin\bash.exe" && goto run
if defined SCOOP_GLOBAL if exist "%SCOOP_GLOBAL%\apps\git\current\bin\bash.exe" set "ANI365_BASH=%SCOOP_GLOBAL%\apps\git\current\bin\bash.exe" && goto run

echo ani365-cli: bash.exe not found. Install Git with Scoop: scoop install git 1>&2
exit /b 1

:run
"%ANI365_BASH%" "%~dp0ani365-cli" %*
exit /b %errorlevel%
