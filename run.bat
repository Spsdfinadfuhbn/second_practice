@echo off
rem Запуск depgraph.
rem   run.bat test         - запустить тесты
rem   run.bat ^<аргументы^>  - запустить программу
cd /d "%~dp0"
if "%1"=="test" (
    python -m unittest discover -s tests -v
) else (
    python src\main.py %*
)
