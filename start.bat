@echo off
REM ===========================================================
REM  Keep this file PURE ASCII. Do NOT add Chinese characters.
REM  cmd reads .bat by byte offset; switching code page mid-file
REM  makes it lose sync and it starts executing garbled lines as
REM  commands. All Chinese UI lives in start.py instead.
REM ===========================================================
chcp 65001 >nul
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [ERROR] "uv" was not found on this computer.
    echo.
    echo   uv is needed to run this tool. Install it from:
    echo       https://docs.astral.sh/uv/
    echo.
    echo   After installing, double-click this file again.
    echo.
    pause
    exit /b 1
)

uv run python start.py %*
