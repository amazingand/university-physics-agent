@echo off
setlocal
cd /d "%~dp0\.."

if exist ".venv\.physics-agent-0.6.0-ready" goto launch

echo University Physics Agent Windows v0 first-time setup
echo First install downloads existing dependencies from PyPI and may use network traffic.
choice /C YN /N /M "Continue? [Y/N] "
if errorlevel 2 exit /b 1

set "PYTHON_COMMAND="
set "PYTHON_ARGUMENTS="
if defined PHYSICS_AGENT_PYTHON (
  if not exist "%PHYSICS_AGENT_PYTHON%" goto invalid_explicit_python
  if exist "%PHYSICS_AGENT_PYTHON%\NUL" goto invalid_explicit_python
  set "PYTHON_COMMAND=%PHYSICS_AGENT_PYTHON%"
  goto create_venv
)

where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_COMMAND=py"
  set "PYTHON_ARGUMENTS=-3.12"
  goto create_venv
)

where python >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_COMMAND=python"
  goto create_venv
)
goto no_python

:create_venv
"%PYTHON_COMMAND%" %PYTHON_ARGUMENTS% -m venv .venv
if errorlevel 1 goto install_failed

if exist "artifacts\university_physics_agent-0.6.0-py3-none-any.whl" (
  ".venv\Scripts\python.exe" -m pip install "artifacts\university_physics_agent-0.6.0-py3-none-any.whl"
) else (
  if not exist "pyproject.toml" goto no_source
  ".venv\Scripts\python.exe" -m pip install .
)
if errorlevel 1 goto install_failed
> ".venv\.physics-agent-0.6.0-ready" echo ready
if errorlevel 1 goto install_failed

:launch
".venv\Scripts\python.exe" -m physics_agent.gui
if errorlevel 1 goto launch_failed
exit /b 0

:no_python
echo No usable Python command was found.
echo Choose one: set PHYSICS_AGENT_PYTHON to an existing python.exe,
echo install Python 3.12 with the py launcher, or add python.exe to PATH.
pause
exit /b 1

:invalid_explicit_python
echo PHYSICS_AGENT_PYTHON does not point to an existing file.
echo Set it to python.exe, unset it and use py -3.12, or add python.exe to PATH.
pause
exit /b 1

:no_source
echo No fixed wheel or project source was found in this directory.
pause
exit /b 1

:install_failed
echo Installation failed. Check the message above, then run this launcher again.
pause
exit /b 1

:launch_failed
echo The GUI could not start. Check the message above.
pause
exit /b 1
