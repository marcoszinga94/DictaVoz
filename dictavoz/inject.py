"""Escritura de texto en el cursor mediante un teclado virtual (/dev/uinput).

Crea un dispositivo de teclado virtual y emite pulsaciones reales, que el
compositor (KWin/Wayland o X11) recibe como si vinieran de un teclado físico.
El mapa caracter -> teclas se carga desde keymaps/<layout>.json.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import struct
import time

# Constantes de linux/input-event-codes.h y linux/uinput.h.
EV_SYN, EV_KEY = 0x00, 0x01
SYN_REPORT = 0x00
KEY_ESC = 1
KEY_BACKSPACE = 14
KEY_TAB = 15
KEY_ENTER = 28
KEY_LEFTSHIFT = 42
KEY_SPACE = 57
KEY_RIGHTALT = 100
BUS_USB = 0x03

KEYMAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keymaps")


def _IOC(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


UI_DEV_CREATE = _IOC(0, "U", 1, 0)
UI_DEV_DESTROY = _IOC(0, "U", 2, 0)
UI_DEV_SETUP = _IOC(1, "U", 3, 92)
UI_SET_EVBIT = _IOC(1, "U", 100, 4)
UI_SET_KEYBIT = _IOC(1, "U", 101, 4)

# input_event: struct timeval (2 longs) + type + code + value.
# uinput_setup: input_id (4 x u16) + name[80] + ff_effects_max (u32).
INPUT_EVENT = struct.Struct("llHHi")
UINPUT_SETUP = struct.Struct("HHHH80sI")


class InjectError(RuntimeError):
    pass


def available_layouts() -> list[str]:
    if not os.path.isdir(KEYMAP_DIR):
        return []
    return sorted(
        name[:-5] for name in os.listdir(KEYMAP_DIR) if name.endswith(".json")
    )


def load_keymap(layout: str) -> dict:
    path = os.path.join(KEYMAP_DIR, f"{layout}.json")
    if not os.path.isfile(path):
        raise InjectError(
            f"No existe el mapa de teclado '{layout}'. "
            f"Genéralo con: python3 tools/gen_keymap.py --layout {layout}"
        )
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    # Normaliza a {char: [(code, shift, altgr), ...]}.
    chars = {
        ch: [tuple(int(v) for v in step) for step in steps]
        for ch, steps in data.get("chars", {}).items()
    }
    return {"layout": data.get("layout", layout), "chars": chars}


class VirtualKeyboard:
    """Teclado virtual que escribe en la ventana/posición con el foco."""

    def __init__(self, keymap: dict, name: str = "DictaVoz Virtual Keyboard"):
        self.chars: dict[str, list[tuple[int, int, int]]] = keymap["chars"]
        self.fd = None
        try:
            self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:
            raise InjectError(
                "No se puede abrir /dev/uinput. Comprueba que tu usuario tiene "
                "permiso de escritura (ACL, grupo o regla udev). "
                f"Detalle: {error}"
            ) from error
        try:
            self._setup(name)
        except Exception:
            os.close(self.fd)
            self.fd = None
            raise

    def _setup(self, name: str) -> None:
        ioctl = lambda req, arg=0: fcntl.ioctl(self.fd, req, arg)  # noqa: E731
        ioctl(UI_SET_EVBIT, EV_KEY)
        ioctl(UI_SET_EVBIT, EV_SYN)
        codes = {KEY_LEFTSHIFT, KEY_RIGHTALT, KEY_SPACE, KEY_ENTER, KEY_TAB, KEY_BACKSPACE}
        for steps in self.chars.values():
            for code, _, _ in steps:
                codes.add(code)
        for code in sorted(codes):
            ioctl(UI_SET_KEYBIT, code)
        setup = UINPUT_SETUP.pack(BUS_USB, 0x1234, 0x5678, 1, name.encode()[:79], 0)
        ioctl(UI_DEV_SETUP, setup)
        ioctl(UI_DEV_CREATE)
        time.sleep(0.25)  # deja que el kernel cree el nodo /dev/input/event*

    def _emit(self, event_type: int, code: int, value: int) -> None:
        data = INPUT_EVENT.pack(0, 0, event_type, code, value)
        # El dispositivo se abre sin bloqueo: si el buffer del kernel está lleno
        # (EAGAIN) reintentamos en vez de abortar. Así un texto muy largo no se
        # corta a mitad.
        while data:
            try:
                written = os.write(self.fd, data)
            except BlockingIOError:
                time.sleep(0.002)
                continue
            except OSError as error:
                if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    time.sleep(0.002)
                    continue
                raise
            data = data[written:]

    def _tap(self, code: int, delay: float = 0.006) -> None:
        self._emit(EV_KEY, code, 1)
        self._emit(EV_SYN, SYN_REPORT, 0)
        time.sleep(delay)
        self._emit(EV_KEY, code, 0)
        self._emit(EV_SYN, SYN_REPORT, 0)
        time.sleep(delay)

    def _press(self, code: int) -> None:
        self._emit(EV_KEY, code, 1)
        self._emit(EV_SYN, SYN_REPORT, 0)

    def _release(self, code: int) -> None:
        self._emit(EV_KEY, code, 0)
        self._emit(EV_SYN, SYN_REPORT, 0)

    def type_text(self, text: str, append_space: bool = False) -> set[str]:
        """Escribe `text`. Devuelve el conjunto de caracteres no soportados."""
        unknown: set[str] = set()
        if self.fd is None:
            raise InjectError("El teclado virtual no está disponible.")
        for ch in text:
            if ch == "\n":
                self._tap(KEY_ENTER)
                continue
            if ch == "\t":
                self._tap(KEY_TAB)
                continue
            steps = self.chars.get(ch)
            if not steps:
                if not ch.isspace():
                    unknown.add(ch)
                continue
            for index, (code, shift, altgr) in enumerate(steps):
                if shift:
                    self._press(KEY_LEFTSHIFT)
                if altgr:
                    self._press(KEY_RIGHTALT)
                # Pausa algo mayor entre la tecla muerta y la siguiente letra.
                self._tap(code, delay=0.012 if index + 1 < len(steps) else 0.006)
                if altgr:
                    self._release(KEY_RIGHTALT)
                if shift:
                    self._release(KEY_LEFTSHIFT)
        if append_space:
            self._tap(KEY_SPACE)
        return unknown

    def close(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None
