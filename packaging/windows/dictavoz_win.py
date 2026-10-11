"""DictaVoz para Windows (sin GTK; interfaz Tkinter).

Se empaqueta con PyInstaller (ver dictavoz_win.spec) y se instala con el
instalador NSIS (ver installer.nsi). Comparte el núcleo con la versión Linux:
descarga de modelos, whisper.cpp, VAD, configuración.

Diferencias con Linux:
  - Escribir: SendInput con Unicode (soporta acentos sin keymap).
  - Atajo global: RegisterHotKey (Ctrl+Shift+D por defecto).
  - Audio: sounddevice (WASAPI).
  - STT: whisper-cli.exe (el servidor no se empaqueta en Windows).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave

import tkinter as tk
from tkinter import messagebox, ttk

# ---------------------------------------------------------------- #
# 0. Localizar whisper-cli empaquetado y preparar imports
# ---------------------------------------------------------------- #
_MEIPASS = getattr(sys, "_MEIPASS", None)

_whisper_cli = "whisper-cli"
if _MEIPASS:
    for _candidate in (
        os.path.join(_MEIPASS, "whisper", "whisper-cli.exe"),
        os.path.join(_MEIPASS, "whisper-cli.exe"),
    ):
        if os.path.isfile(_candidate):
            _whisper_cli = _candidate
            break
if sys.platform == "win32":
    _whisper_cli += ".exe"
os.environ.setdefault("DICTAVOZ_WHISPER_CLI", _whisper_cli)

import dictavoz.models as models  # noqa: E402
from dictavoz import config as cfg  # noqa: E402
from dictavoz.stt import Transcriber  # noqa: E402
from dictavoz.vad import UtteranceSplitter  # noqa: E402

TEST_TEXT = "Hola, esto es una prueba de DictaVoz: á é í ó ú ñ ¿qué tal? ¡Adiós!"


# ---------------------------------------------------------------- #
# 1. Escritura con SendInput (Unicode)
# ---------------------------------------------------------------- #
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_SendInput = ctypes.windll.user32.SendInput
_SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_SendInput.restype = wintypes.UINT


def _send_unicode(char: str) -> None:
    scan = ord(char)
    down = INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, scan, KEYEVENTF_UNICODE, 0, None)))
    up = INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, scan, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)))
    _SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    _SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
    time.sleep(0.004)


class WinKeyboard:
    """Escribe texto en la ventana con el foco (SendInput Unicode)."""

    def type_text(self, text: str, append_space: bool = False) -> set[str]:
        # En Windows todos los caracteres Unicode se pueden teclear directamente.
        for char in text:
            if char == "\n":
                _send_unicode("\n")
            elif char == "\t":
                _send_unicode("\t")
            elif not char.isspace() or char == " ":
                _send_unicode(char)
        if append_space:
            _send_unicode(" ")
        return set()

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- #
# 2. Atajo global con RegisterHotKey
# ---------------------------------------------------------------- #
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312

_MOD_FLAGS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "shift": MOD_SHIFT, "meta": MOD_WIN}

_KEY_BY_NAME = {
    "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "espacio": 0x20, "space": 0x20,
    "enter": 0x0D, "retroceso": 0x08, "pausa": 0x13, "bloq despla": 0x91,
    "insert": 0x2D, "supr": 0x2E, "inicio": 0x24, "fin": 0x23,
    "re pag": 0x21, "av pag": 0x22, "bloq num": 0x90,
    "impr pant": 0x2C, "menu": 0x5D,
}


def _key_to_vk(name: str) -> int:
    """Nombre (p. ej. 'D', 'F9', 'Espacio') -> VK de Windows."""
    name = name.strip().lower()
    if name in _KEY_BY_NAME:
        return _KEY_BY_NAME[name]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    if name.startswith("f") and name[1:].isdigit():
        num = int(name[1:])
        return 0x70 + (num - 1) if 1 <= num <= 24 else 0
    return 0


def format_combo(text: str) -> str:
    """''Ctrl+Shift+D'' -> forma canónica ''Ctrl+Shift+D'' (capitalizada)."""
    order = ("alt", "ctrl", "shift", "meta", "super", "win")
    mods, key = [], ""
    for piece in str(text).split("+"):
        token = piece.strip().lower()
        if token in ("ctrl", "alt", "shift", "meta", "super", "win"):
            if token not in mods:
                mods.append(token)
        else:
            key = token
    parts = []
    for name in order:
        if name in mods:
            parts.append({"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift",
                          "meta": "Meta", "super": "Super", "win": "Win"}[name])
    parts.append(key.capitalize() if key else "F9")
    return "+".join(parts)


def parse_combo(text: str) -> tuple[int, int]:
    """'Ctrl+Shift+D' -> (modflags, vk)."""
    mods, key = 0, ""
    for piece in str(text).split("+"):
        token = piece.strip().lower()
        if token in _MOD_FLAGS:
            mods |= _MOD_FLAGS[token]
        elif token in ("super", "win"):
            mods |= MOD_WIN
        else:
            key = token
    return mods, _key_to_vk(key)


class WinHotkey:
    """Registra un atajo global; llama on_press desde el hilo de mensajes."""

    def __init__(self, combo: str, on_press, on_release):
        self.combo = combo
        self.on_press = on_press
        self.on_release = on_release
        self._id = 0x4456  # "DVOZ"
        self._stop = threading.Event()
        self._modflags, self._vk = parse_combo(combo)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def set_combo(self, combo: str) -> None:
        self.combo = combo
        self._modflags, self._vk = parse_combo(combo)
        self._re_register()

    def _re_register(self) -> None:
        user32 = ctypes.windll.user32
        user32.UnregisterHotKey(None, self._id)
        user32.RegisterHotKey(None, self._id, self._modflags, self._vk)

    def _loop(self) -> None:
        user32 = ctypes.windll.user32
        user32.RegisterHotKey(None, self._id, self._modflags, self._vk)
        msg = wintypes.MSG()
        while not self._stop.is_set():
            res = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if res <= 0:
                break
            if msg.message == WM_HOTKEY and msg.wParam == self._id:
                self.on_press()
        user32.UnregisterHotKey(None, self._id)

    def stop(self) -> None:
        self._stop.set()
        try:
            ctypes.windll.user32.PostThreadMessageW(
                self._thread.native_id, 0x0012, 0, 0  # WM_QUIT
            )
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- #
# 3. Aplicación
# ---------------------------------------------------------------- #
class DictaVozWin:
    STATE_IDLE, STATE_REC, STATE_WORK = "idle", "rec", "work"

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("DictaVoz")
        root.minsize(460, 420)

        icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "icons", "dictavoz.ico")
        if os.path.isfile(icon):
            try:
                root.iconbitmap(icon)
            except tk.TclError:
                pass

        self.settings = cfg.load()
        self.state = self.STATE_IDLE
        self.keyboard = WinKeyboard()
        self.transcriber = Transcriber(
            model=self.settings["model"],
            language=self.settings["language"],
            threads=int(self.settings["threads"]),
            port=int(self.settings["server_port"]) + 2,
            use_server=False,
        )
        self.stream_transcriber: Transcriber | None = None

        self._queue: queue.Queue = queue.Queue()
        self._audio = None
        self._splitter = None
        self._worker = None
        self._rec_buffer = bytearray()
        self._stream_text = ""
        self._last_text = ""
        self._downloading = False
        self._cancel_download = threading.Event()

        self.hotkey_combo = self.settings["hotkey"]
        self.hotkey = None
        self._build_ui()
        self._start_hotkey()

    # ------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 4}
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="DictaVoz", font=("", 16, "bold")).pack(anchor="w")
        ttk.Label(
            main, text="Habla y el texto aparece donde tengas el cursor (Windows)."
        ).pack(anchor="w", **pad)

        self.toggle_btn = ttk.Button(main, text="Activar dictado", command=self._toggle)
        self.toggle_btn.pack(fill="x", **pad)

        self.status = tk.StringVar(value="Listo.")
        ttk.Label(main, textvariable=self.status, wraplength=440).pack(
            anchor="w", fill="x", **pad
        )

        opts = ttk.Frame(main)
        opts.pack(fill="x", **pad)

        row1 = ttk.Frame(opts)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Modo:").pack(side="left")
        self.mode_var = tk.StringVar(value=self.settings["mode"])
        ttk.Radiobutton(row1, text="Mantener tecla", value="hold",
                        variable=self.mode_var,
                        command=self._save_mode).pack(side="left", padx=6)
        ttk.Radiobutton(row1, text="Pulsar", value="toggle",
                        variable=self.mode_var,
                        command=self._save_mode).pack(side="left", padx=6)
        self.stream_var = tk.BooleanVar(value=bool(self.settings["streaming"]))
        ttk.Checkbutton(row1, text="Escritura continua",
                        variable=self.stream_var,
                        command=self._save_streaming).pack(side="left", padx=6)

        row2 = ttk.Frame(opts)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="Atajo:").pack(side="left")
        self.hotkey_var = tk.StringVar(value=self.settings["hotkey"])
        ttk.Entry(row2, textvariable=self.hotkey_var, width=16).pack(side="left", padx=4)
        ttk.Button(row2, text="Aplicar", command=self._apply_hotkey).pack(side="left")

        row3 = ttk.Frame(opts)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="Idioma:").pack(side="left")
        self.lang_var = tk.StringVar(value=self.settings["language"])
        ttk.Combobox(row3, textvariable=self.lang_var, values=("es", "en", "auto"),
                     width=6, state="readonly").pack(side="left", padx=4)
        ttk.Label(row3, text="Micrófono:").pack(side="left", padx=(12, 0))
        self.device_var = tk.StringVar()
        ttk.Combobox(row3, textvariable=self.device_var, width=22).pack(side="left", padx=4)

        row4 = ttk.Frame(opts)
        row4.pack(fill="x", pady=2)
        ttk.Label(row4, text="Modelo:").pack(side="left")
        self.model_combo = ttk.Combobox(row4, state="readonly", width=34)
        self.model_combo.pack(side="left", padx=4)
        self.model_combo.bind("<<ComboboxSelected>>", lambda _e: self._model_selected())
        ttk.Button(row4, text="Descargar", command=self._download_selected).pack(side="left")
        ttk.Button(row4, text="Eliminar", command=self._delete_selected).pack(side="left")

        self.progress = ttk.Progressbar(main, maximum=100)
        self.progress.pack(fill="x", **pad)
        self.progress.pack_forget()

        self.total_label = tk.StringVar()
        ttk.Label(main, textvariable=self.total_label).pack(anchor="w", **pad)

        ttk.Label(main, text="Pruébalo aquí:").pack(anchor="w")
        self.test_entry = ttk.Entry(main)
        self.test_entry.pack(fill="x", **pad)
        ttk.Button(main, text="Escribir texto de prueba",
                   command=self._test_write).pack(anchor="w", **pad)

        ttk.Label(main, text="Última transcripción:").pack(anchor="w")
        self.transcript = tk.Text(main, height=4, state="disabled", wrap="word")
        self.transcript.pack(fill="both", expand=True, **pad)

        self._refresh_models()
        self._refresh_devices()
        self._refresh_total()

    # ---------------------------------------------------- modelos
    def _model_labels(self):
        return [
            f"{e['label']}  {'✓ ('+models.human_size(models.file_size(e))+')' if models.is_downloaded(e) else '· '+models.human_size(e['bytes'])}"
            for e in models.MODELS
        ]

    def _refresh_models(self):
        self.model_combo["values"] = self._model_labels()
        # seleccionar el modelo activo si existe
        entry = models.entry_for_path(self.settings["model"])
        idx = models.MODELS.index(entry) if entry else 2
        self.model_combo.current(min(idx, len(models.MODELS) - 1))

    def _selected_entry(self):
        return models.MODELS[max(0, self.model_combo.current())]

    def _model_selected(self):
        entry = self._selected_entry()
        existing = models.find_existing(entry["file"])
        if existing:
            self.settings["model"] = existing
            cfg.save(self.settings)
            self.transcriber.close()
            self.transcriber = Transcriber(
                model=existing, language=self.settings["language"],
                threads=int(self.settings["threads"]),
                port=int(self.settings["server_port"]) + 2, use_server=False,
            )

    def _download_selected(self):
        entry = self._selected_entry()
        if models.is_downloaded(entry):
            self._set_status(f"«{entry['key']}» ya está descargado.")
            return
        if self._downloading:
            return
        self._downloading = True
        self._cancel_download.clear()
        self.progress.pack(fill="x", pady=2)
        self.progress["value"] = 0
        threading.Thread(target=self._dl_worker, args=(entry,), daemon=True).start()

    def _dl_worker(self, entry):
        def prog(done, total):
            self._after(0, lambda: self.progress.configure(value=0 if not total else min(100, done * 100 / total)))

        try:
            path = models.download(entry, progress=prog, cancel=self._cancel_download.is_set)
            self._after(0, self._dl_done, entry, path, None)
        except models.DownloadCancelled:
            self._after(0, self._dl_done, entry, None, "cancelada")
        except Exception as exc:
            self._after(0, self._dl_done, entry, None, str(exc))

    def _dl_done(self, entry, path, error):
        self._downloading = False
        self.progress.pack_forget()
        if error:
            self._set_status(f"⚠️ Descarga de «{entry['key']}»: {error}")
        else:
            self._set_status(f"✅ Modelo «{entry['key']}» descargado.")
        self._refresh_models()
        self._refresh_total()

    def _delete_selected(self):
        entry = self._selected_entry()
        if not models.is_downloaded(entry):
            self._set_status("Este modelo no está descargado.")
            return
        if not messagebox.askyesno(
            "Eliminar modelo",
            f"¿Eliminar «{entry['key']}» ({models.human_size(models.file_size(entry))})?",
        ):
            return
        removed = models.delete(entry)
        if not removed:
            self._set_status("⚠️ No se pudo eliminar (¿permisos o ruta de sistema?).")
            return
        if models.entry_for_path(self.settings["model"]) == entry:
            self.settings["model"] = ""
            cfg.save(self.settings)
            self.transcriber.close()
            self.transcriber = Transcriber(
                model="", language=self.settings["language"],
                threads=int(self.settings["threads"]),
                port=int(self.settings["server_port"]) + 2, use_server=False,
            )
        self._refresh_models()
        self._refresh_total()
        self._set_status(f"✅ Modelo «{entry['key']}» eliminado.")

    def _refresh_total(self):
        total = models.total_downloaded_bytes()
        self.total_label.set(
            f"Ocupado por modelos descargados: {models.human_size(total)}"
            if total else "No hay modelos descargados."
        )

    # ---------------------------------------------------- audio
    def _refresh_devices(self):
        try:
            import sounddevice as sd
            self._devices = [(d["name"], d["index"]) for d in sd.query_devices() if d["max_input_channels"] > 0]
        except Exception:  # noqa: BLE001
            self._devices = []
        names = [n for n, _ in self._devices]
        self.device_var.set(self.settings["source"] or (names[0] if names else ""))
        if names:
            self._dev_device = self._devices[0]

    def _open_audio(self, on_chunk):
        import sounddevice as sd

        def cb(indata, frames, time_info, status):
            on_chunk(indata.tobytes())

        device = None
        if self._devices:
            device = self._devices[0][1]
        stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                                blocksize=1024, device=device, callback=cb)
        stream.start()
        return stream

    # ---------------------------------------------------- flujo
    def _start_hotkey(self):
        self.hotkey = WinHotkey(self.hotkey_combo, self._on_hotkey_press, lambda: None)

    def _on_hotkey_press(self):
        self._after(0, self._hotkey_ui)

    def _hotkey_ui(self):
        if self.state == self.STATE_IDLE:
            self._start()
        elif self.state == self.STATE_REC:
            if self.settings["mode"] == "toggle":
                self._stop()

    def _apply_hotkey(self):
        combo = self.hotkey_var.get().strip()
        if not combo:
            return
        new = format_combo(combo)
        self.settings["hotkey"] = new
        self.hotkey_combo = new
        cfg.save(self.settings)
        if self.hotkey:
            self.hotkey.set_combo(new)
        self.hotkey_var.set(new)
        self._set_status(f"Atajo cambiado a {new}.")

    def _toggle(self):
        if self.state == self.STATE_IDLE:
            self._start()
        elif self.state == self.STATE_REC:
            self._stop()

    def _save_mode(self):
        self.settings["mode"] = self.mode_var.get()
        cfg.save(self.settings)

    def _save_streaming(self):
        self.settings["streaming"] = bool(self.stream_var.get())
        cfg.save(self.settings)

    def _start(self):
        if not self.transcriber.available:
            self._set_status("⚠️ No hay ningún modelo. Descarga uno.")
            return
        self.state = self.STATE_REC
        self._rec_buffer = bytearray()
        self._stream_text = ""
        try:
            if self.settings["streaming"]:
                self._splitter = UtteranceSplitter(min_silence_ms=650, max_utterance_ms=10000)
                self._queue = queue.Queue()

                def on_chunk(data):
                    for chunk in self._splitter.feed(data):
                        self._queue.put(chunk)

                self._audio = self._open_audio(on_chunk)
                self._worker = threading.Thread(target=self._stream_worker, daemon=True)
                self._worker.start()
            else:
                self._audio = self._open_audio(self._rec_buffer.extend)
        except Exception as exc:
            self.state = self.STATE_IDLE
            self._set_status(f"⚠️ No se pudo acceder al micrófono: {exc}")
            return
        self._set_status("🔴 Grabando… habla ahora (Ctrl+Shift+D para parar).")
        if self.settings["mode"] == "hold":
            self._hold_watch()

    def _hold_watch(self):
        """Modo mantener: para al soltar el atajo."""
        _, vk = parse_combo(self.hotkey_combo)
        GetKeyState = ctypes.windll.user32.GetKeyState

        def watch():
            while self.state == self.STATE_REC:
                try:
                    blocked = GetKeyState(vk) & 0x8000
                except Exception:  # noqa: BLE001
                    break
                if not blocked:
                    self._after(0, self._stop)
                    break
                time.sleep(0.05)

        threading.Thread(target=watch, daemon=True).start()

    def _stop(self):
        if self.state != self.STATE_REC:
            return
        self.state = self.STATE_WORK
        self._set_status("⏳ Transcribiendo…")
        try:
            if self._audio is not None:
                self._audio.stop()
                self._audio.close()
        except Exception:  # noqa: BLE001
            pass
        self._audio = None
        if self.settings["streaming"] and self._splitter is not None:
            for chunk in self._splitter.flush():
                self._queue.put(chunk)
            self._queue.put(None)
        else:
            data = bytes(self._rec_buffer)
            threading.Thread(target=self._once_worker, args=(data,), daemon=True).start()

    def _stream_worker(self):
        while True:
            chunk = self._queue.get()
            if chunk is None:
                break
            try:
                text = self._transcribe(chunk)
                if text:
                    self.keyboard.type_text(text, append_space=True)
                    self._after(0, self._stream_typed, text)
            except Exception as exc:  # noqa: BLE001
                self._after(0, self._set_status, f"⚠️ Error: {exc}")
        self._after(0, self._finish)

    def _once_worker(self, data):
        text = ""
        try:
            if len(data) >= 16000 * 2 * 0.4:  # >= 0,4 s
                text = self._transcribe(data)
                if text:
                    self.keyboard.type_text(text, append_space=bool(self.settings["append_space"]))
        except Exception as exc:  # noqa: BLE001
            self._after(0, self._set_status, f"⚠️ Error: {exc}")
        self._after(0, self._finish, text)

    def _transcribe(self, data: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="dictavoz-")
        os.close(fd)
        try:
            with wave.open(path, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(data)
            return self.transcriber.transcribe(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _stream_typed(self, text):
        self._stream_text = (self._stream_text + " " + text).strip()
        self._append_transcript(text)
        self._set_status("✅ Escrito: " + text[:80])

    def _finish(self, text=None):
        self.state = self.STATE_IDLE
        if text is not None and text:
            self._append_transcript(text)
        if self._stream_text or (text and text):
            self._set_status(f"✅ Dictado terminado (total {len(self._stream_text or text)} car.).")
        else:
            self._set_status("No se detectó habla.")
        self._refresh_button()

    def _append_transcript(self, text):
        self.transcript.configure(state="normal")
        if self._last_text:
            self.transcript.insert("end", " " + text)
        else:
            self.transcript.insert("end", text)
        self._last_text += text
        self.transcript.configure(state="disabled")

    def _refresh_button(self):
        self.toggle_btn.configure(
            text="Detener y escribir" if self.state == self.STATE_REC else "Activar dictado"
        )

    def _test_write(self):
        self.keyboard.type_text(TEST_TEXT, append_space=True)
        self._set_status("Texto de prueba escrito.")

    def _after(self, delay, func, *args):
        """Programa en el hilo de Tk ignorando si la ventana ya se cerró."""
        try:
            self.root.after(delay, lambda: func(*args))
        except Exception:  # noqa: BLE001 - ventana cerrada
            pass

    def _set_status(self, text):
        self.status.set(text)

    def close(self):
        if self.state == self.STATE_REC:
            self._stop()
        if self.hotkey:
            self.hotkey.stop()
        try:
            self.transcriber.close()
        except Exception:  # noqa: BLE001
            pass


def main():
    root = tk.Tk()
    app = DictaVozWin(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.close(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()