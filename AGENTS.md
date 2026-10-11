# AGENTS.md

DictaVoz: dictado por voz local (whisper.cpp + teclado virtual) que escribe en
la ventana con el foco. Todo el código, la UI, los comentarios, los commits y
el README están **en español**; mantenlo así.

## Arquitectura

Dos entradas que comparten el núcleo Python (sin `pyproject.toml` ni
`requirements.txt` de Linux; las dependencias son paquetes del sistema):

- **Linux (principal)**: paquete `dictavoz/`, UI GTK4, lanzador `run.sh`
  (`python3 -m dictavoz`).
- **Windows**: `packaging/windows/dictavoz_win.py` (Tkinter), empaquetado con
  PyInstaller + NSIS. Reutiliza solo `models.py`, `config.py`, `stt.py` y
  `vad.py`.

Reparto crítico de módulos Linux-only vs compartidos:
- Linux-only (usan `gi`, `/dev/uinput`, `/dev/input`, `parec`): `app.py`,
  `inject.py`, `hotkey.py`, `audio.py`.
- Compartidos con Windows: `models.py`, `config.py`, `stt.py`, `vad.py`,
  `__main__.py` (solo Linux).
- Por eso, **nunca añadas imports de `gi`/GTK ni de `/dev/*` a los módulos
  compartidos**: romperían el build de Windows (el `.spec` excluye `gi`/`gtk`).
- `models.py` (`SEARCH_DIRS`) y `stt.py` (`MODEL_DIRS`) duplican las rutas de
  búsqueda de modelos; si añades una, actualiza ambas.

## Comandos

```bash
./run.sh                      # lanzar (Linux)
./run.sh --self-test          # teclado virtual + modelo + micrófono
./run.sh --smoke-test         # abre y cierra la ventana en 1.5 s
./run.sh --type "Hola"        # escribe texto de prueba y sale
./run.sh --list-sources | --list-layouts | --list-models
./run.sh --download small     # descarga un modelo del catálogo
python3 tools/gen_keymap.py --layout es   # regenerar keymap (requiere xkbcli)
./install.sh                  # instalación opcional en el menú (sin sudo)
```

- **No hay tests, ni lint, ni typecheck.** Verifica con `--self-test`,
  `--smoke-test` o `--type`. No inventes `pytest`/`ruff`/`mypy`.
- `dictavoz/keymaps/latam.json` es un archivo **generado y commiteado**; no lo
  edites a mano, regenéralo con `tools/gen_keymap.py` (necesita `xkbcli`).

## Empaquetado

- **Arch/CachyOS**: `cd packaging/cachyos && makepkg -f`. Al cambiar el fuente
  hay que regenerar `sha256sums` con `makepkg -g` y actualizar `PKGBUILD` y
  `.SRCINFO` (van sincronizados).
- **Windows**: no se compila desde Linux (PyInstaller no cruza). Usa
  `packaging/windows/build_win.bat` o en CI `packaging/windows/dictavoz_win.spec`
  + `installer.nsi`. Define `WHISPER_DIR` para empaquetar `whisper-cli.exe`; sin
  él, el `.exe` no transcribe salvo que `whisper-cli` esté en el PATH.
- **CI**: solo existe `.github/workflows/windows.yml` (se dispara en push a
  `main` o al empujar un tag `v*`). Con un tag `v*` además **publica una
  Release** con `DictaVoz.exe` y `DictaVoz-Setup.exe`; en push a `main` solo
  sube artefactos. `packaging/windows/AZURE_FIRMA.md` describe firma con Azure
  Trusted Signing, pero el workflow **no** la implementa: solo firma con los
  secrets `CODESIGN_PFX_BASE64` / `CODESIGN_PFX_PASSWORD` (autofirmada).
- Salidas de build en `dist/` (ignorado por git).

## Notas operativas

- Config y modelos respetan XDG: `~/.config/dictavoz/config.json` y
  `~/.local/share/dictavoz/models/`. Las claves por defecto están en
  `config.py:DEFAULTS`.
- `stt.py` usa `whisper-server` (puerto `server_port`, 8178) y cae a
  `whisper-cli`. El dictado continuo usa un servidor en `server_port + 1`;
  Windows usa `server_port + 2` y `use_server=False`.
- Var de entorno `DICTAVOZ_WHISPER_CLI` apunta al binario de whisper-cli (la usa
  el build de Windows para el ejecutable empaquetado).
- Requisitos de sistema Linux: `whisper-cpp`, `parec` (PipeWire/PulseAudio),
  `xkbcli`, `python3` con GTK4, grupo `input` y escritura en `/dev/uinput`.
