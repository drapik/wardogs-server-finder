@echo off
rem WARDOGS: wait for a matching server and join it via Steam.
rem Edit the values below. Maps: bakurani, ozeti, zestafona, sabatulo (comma-separated).
rem Regions: eu, eu-east, eu-central, eu-west, eu-south, na, ...
rem MINUTES: match minute range, e.g. 0-10, 30-45, 60-
set MAP=bakurani
set REGION=eu-east,eu-central
set MIN_PLAYERS=50
set MINUTES=0-10

chcp 65001 >nul
cd /d "%~dp0"
py -3 wardogs_finder.py --map %MAP% --region %REGION% --min-players %MIN_PLAYERS% --minutes %MINUTES% --join %*
pause
