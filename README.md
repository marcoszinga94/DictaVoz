# DictaVoz

Dictado por voz para Linux (Wayland y X11). Hablas por el micrófono y el
texto aparece **donde tengas el cursor**, en cualquier aplicación. Todo el
reconocimiento de voz se hace **en local** con `whisper.cpp`: no se envía
nada a internet y no hace falta instalar nada con `sudo`.

Creado para CachyOS / KDE Plasma (Wayland) con layout de teclado `latam`,
pero configurable para otros layouts.

## Cómo funciona

```
micrófono ──parec──► WAV ──whisper.cpp──► texto ──/dev/uinput──► se escribe en el cursor
                        (STT local)         (teclado virtual)
```

- **Captura**: `parec` (PipeWire/PulseAudio) graba a 16 kHz mono.
- **STT**: `whisper.cpp` (`whisper-server` mantiene el modelo cargado en
  memoria para responder rápido; si falla, usa `whisper-cli`).
- **Escritura**: un teclado virtual por `/dev/uinput` emite pulsaciones
  reales, así que funciona en Wayland sin `ydotool`/`xdotool`.
- **Atajo global**: se detecta leyendo los teclados en `/dev/input/event*`
  (necesita tu usuario en el grupo `input`, como ya lo tienes).

## Requisitos (ya presentes en tu equipo)

- `whisper-cpp` (incluye `whisper-cli` y `whisper-server`).
- Un modelo `ggml-*.bin` (tienes `~/.local/share/whisper-cpp/ggml-small.bin`).
- `parec` (PipeWire o PulseAudio), `xkbcli` (xkbcommon) y `python3` con GTK4.
- Permiso de escritura en `/dev/uinput` y pertenencia al grupo `input`.

Comprueba que todo está en orden:

```bash
./run.sh --self-test
```

## Uso rápido

```bash
./run.sh
```

En la ventana eliges modo, idioma y micrófono, y ves la última transcripción.
Para probar, haz clic en el campo **«Pruébalo aquí»** y usa el atajo.

### Modos

- **Mantener tecla** (por defecto): mantén pulsada la tecla (Ctrl+Shift+D por
  defecto), habla y suelta. Entonces se transcribe y se escribe.
- **Pulsar para iniciar/parar**: pulsa una vez para empezar a grabar y otra
  vez para parar y escribir.

El atajo por defecto es **Ctrl+Shift+D** y funciona aunque la ventana no tenga
el foco. Con el botón **Cambiar** puedes reasignarlo: mantén los modificadores
y pulsa la tecla (p. ej. **Ctrl+Alt+F9** o **Ctrl+Shift+V**). Desde la terminal
también se puede fijar con `--hotkey "Ctrl+Alt+F9"`.

> Al dictar, si haces una pausa whispier a veces escribe puntos suspensivos
> ("…"). DictaVoz los elimina automáticamente (no se escriben).

### Escritura continua (dictado en vivo)

Con el interruptor **«Escritura continua»** activado (por defecto), el texto se
va escribiendo **a medida que hablas** en vez de todo al final:

- Se detectan las frases por pausas (~0,7 s de silencio) y cada frase se
  transcribe y escribe en cuanto terminas de decirla.
- Si hablas sin pausar, se escribe igualmente un trozo cada ~10 s
  (`vad_max_utterance_ms`).
- Para que sea ágil, esta opción usa **automáticamente un modelo pequeño y
  rápido** (config `streaming_model`, por defecto `base`; se descarga solo si
  falta). Tu modelo grande sigue seleccionable para el modo normal.
- Apagando el interruptor, vuelve al comportamiento de antes: graba todo y
  transcribe al soltar el atajo.

Ajustes en `~/.config/dictavoz/config.json`: `vad_min_silence_ms`,
`vad_max_utterance_ms` y `streaming_model`.

## Opciones de línea de comandos

```bash
./run.sh --hotkey "Ctrl+Shift+D" --mode hold  # atajo y modo
./run.sh --language es                 # es | en | auto | ...
./run.sh --source "alsa_input.…"       # micrófono concreto (ver --list-sources)
./run.sh --model /ruta/ggml-small.bin  # modelo concreto

./run.sh --list-sources                # lista micrófonos
./run.sh --list-layouts                # lista mapas de teclado disponibles
./run.sh --self-test                   # comprueba teclado, modelo y micrófono
./run.sh --type "Hola, ¿qué tal?"      # escribe un texto de prueba y sale
```

## Configuración

Se guarda en `~/.config/dictavoz/config.json`. Desde la ventana se
actualiza automáticamente. Claves: `hotkey`, `mode`, `language`, `model`,
`source`, `threads`, `layout`, `append_space`, `server_port`, `min_seconds`.

## Modelos de voz

