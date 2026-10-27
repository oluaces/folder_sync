# ----------------------------------------------------------------------------
#  FolderSync - Makefile multiplataforma (macOS, Windows y Linux)
#
#  make            -> muestra la ayuda
#  make run        -> ejecuta la aplicación en un entorno virtual (.venv)
#  make build      -> genera la aplicación para el sistema actual (en dist/)
#  make dmg        -> (solo macOS) empaqueta FolderSync.app en un .dmg
#  make clean      -> borra build/, dist/ y cachés
#  make distclean  -> además borra el entorno virtual
#
#  En Windows hace falta GNU make y una shell tipo sh (p. ej. Git Bash, o
#  "choco install make" / "scoop install make" y ejecutar desde Git Bash).
# ----------------------------------------------------------------------------

APP_NAME := FolderSync
ENTRY    := folder_sync.py
VENV     := .venv
STAMP    := $(VENV)/.deps-installed

# ---- Detección del sistema ---------------------------------------------------
ifeq ($(OS),Windows_NT)
  PLATFORM  := windows
  PYTHON    ?= python
  VENV_PY   := $(VENV)/Scripts/python.exe
  PYI_FLAGS := --onefile --icon icons/folder_sync.ico
  ARTIFACT  := dist/$(APP_NAME).exe
else
  PYTHON    ?= python3
  VENV_PY   := $(VENV)/bin/python
  ifeq ($(shell uname -s),Darwin)
    PLATFORM  := macos
    PYI_FLAGS := --icon icons/folder_sync.icns --osx-bundle-identifier com.foldersync.app
    ARTIFACT  := dist/$(APP_NAME).app
  else
    PLATFORM  := linux
    PYI_FLAGS := --onefile
    ARTIFACT  := dist/$(APP_NAME)
  endif
endif

.DEFAULT_GOAL := help
.PHONY: help venv run build dmg clean distclean

help:
	@echo FolderSync - sistema detectado: $(PLATFORM)
	@echo   make venv       Crea el entorno virtual e instala las dependencias
	@echo   make run        Ejecuta la aplicacion
	@echo   make build      Genera la aplicacion para $(PLATFORM) en dist/
	@echo   make dmg        Crea un .dmg a partir de la .app - solo macOS
	@echo   make clean      Borra build/, dist/ y caches
	@echo   make distclean  Ademas borra el entorno virtual

# ---- Entorno virtual y dependencias ----------------------------------------
$(VENV_PY):
	$(PYTHON) -m venv $(VENV)

$(STAMP): $(VENV_PY) requirements.txt
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -r requirements.txt pyinstaller
	$(VENV_PY) -c "import pathlib; pathlib.Path('$(STAMP)').touch()"

venv: $(STAMP)

# ---- Ejecutar -------------------------------------------------------------
run: $(STAMP)
	$(VENV_PY) $(ENTRY)

# ---- Empaquetar -----------------------------------------------------------
build: $(STAMP)
	$(VENV_PY) -m PyInstaller --noconfirm --clean --windowed --name $(APP_NAME) $(PYI_FLAGS) $(ENTRY)
	@echo Listo: $(ARTIFACT)

dmg: build
ifeq ($(PLATFORM),macos)
	hdiutil create -volname "$(APP_NAME)" -srcfolder "dist/$(APP_NAME).app" -ov -format UDZO "dist/$(APP_NAME).dmg"
	@echo Listo: dist/$(APP_NAME).dmg
else
	@echo "make dmg" solo esta disponible en macOS
endif

# ---- Limpieza -------------------------------------------------------------
clean:
	$(PYTHON) -c "import shutil, pathlib; [shutil.rmtree(d, ignore_errors=True) for d in ('build', 'dist', '__pycache__')]; [f.unlink() for f in pathlib.Path('.').glob('*.spec')]"

distclean: clean
	$(PYTHON) -c "import shutil; shutil.rmtree('$(VENV)', ignore_errors=True)"
