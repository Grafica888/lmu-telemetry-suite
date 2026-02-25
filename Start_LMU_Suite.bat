@echo off
title "LMU Telemetry & Performance Suite"

echo =======================================================
echo     Start LMU Telemetry ^& Performance Suite
echo =======================================================
echo.

:: Prüfen ob Python installiert ist
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [FEHLER] Python ist nicht installiert oder nicht im PATH System!
    echo Bitte installiere Python 3.9+ von python.org
    pause
    exit /b
)

:: Dependencies installieren falls nötig
echo Pruefe und installiere Abhaengigkeiten...
pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo [FEHLER] Konnte reqirements.txt nicht installieren. Bitte manuell 'pip install -r requirements.txt' ausfuehren.
    pause
    exit /b
)

echo.
echo.
echo [1/3] Starte Data Logger im Hintergrund...
start /B "" python data_logger.py

echo [2/3] Starte Shift Overlay im Hintergrund...
start /B "" python shift_overlay.py

echo [3/3] Starte Streamlit Dashboard...
echo =======================================================
echo.
echo Die LMU Telemetry Suite laeuft jetzt!
echo Du kannst dieses eine schwarze Fenster einfach offen lassen.
echo.
echo Wenn du das Programm beenden willst:
echo Schliesse einfach DIESES EINE Fenster - das beendet auch den Logger im Hintergrund.
echo =======================================================
python -m streamlit run app.py
