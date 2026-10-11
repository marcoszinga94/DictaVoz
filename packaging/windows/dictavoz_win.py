"""DictaVoz para Windows (sin GTK; interfaz Tkinter).

Se empaqueta con PyInstaller (ver dictavoz_win.spec) y se instala con el
instalador NSIS (ver installer.nsi). Comparte el núcleo con la versión Linux:
descarga de modelos, whisper.cpp, VAD, configuración.

Diferencias con Linux:
  - Escribir: SendInput con Unicode (soporta acentos sin keymap).
  - Atajo global: RegisterHotKey (Ctrl+Alt+Y por defecto).
  - Audio: sounddevice (WASAPI).
  - STT: whisper-cli.exe (el servidor no se empaqueta en Windows).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import wave
from tkinter import messagebox, ttk

# ---------------------------------------------------------------- #
# 0. Recursos empaquetados (icono, whisper-cli) e imports del núcleo
# ---------------------------------------------------------------- #
_MEIPASS = getattr(sys, "_MEIPASS", None)


def _resource_path(*parts: str) -> str:
    """Ruta a un recurso, tanto en desarrollo como dentro del .exe (onefile)."""
    base = _MEIPASS if _MEIPASS else os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    return os.path.join(base, *parts)


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

# Permite ejecutar este archivo directamente (py packaging/windows/dictavoz_win.py).
if not _MEIPASS:
    _root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if _root not in sys.path:
        sys.path.insert(0, _root)

import dictavoz.models as models  # noqa: E402
from dictavoz import __version__, config as cfg  # noqa: E402
from dictavoz.stt import Transcriber  # noqa: E402
from dictavoz.vad import UtteranceSplitter  # noqa: E402

TEST_TEXT = "Hola, esto es una prueba de DictaVoz: á é í ó ú ñ ¿qué tal? ¡Adiós!"


def enable_dpi_awareness() -> None:
    """Ventana nítida en pantallas con escalado (evita el texto borroso)."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:  # noqa: BLE001 - Windows antiguo
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- #
# 1. Escritura con SendInput (Unicode)
# ---------------------------------------------------------------- #
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002

VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D

_MOD_KEYS = {
    "ctrl": 0x11,   # VK_CONTROL
    "alt": 0x12,    # VK_MENU
    "shift": 0x10,  # VK_SHIFT
    "meta": 0x5B,   # VK_LWIN
    "super": 0x5B,
    "win": 0x5B,
}


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


def _send_scan(scan: int) -> None:
    down = INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, scan, KEYEVENTF_UNICODE, 0, None)))
    up = INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, scan, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)))
    _SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    _SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


def _send_char(char: str) -> None:
    """Escribe un carácter Unicode (incluye emoji con pares subrogados)."""
    code = ord(char)
    if code > 0xFFFF:
        data = char.encode("utf-16-le")
        for index in range(0, len(data), 2):
            _send_scan(int.from_bytes(data[index:index + 2], "little"))
    else:
        _send_scan(code)
    time.sleep(0.004)


def _send_vk(vk: int) -> None:
    """Pulsa una tecla virtual (Enter, Tab, Retroceso…)."""
    down = INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, 0, 0, 0, None)))
    up = INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None)))
    _SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    _SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
    time.sleep(0.004)


class WinKeyboard:
    """Escribe texto en la ventana con el foco (SendInput Unicode)."""

    def type_text(self, text: str, append_space: bool = False) -> set[str]:
        # En Windows todos los caracteres Unicode se pueden teclear directamente.
        for char in text:
            if char in ("\n", "\r"):
                _send_vk(VK_RETURN)
            elif char == "\t":
                _send_vk(VK_TAB)
            elif char.isspace() and char != " ":
                continue
            else:
                _send_char(char)
        if append_space:
            _send_char(" ")
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
WM_APP_RELOAD = 0x8000 + 1
WM_QUIT = 0x0012

_MOD_FLAGS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "shift": MOD_SHIFT, "meta": MOD_WIN}

