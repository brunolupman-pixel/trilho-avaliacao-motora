@echo off
REM Inicia o Trilho (site + servidor local).
REM Na primeira vez cria o ambiente do Python; a cada abertura confere as dependencias
REM (instala o que faltar, por exemplo quando uma versao nova do Trilho precisa de algo novo).
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv || (echo Instale o Python 3.12+ e marque "Add Python to PATH". & pause & exit /b 1)
)
.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt || (
  echo.
  echo Nao foi possivel instalar as dependencias. Confira a internet e tente de novo.
  pause & exit /b 1
)
cd servidor
..\.venv\Scripts\python -m trilho %*
pause
