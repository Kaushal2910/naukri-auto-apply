@echo off
setlocal enabledelayedexpansion

title Naukri Auto Apply - Continuous Runner
cd /d "C:\Users\sonaw\OneDrive\Desktop\job\naukri-job-apply-ai"

REM ============================================================
REM  CONFIGURATION - edit these numbers if you want
REM ============================================================
REM Seconds to wait between runs (each run applies up to
REM stop_after_n_applications jobs from profile.yaml)
set WAIT_BETWEEN_RUNS=120

REM Total number of runs before stopping (0 = run forever
REM until you close this window)
set MAX_RUNS=8
REM ============================================================

set RUN_COUNT=0

echo ============================================================
echo   NAUKRI AUTO APPLY - CONTINUOUS RUNNER
echo   Each run applies up to your profile's limit.
echo   Close this window at any time to stop.
echo ============================================================
echo.

:loop
echo.
echo ############################################################
echo   RUN %RUN_COUNT% STARTING:  %date% %time%
echo ############################################################
call venv\Scripts\activate.bat
call python naukri_apply.py
REM Using 'call' so control returns here even if python exits weirdly

set /a RUN_COUNT+=1
echo.
echo --- Run %RUN_COUNT% finished at %time% ---

if %MAX_RUNS% GTR 0 if %RUN_COUNT% GEQ %MAX_RUNS% goto end

echo Waiting %WAIT_BETWEEN_RUNS% seconds before next run...
python -c "import time; time.sleep(%WAIT_BETWEEN_RUNS%)"
goto loop

:end
echo.
echo ============================================================
echo   DONE: %RUN_COUNT% runs completed.
echo   Results: applications_log.csv
echo ============================================================
echo.
echo Press any key to exit or just close the window.
pause >nul 2>nul