_KEY_BY_NAME = {
    "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "espacio": 0x20, "space": 0x20,
    "enter": 0x0D, "intro": 0x0D, "retroceso": 0x08, "backspace": 0x08,
    "pausa": 0x13, "pause": 0x13, "bloq despla": 0x91, "scrolllock": 0x91,
    "insert": 0x2D, "supr": 0x2E, "delete": 0x2E, "inicio": 0x24, "home": 0x24,
    "fin": 0x23, "end": 0x23, "re pag": 0x21, "pageup": 0x21,
    "av pag": 0x22, "pagedown": 0x22, "bloq num": 0x90, "numlock": 0x90,
    "impr pant": 0x2C, "printscreen": 0x2C, "menu": 0x5D,
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
    """''Ctrl+Alt+Y'' -> forma canónica ''Ctrl+Alt+Y'' (capitalizada)."""
    order = ("ctrl", "alt", "shift", "meta", "super", "win")
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
    """'Ctrl+Alt+Y' -> (modflags, vk)."""
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
    """Registra un atajo global; llama on_press desde el hilo de mensajes.

    El registro se asocia al hilo que lo crea, así que reasignar el atajo se
    hace enviando un mensaje a ese mismo hilo (no desde el hilo de Tk).
    """

    def __init__(self, combo: str, on_press, on_error=None):
        self.combo = combo
        self.on_press = on_press
        self.on_error = on_error
        self._id = 0x4456  # "DVOZ"
        self._stop = threading.Event()
        self._modflags, self._vk = parse_combo(combo)
        self.registered = False
        self._thread = threading.Thread(target=self._loop, daemon=True, name="win-hotkey")
        self._thread.start()

    def set_combo(self, combo: str) -> None:
        self.combo = combo
        self._modflags, self._vk = parse_combo(combo)
        thread_id = self._thread.native_id
        if thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(thread_id, WM_APP_RELOAD, 0, 0)
                return
            except Exception:  # noqa: BLE001
                pass
        # El hilo aún no tiene cola: se registrará con los nuevos valores al arrancar.

    def _loop(self) -> None:
        user32 = ctypes.windll.user32

        def register() -> None:
            if self.registered:
                user32.UnregisterHotKey(None, self._id)
                self.registered = False
            ok = user32.RegisterHotKey(None, self._id, self._modflags, self._vk)
            self.registered = bool(ok)
            if not ok and self.on_error:
                try:
                    self.on_error(self.combo)
                except Exception:  # noqa: BLE001
                    pass

        register()
        msg = wintypes.MSG()
        while not self._stop.is_set():
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result <= 0:
                break
            if msg.message == WM_HOTKEY and msg.wParam == self._id:
                try:
                    self.on_press()
                except Exception:  # noqa: BLE001
                    pass
            elif msg.message == WM_APP_RELOAD:
                register()
        if self.registered:
            user32.UnregisterHotKey(None, self._id)

    def stop(self) -> None:
        self._stop.set()
        thread_id = self._thread.native_id
        if thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
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
        root.minsize(560, 620)
        root.configure(bg="#f5f6f8")
        try:
            icon = _resource_path("icons", "dictavoz.ico")
            if os.path.isfile(icon):
                root.iconbitmap(icon)
        except tk.TclError:
            pass
        self._center_window(620, 700)
        self._header_icon = None

        self.settings = cfg.load()
        self.state = self.STATE_IDLE
        self.keyboard = WinKeyboard()
        self.transcriber = self._new_transcriber()
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
        self._devices: list[tuple[str, int]] = []
        self._selected_device_index = None

        self.hotkey_combo = self.settings["hotkey"]
        self.hotkey = None

        self._setup_style()
        self._build_ui()
        self._start_hotkey()
        if self.settings.get("streaming"):
            self._ensure_stream_model_async()

    # ------------------------------------------------------------ estilo
    def _setup_style(self) -> None:
        try:
            self.root.tk.call("tk", "scaling", self.root.winfo_fpixels("1i") / 72.0)
        except tk.TclError:
            pass
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure(".", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("Sub.TLabel", foreground="#6b7280")
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"), padding=7)
        style.configure("Footer.TLabel", foreground="#9ca3af", font=("Segoe UI", 9))

    def _center_window(self, width: int, height: int) -> None:
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(width, screen_w - 40)
        height = min(height, screen_h - 80)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _load_header_icon(self):
        try:
            path = _resource_path("icons", "dictavoz.png")
            if not os.path.isfile(path):
                return None
            image = tk.PhotoImage(file=path)
            factor = max(1, image.width() // 40)
            if factor > 1:
                image = image.subsample(factor, factor)
            self._header_icon = image  # evita que el GC lo libere
            return image
        except Exception:  # noqa: BLE001 - el icono es opcional
            return None

    # ------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=14)
        main.pack(fill="both", expand=True)

        # Cabecera: icono + título.
        header = ttk.Frame(main)
        header.pack(fill="x")
        icon = self._load_header_icon()
        if icon is not None:
            ttk.Label(header, image=icon).pack(side="left", padx=(0, 10))
        titles = ttk.Frame(header)
        titles.pack(side="left", fill="x", expand=True)
        ttk.Label(titles, text="DictaVoz", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            titles,
            text="Habla y el texto aparece donde tengas el cursor.",
            style="Sub.TLabel",
        ).pack(anchor="w")

        # Botón principal y estado.
        self.toggle_btn = ttk.Button(
            main, text="Activar dictado", style="Accent.TButton", command=self._toggle
        )
        self.toggle_btn.pack(fill="x", pady=(12, 6))

        self.status = tk.StringVar(value="Listo.")
        self.status_label = ttk.Label(
            main, textvariable=self.status, wraplength=560, justify="left"
        )
        self.status_label.pack(anchor="w", fill="x", pady=(0, 8))

        # Sección: dictado.
        dict_frame = ttk.LabelFrame(main, text=" Dictado ", padding=10)
        dict_frame.pack(fill="x", pady=(0, 8))
        dict_frame.columnconfigure(1, weight=1)

        ttk.Label(dict_frame, text="Modo:").grid(row=0, column=0, sticky="w", pady=3)
        modo = ttk.Frame(dict_frame)
        modo.grid(row=0, column=1, sticky="w")
        self.mode_var = tk.StringVar(value=self.settings["mode"])
        ttk.Radiobutton(modo, text="Mantener tecla", value="hold",
                        variable=self.mode_var, command=self._save_mode).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(modo, text="Pulsar para empezar/parar", value="toggle",
                        variable=self.mode_var, command=self._save_mode).pack(side="left")

        self.stream_var = tk.BooleanVar(value=bool(self.settings["streaming"]))
        ttk.Checkbutton(
            dict_frame,
            text="Escritura continua (escribe mientras hablas)",
            variable=self.stream_var,
            command=self._save_streaming,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=3)

        ttk.Label(dict_frame, text="Atajo:").grid(row=2, column=0, sticky="w", pady=3)
        hotkey_row = ttk.Frame(dict_frame)
        hotkey_row.grid(row=2, column=1, sticky="ew")
        self.hotkey_var = tk.StringVar(value=self.settings["hotkey"])
        self.hotkey_entry = ttk.Entry(hotkey_row, textvariable=self.hotkey_var, width=16)
        self.hotkey_entry.pack(side="left")
        self.hotkey_entry.bind("<Return>", lambda _e: self._apply_hotkey())
        ttk.Button(hotkey_row, text="Aplicar", command=self._apply_hotkey).pack(side="left", padx=6)

        ttk.Label(dict_frame, text="Idioma:").grid(row=3, column=0, sticky="w", pady=3)
        self.lang_var = tk.StringVar(value=self.settings["language"])
        self.lang_combo = ttk.Combobox(
            dict_frame, textvariable=self.lang_var, values=("es", "en", "auto"),
            width=8, state="readonly",
        )
        self.lang_combo.grid(row=3, column=1, sticky="w")
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language)

        ttk.Label(dict_frame, text="Micrófono:").grid(row=4, column=0, sticky="w", pady=3)
        device_row = ttk.Frame(dict_frame)
        device_row.grid(row=4, column=1, sticky="ew")
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(device_row, textvariable=self.device_var, width=30)
        self.device_combo.pack(side="left", fill="x", expand=True)
        self.device_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_device_selected())
        ttk.Button(device_row, text="⟳", width=3,
                   command=self._refresh_devices).pack(side="left", padx=(6, 0))

        # Sección: modelo de voz.
        model_frame = ttk.LabelFrame(main, text=" Modelo de voz ", padding=10)
        model_frame.pack(fill="x", pady=(0, 8))
        self.model_combo = ttk.Combobox(model_frame, state="readonly", width=44)
        self.model_combo.pack(fill="x")
        self.model_combo.bind("<<ComboboxSelected>>", lambda _e: self._model_selected())
        buttons = ttk.Frame(model_frame)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Descargar / activar",
                   command=self._download_selected).pack(side="left")
        ttk.Button(buttons, text="Eliminar", command=self._delete_selected).pack(side="left", padx=6)
        self.progress = ttk.Progressbar(model_frame, maximum=100)
        self.progress.pack(fill="x", pady=(6, 0))
        self.progress.pack_forget()
        self.total_label = tk.StringVar()
        ttk.Label(model_frame, textvariable=self.total_label,
                  style="Sub.TLabel").pack(anchor="w", pady=(6, 0))

        # Sección: prueba.
        test_frame = ttk.LabelFrame(main, text=" Pruébalo aquí ", padding=10)
        test_frame.pack(fill="x", pady=(0, 8))
        self.test_entry = ttk.Entry(test_frame)
        self.test_entry.pack(fill="x")
        self.test_entry.insert(0, "Haz clic aquí y usa el atajo…")
        self.test_entry.bind("<FocusIn>", self._clear_placeholder)
        ttk.Button(test_frame, text="Escribir texto de prueba",
                   command=self._test_write).pack(anchor="w", pady=(6, 0))

        # Transcripción.
        ttk.Label(main, text="Última transcripción:", style="Sub.TLabel").pack(anchor="w")
        self.transcript = tk.Text(main, height=4, state="disabled", wrap="word",
                                  relief="solid", borderwidth=1)
        self.transcript.pack(fill="both", expand=True, pady=(2, 6))

        ttk.Label(
            main,
            text=f"Desarrollado por Marcos Zingaretti  ·  v{__version__}",
            style="Footer.TLabel",
        ).pack(anchor="w")

        self._set_status("Listo. Usa el botón o el atajo para dictar.")
        self._refresh_models()
        self._refresh_devices()
        self._refresh_total()
        main.bind(
            "<Configure>",
            lambda event: self.status_label.configure(wraplength=max(200, event.width - 24)),
        )

    def _clear_placeholder(self, _event) -> None:
        if self.test_entry.get() == "Haz clic aquí y usa el atajo…":
            self.test_entry.delete(0, "end")

    # ---------------------------------------------------- modelos
    def _model_labels(self) -> list[str]:
        labels = []
        for entry in models.MODELS:
            if models.is_downloaded(entry):
                state = f"✓ ({models.human_size(models.file_size(entry))})"
            else:
                state = f"· {models.human_size(entry['bytes'])}"
            labels.append(f"{entry['label']}  {state}")
        return labels

    def _refresh_models(self) -> None:
        self.model_combo["values"] = self._model_labels()
        entry = models.entry_for_path(self.settings["model"])
        index = models.MODELS.index(entry) if entry else min(2, len(models.MODELS) - 1)
        self.model_combo.current(index)

    def _selected_entry(self) -> dict:
        index = self.model_combo.current()
        if index < 0:
            index = 0
        return models.MODELS[index]

    def _model_selected(self) -> None:
        entry = self._selected_entry()
        existing = models.find_existing(entry["file"])
        if existing:
            if self.settings.get("model") != existing:
                self.settings["model"] = existing
                cfg.save(self.settings)
                self._rebuild_transcriber()
            self._set_status(f"Modelo activo: {entry['key']}")
        else:
            self._set_status(
                f"«{entry['key']}» no está descargado. Pulsa «Descargar / activar»."
            )

    def _download_selected(self) -> None:
        entry = self._selected_entry()
        if models.is_downloaded(entry):
            self._model_selected()
            return
        if self._downloading:
            self._set_status("Ya hay una descarga en curso…")
            return
        self._downloading = True
        self._cancel_download.clear()
        self.progress.pack(fill="x", pady=(6, 0))
        self.progress["value"] = 0
        self._set_status(f"Descargando «{entry['key']}»…")
        threading.Thread(target=self._dl_worker, args=(entry, True), daemon=True).start()

    def _ensure_stream_model_async(self) -> None:
        """Descarga en segundo plano el modelo rápido del dictado continuo."""
        entry = models.BY_KEY.get(self.settings.get("streaming_model", "base"))
        if entry is None or models.is_downloaded(entry) or self._downloading:
            return
        self._downloading = True
        self._cancel_download.clear()
        self.progress.pack(fill="x", pady=(6, 0))
        self.progress["value"] = 0
        threading.Thread(target=self._dl_worker, args=(entry, False), daemon=True).start()

    def _dl_worker(self, entry: dict, activate: bool) -> None:
        def prog(done: int, total: int) -> None:
            value = 0 if not total else min(100, done * 100 / total)
            self._after(0, lambda: self.progress.configure(value=value))

        try:
            path = models.download(entry, progress=prog, cancel=self._cancel_download.is_set)
            self._after(0, self._dl_done, entry, path, None, activate)
        except models.DownloadCancelled:
            self._after(0, self._dl_done, entry, None, "cancelada", activate)
        except Exception as exc:  # noqa: BLE001
            self._after(0, self._dl_done, entry, None, str(exc), activate)

    def _dl_done(self, entry: dict, path, error, activate: bool = True) -> None:
        self._downloading = False
        self.progress.pack_forget()
        if error:
            self._set_status(f"⚠️ Descarga de «{entry['key']}»: {error}")
        elif activate:
            self.settings["model"] = path
            cfg.save(self.settings)
            self._rebuild_transcriber()
            self._set_status(f"✅ Modelo «{entry['key']}» descargado y activo.")
        else:
            self._set_status(f"✅ Modelo rápido «{entry['key']}» listo para escritura continua.")
        self._refresh_models()
        self._refresh_total()

    def _delete_selected(self) -> None:
        entry = self._selected_entry()
        if not models.is_downloaded(entry):
            self._set_status("Este modelo no está descargado.")
            return
        size = models.human_size(models.file_size(entry))
        if not messagebox.askyesno(
            "Eliminar modelo", f"¿Eliminar «{entry['key']}» ({size})?"
        ):
            return
        removed = models.delete(entry)
        if not removed:
            self._set_status("⚠️ No se pudo eliminar (¿permisos o ruta de sistema?).")
            return
        if models.entry_for_path(self.settings["model"]) == entry:
            self.settings["model"] = ""
            cfg.save(self.settings)
            self._rebuild_transcriber()
        if self.stream_transcriber is not None and \
                models.entry_for_path(self.stream_transcriber.model) == entry:
            try:
                self.stream_transcriber.close()
            except Exception:  # noqa: BLE001
                pass
            self.stream_transcriber = None
        self._refresh_models()
        self._refresh_total()
        self._set_status(f"✅ Modelo «{entry['key']}» eliminado.")

    def _refresh_total(self) -> None:
        total = models.total_downloaded_bytes()
        self.total_label.set(
            f"Ocupado por modelos descargados: {models.human_size(total)}"
            if total else "No hay modelos descargados."
        )

    # ---------------------------------------------------- motor / transcriber
    def _new_transcriber(self) -> Transcriber:
        return Transcriber(
            model=self.settings["model"],
            language=self.settings["language"],
            threads=int(self.settings["threads"]),
            port=int(self.settings["server_port"]) + 2,
            use_server=False,
        )

    def _rebuild_transcriber(self) -> None:
        try:
            self.transcriber.close()
        except Exception:  # noqa: BLE001
            pass
        self.transcriber = self._new_transcriber()

    def _stream_model_path(self):
        entry = models.BY_KEY.get(self.settings.get("streaming_model", "base"))
        if entry is None:
            return None
        return models.find_existing(entry["file"])

    def _ensure_stream_transcriber(self):
        path = self._stream_model_path()
        if not path:
            return None
        if self.stream_transcriber is None:
            self.stream_transcriber = Transcriber(
                model=path,
                language=self.settings["language"],
                threads=int(self.settings["threads"]),
                port=int(self.settings["server_port"]) + 1,
                use_server=False,
            )
        return self.stream_transcriber

    # ---------------------------------------------------- audio
    def _refresh_devices(self) -> None:
        try:
            import sounddevice as sd

            self._devices = [
                (d["name"], d["index"])
                for d in sd.query_devices()
                if d.get("max_input_channels", 0) > 0
            ]
        except Exception as exc:  # noqa: BLE001 - PortAudio ausente o sin dispositivos
            self._devices = []
            self._set_status(f"⚠️ Micrófono no disponible: {exc}")
        names = [name for name, _ in self._devices]
        self.device_combo["values"] = names
        if not names:
            self.device_var.set("")
            self._selected_device_index = None
            return
        current = self.device_var.get()
        if current not in names:
            current = names[0]
        self.device_var.set(current)
        self._selected_device_index = self._devices[names.index(current)][1]

    def _on_device_selected(self) -> None:
        index = self.device_combo.current()
        if 0 <= index < len(self._devices):
            self._selected_device_index = self._devices[index][1]
            self._set_status(f"Micrófono: {self._devices[index][0]}")

    def _open_audio(self, on_chunk):
        import sounddevice as sd

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            on_chunk(indata.tobytes())

        stream = sd.InputStream(
            samplerate=16000, channels=1, dtype="int16", blocksize=1024,
            device=self._selected_device_index, callback=callback,
        )
        stream.start()
        return stream

    # ---------------------------------------------------- atajo
    def _start_hotkey(self) -> None:
        self.hotkey = WinHotkey(
            self.hotkey_combo, self._on_hotkey_press, self._on_hotkey_error
        )

    def _on_hotkey_error(self, combo: str) -> None:
        self._after(
            0, self._set_status,
            f"⚠️ No se pudo registrar el atajo «{combo}» (¿en uso por otra app?).",
        )

    def _on_hotkey_press(self) -> None:
        self._after(0, self._hotkey_ui)

    def _hotkey_ui(self) -> None:
        if self.state == self.STATE_IDLE:
            self._start()
        elif self.state == self.STATE_REC and self.settings["mode"] == "toggle":
            self._stop()

    def _apply_hotkey(self) -> None:
        combo = self.hotkey_var.get().strip()
        if not combo:
            return
        self.hotkey_var.set(format_combo(combo))
        new = self.hotkey_var.get()
        self.settings["hotkey"] = new
        self.hotkey_combo = new
        cfg.save(self.settings)
        if self.hotkey:
            self.hotkey.set_combo(new)
        self._set_status(f"Atajo cambiado a {new}.")

    # ---------------------------------------------------- ajustes
    def _save_mode(self) -> None:
        self.settings["mode"] = self.mode_var.get()
        cfg.save(self.settings)

    def _save_streaming(self) -> None:
        enabled = bool(self.stream_var.get())
        self.settings["streaming"] = enabled
        cfg.save(self.settings)
        if enabled:
            self._ensure_stream_model_async()
            self._set_status("Escritura continua activada.")
        else:
            if self.stream_transcriber is not None:
                try:
                    self.stream_transcriber.close()
                except Exception:  # noqa: BLE001
                    pass
                self.stream_transcriber = None
            self._set_status("Escritura continua desactivada.")

    def _on_language(self, _event=None) -> None:
        language = self.lang_var.get().strip() or "auto"
        self.settings["language"] = language
        self.transcriber.language = language
        if self.stream_transcriber is not None:
            self.stream_transcriber.language = language
        cfg.save(self.settings)
        self._set_status(f"Idioma: {language}")

    # ---------------------------------------------------- flujo dictado
    def _toggle(self) -> None:
        if self.state == self.STATE_IDLE:
            self._start()
        elif self.state == self.STATE_REC:
            self._stop()

    def _start(self) -> None:
        if self.state != self.STATE_IDLE:
            return
        if not self.transcriber.available:
            self._set_status("⚠️ No hay ningún modelo. Descarga uno en «Modelo de voz».")
            return
        self.state = self.STATE_REC
        self._rec_buffer = bytearray()
        self._stream_text = ""
        self._last_text = ""
        self._clear_transcript()
        try:
            if self.settings["streaming"]:
                self._splitter = UtteranceSplitter(
                    min_silence_ms=int(self.settings.get("vad_min_silence_ms", 650)),
                    max_utterance_ms=int(self.settings.get("vad_max_utterance_ms", 10000)),
                )
                self._queue = queue.Queue()

                def on_chunk(data):
                    for chunk in self._splitter.feed(data):
                        self._queue.put(chunk)

                self._audio = self._open_audio(on_chunk)
                self._worker = threading.Thread(target=self._stream_worker, daemon=True)
                self._worker.start()
            else:
                self._audio = self._open_audio(self._rec_buffer.extend)
        except Exception as exc:  # noqa: BLE001
            self.state = self.STATE_IDLE
            self._set_status(f"⚠️ No se pudo acceder al micrófono: {exc}")
            return
        self._refresh_button()
        self._set_status("🔴 Grabando… habla ahora.")
        if self.settings["mode"] == "hold":
            self._hold_watch()

    def _hold_watch(self) -> None:
        """Modo mantener: para al soltar el atajo o cualquiera de sus teclas."""
        _, vk = parse_combo(self.hotkey_combo)
        keys = [vk]
        for piece in self.hotkey_combo.split("+"):
            mod = _MOD_KEYS.get(piece.strip().lower())
            if mod:
                keys.append(mod)
        get_async = ctypes.windll.user32.GetAsyncKeyState

        def watch():
            time.sleep(0.12)
            while self.state == self.STATE_REC:
                try:
                    if not all(get_async(key) & 0x8000 for key in keys):
                        self._after(0, self._stop)
                        break
                except Exception:  # noqa: BLE001
                    break
                time.sleep(0.04)

        threading.Thread(target=watch, daemon=True).start()

    def _stop(self) -> None:
        if self.state != self.STATE_REC:
            return
        self.state = self.STATE_WORK
        self._refresh_button()
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

    def _stream_worker(self) -> None:
        transcriber = self._ensure_stream_transcriber() or self.transcriber
        while True:
            chunk = self._queue.get()
            if chunk is None:
                break
            try:
                text = self._transcribe(chunk, transcriber)
                if text:
                    self.keyboard.type_text(
                        text, append_space=bool(self.settings["append_space"])
                    )
                    self._after(0, self._stream_typed, text)
            except Exception as exc:  # noqa: BLE001
                self._after(0, self._set_status, f"⚠️ Error al transcribir: {exc}")
        self._after(0, self._finish)

    def _once_worker(self, data: bytes) -> None:
        text = ""
        try:
            if len(data) >= 16000 * 2 * 0.4:  # >= 0,4 s
                text = self._transcribe(data, self.transcriber)
                if text:
                    self.keyboard.type_text(
                        text, append_space=bool(self.settings["append_space"])
                    )
            else:
                self._after(0, self._set_status, "Grabación demasiado corta.")
        except Exception as exc:  # noqa: BLE001
            self._after(0, self._set_status, f"⚠️ Error: {exc}")
        self._after(0, self._finish, text)

    def _transcribe(self, data: bytes, transcriber: Transcriber) -> str:
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="dictavoz-")
        os.close(fd)
        try:
            with wave.open(path, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(data)
            return transcriber.transcribe(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _stream_typed(self, text: str) -> None:
        self._stream_text = (self._stream_text + " " + text).strip()
        self._append_transcript(text)
        short = text[:90] + ("…" if len(text) > 90 else "")
        self._set_status(f"✅ Escrito: {short}")

    def _finish(self, text=None) -> None:
        self.state = self.STATE_IDLE
        self._refresh_button()
        if text:
            self._append_transcript(text)
        if self._stream_text or text:
            self._set_status("✅ Dictado terminado.")
        else:
            self._set_status("No se detectó habla.")

    def _append_transcript(self, text: str) -> None:
        self.transcript.configure(state="normal")
        if self._last_text:
            self.transcript.insert("end", " " + text)
        else:
            self.transcript.insert("end", text)
        self._last_text = (self._last_text + " " + text).strip()
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _clear_transcript(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")

    def _refresh_button(self) -> None:
        self.toggle_btn.configure(
            text="Detener y escribir" if self.state == self.STATE_REC else "Activar dictado"
        )

    def _test_write(self) -> None:
        self.keyboard.type_text(TEST_TEXT, append_space=True)
        self._set_status("Texto de prueba escrito.")

    # ---------------------------------------------------- utilidades
    def _after(self, delay: int, func, *args) -> None:
        """Programa en el hilo de Tk ignorando si la ventana ya se cerró."""
        try:
            self.root.after(delay, lambda: func(*args))
        except Exception:  # noqa: BLE001 - ventana cerrada
            pass

    def _set_status(self, text: str) -> None:
        self.status.set(text)

    def close(self) -> None:
        if self.state == self.STATE_REC:
            self._stop()
        if self.hotkey:
            self.hotkey.stop()
        if self.stream_transcriber is not None:
            try:
                self.stream_transcriber.close()
            except Exception:  # noqa: BLE001
                pass
            self.stream_transcriber = None
        try:
            self.transcriber.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    enable_dpi_awareness()
    root = tk.Tk()
    app = DictaVozWin(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.close(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
