@echo off
rem Windows stand-in for `make`, so the same commands work without installing
rem GNU make:  make, make help, make upload, make status, ...
rem In PowerShell type .\make instead of make.
setlocal
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0tools\manage.py" %*
) else (
    python "%~dp0tools\manage.py" %*
)
exit /b %errorlevel%