En la ventana, el desplegable **Modelo** lista varios modelos. Los que ya están
descargados aparecen con **✓** y su tamaño en disco; el resto muestran cuánto
ocuparían. Debajo se muestra el **total ocupado** por todos los modelos.
Con el botón **Eliminar modelo** puedes borrar el seleccionado (pide
confirmación; si es el modelo en uso, la app cambia solo a otro disponible).
Los modelos de los directorios del sistema (`/usr/share`) nunca se borran.
Al elegir uno que no está descargado, **se descarga automáticamente** desde
Hugging Face (`ggerganov/whisper.cpp`) con barra de progreso y botón de
cancelar. Los modelos se guardan en `~/.local/share/dictavoz/models/`.

| Modelo | Tamaño | Notas |
| --- | --- | --- |
| Tiny | ~74 MB | Muy rápido, calidad básica |
| Base | ~141 MB | Rápido, calidad aceptable |
| Small | ~466 MB | Equilibrado, **recomendado** |
| Medium | ~1.4 GB | Más preciso, más lento |
| Large v3 Turbo | ~1.5 GB | Muy preciso y rápido |
| Large v3 Turbo Q5 | ~547 MB | Preciso, ocupa menos (cuantizado) |
| Large v3 | ~2.9 GB | Máxima precisión |

También desde la terminal:

```bash
./run.sh --list-models          # lista modelos, su tamaño y el total ocupado
./run.sh --download small       # descarga un modelo del catálogo
./run.sh --model large-v3-turbo # usa (y descarga si hace falta) un modelo
```

> Cuanto mayor el modelo, mejor calidad pero más lento y más memoria. En un
> CPU como el tuyo, `small` o `large-v3-turbo` suelen ser el mejor equilibrio.

## Acentos, ñ y símbolos

El teclado virtual reproduce tu layout real (`latam`). El mapa de teclas
`dictavoz/keymaps/latam.json` se genera automáticamente y cubre letras,
números, signos y las vocales acentuadas (á é í ó ú ü), `ñ`/`Ñ` y `¿`/`¡`
usando las teclas muertas del layout.

Para otro layout (p. ej. `us` o `es`):

```bash
python3 tools/gen_keymap.py --layout es
./run.sh --layout es
```

## Instalar en el menú de aplicaciones (opcional)

```bash
./install.sh
```

Crea `~/.local/bin/dictavoz` y una entrada «DictaVoz» en el menú.

## Empaquetado

Todo lo construido/empaquetado está en `dist/` (y el código en `packaging/`):

- **CachyOS / Arch** — `dist/dictavoz-0.1.0-1-x86_64.pkg.tar.zst`
  (instalar: `sudo pacman -U dist/dictavoz-0.1.0-1-x86_64.pkg.tar.zst`).
  También `PKGBUILD` y `.SRCINFO` listos para publicar en el **AUR**.
  Dependencias: `python`, `python-gobject`, `gtk4`, `whisper-cpp`,
  `libpulse`, `libxkbcommon`.
  Reconstruir: `cd packaging/cachyos && makepkg -f`.
- **Windows** — no se compila desde Linux (PyInstaller no cruza). Preparado:
  - `packaging/windows/dictavoz_win.py` — interfaz Tk + backends Windows
    (SendInput para escribir, RegisterHotKey para el atajo, sounddevice para
    el audio, whisper-cli.exe empaquetado).
  - `dictavoz_win.spec` (PyInstaller), `installer.nsi` (NSIS) y
    `build_win.bat` para compilar en un PC con Windows.
  - `.github/workflows/windows.yml` — **GitHub Actions**: al pushear el
    proyecto a GitHub, genera `dist/DictaVoz-Setup.exe` automáticamente
    (artefacto descargable).

## Solución de problemas

- **«No se puede abrir /dev/uinput»**: tu usuario necesita escritura en ese
  dispositivo (ACL, grupo o regla udev).
- **El atajo global no responde**: comprueba que estás en el grupo `input`
  (`id`), y que has vuelto a iniciar sesión tras añadirte.
- **Escribe pero no salen los acentos**: el layout configurado no coincide
  con el del sistema. Regenera el mapa con tu layout real (`localectl status`).
- **No escribe en la aplicación**: haz clic en el campo de texto para darle
  el foco antes de dictar; el texto va a la ventana con el foco.
- **Tarda en la primera frase**: `whisper-server` está cargando el modelo
  (~2 s). Después de la primera vez, la respuesta es casi inmediata.
- **Reconoce mal**: usa un modelo mayor (`ggml-medium.bin`) o revisa el
  micrófono (`./run.sh --list-sources`).

## Estructura

```
DictaVoz/
├── run.sh                     # lanzador
├── install.sh                 # instalación opcional en el menú
├── dictavoz/
│   ├── __main__.py            # CLI y arranque
│   ├── app.py                 # UI GTK4 y lógica de dictado
│   ├── audio.py               # captura del micrófono (parec)
│   ├── stt.py                 # whisper.cpp (servidor o CLI)
│   ├── inject.py              # teclado virtual (/dev/uinput)
│   ├── hotkey.py              # atajo global (/dev/input)
│   ├── config.py              # configuración persistente
│   └── keymaps/latam.json     # mapa de teclas generado
└── tools/gen_keymap.py        # genera mapas para otros layouts
```

Desarrollado por **Marcos Zingaretti**. MIT License.
