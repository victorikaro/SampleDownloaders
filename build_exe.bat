@echo off
setlocal
cd /d "%~dp0"

REM Usa o Python instalado no sistema e chama o PyInstaller pelo módulo,
REM evitando falha quando o executável do PyInstaller não está no PATH.
where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
) else (
    set "PYTHON_CMD=python"
)

%PYTHON_CMD% -m pip install -U pyinstaller yt-dlp mutagen pillow
%PYTHON_CMD% -m PyInstaller --noconsole --onefile --name BaixadorYT --collect-all yt_dlp --add-data "baixador_youtube.py;." launcher.py

echo.
echo Pronto! O programa esta em dist\BaixadorYT.exe
pause
