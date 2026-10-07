@echo off
REM ============================================================================
REM  RGEM Update Dataset  -  PRODUCER step (run on ONE machine only)
REM
REM  Builds the latest RDPS cycle as RGEM .buf files for every station, writes
REM  them to the local BUFKIT Data folder, and publishes them to the Hugging Face
REM  dataset (ORG/rgem-bufkit) so everyone's downloader gets fresh data.
REM
REM  Takes ~3 min when there's a new cycle; a few seconds if already current
REM  (it no-ops until a new 00/06/12/18Z RDPS cycle is on the MSC datamart).
REM
REM  RGEM = Canada's Regional Deterministic Prediction System (RDPS), 10 km.
REM  BUFKIT reserves the name "RDPS", so the model is registered as RGEM.
REM
REM  NOTE: this is the PUBLISH step, different from "WW Bufkit RGEM.pl"
REM  (which just downloads the finished files into BUFKIT).
REM
REM  EDIT THE TWO PATHS BELOW for this machine:
REM    PYTHON  = path to python.exe (Anaconda recommended; see requirements.txt)
REM    PROJECT = folder where this project is installed (contains the "code" dir)
REM  And set --publish-repo to your own dataset if you are not using this one.
REM ============================================================================
set "PYTHON=C:\path\to\python.exe"
set "PROJECT=C:\path\to\rgem-bufkit"

echo Updating the RGEM BUFKIT dataset - please wait...
echo.
"%PYTHON%" -u "%PROJECT%\code\tools\update_rgem.py" --outdir "C:\Program Files (x86)\BUFKIT\Data" --publish-repo ORG/rgem-bufkit
echo.
echo Finished. Press any key to close.
pause >nul
