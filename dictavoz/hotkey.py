"""Atajo global leyendo los teclados desde /dev/input (grupo `input`).

Soporta combinaciones de teclas: "Ctrl+D" (por defecto), "F9", "Ctrl+F9",
"Ctrl+Alt+V", etc. El formato guardado usa el nombre canónico: "Ctrl+Alt+F9".
"""

from __future__ import annotations

import fcntl
import glob
import os
import select
import struct
import threading
import time

EV_SYN, EV_KEY = 0x00, 0x01
INPUT_EVENT = struct.Struct("llHHi")


def _IOC(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def EVIOCGBIT(event_type, length):
    return _IOC(2, "E", 0x20 + event_type, length)


def EVIOCGNAME(length):
    return _IOC(2, "E", 0x06, length)


# Nombres legibles para las teclas más comunes.
KEY_NAMES = {
    1: "Esc", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7",
    9: "8", 10: "9", 11: "0", 12: "-", 13: "=", 14: "Retroceso", 15: "Tab",
    16: "Q", 17: "W", 18: "E", 19: "R", 20: "T", 21: "Y", 22: "U", 23: "I",
    24: "O", 25: "P", 26: "[", 27: "]", 28: "Enter", 29: "Ctrl izq",
    30: "A", 31: "S", 32: "D", 33: "F", 34: "G", 35: "H", 36: "J", 37: "K",
    38: "L", 39: ";", 40: "'", 41: "`", 42: "Shift izq", 43: "\\",
    44: "Z", 45: "X", 46: "C", 47: "V", 48: "B", 49: "N", 50: "M",
    51: ",", 52: ".", 53: "/", 54: "Shift der", 55: "KP *", 56: "Alt izq",
    57: "Espacio", 58: "Bloq Mayús", 59: "F1", 60: "F2", 61: "F3", 62: "F4",
    63: "F5", 64: "F6", 65: "F7", 66: "F8", 67: "F9", 68: "F10",
    69: "Bloq Num", 70: "Bloq Despl", 87: "F11", 88: "F12",
    96: "KP Enter", 97: "Ctrl der", 98: "KP /", 99: "Impr Pant",
    100: "Alt der", 102: "Inicio", 103: "Arriba", 104: "Av Pag",
    105: "Izquierda", 106: "Derecha", 107: "Fin", 108: "Abajo",
    109: "Re Pag", 110: "Insert", 111: "Supr", 119: "Pausa",
    125: "Meta izq", 126: "Meta der", 127: "Menú",
}
NAME_TO_CODE = {name.lower(): code for code, name in KEY_NAMES.items()}
NAME_TO_CODE.update({"space": 57, "enter": 28, "escape": 1, "scrolllock": 70})

KEY_LEFTCTRL, KEY_RIGHTCTRL = 29, 97
KEY_LEFTSHIFT, KEY_RIGHTSHIFT = 42, 54
KEY_LEFTALT, KEY_RIGHTALT = 56, 100
KEY_LEFTMETA, KEY_RIGHTMETA = 125, 126

# Modificadores: nombre canónico -> códigos evdev (izquierdo y derecho).
MODIFIER_CODES = {
    "ctrl": {KEY_LEFTCTRL, KEY_RIGHTCTRL},
    "alt": {KEY_LEFTALT, KEY_RIGHTALT},
    "shift": {KEY_LEFTSHIFT, KEY_RIGHTSHIFT},
    "meta": {KEY_LEFTMETA, KEY_RIGHTMETA},
}
MODIFIER_BY_CODE = {
    code: name for name, codes in MODIFIER_CODES.items() for code in codes
}
MODIFIER_ORDER = ("ctrl", "alt", "shift", "meta")


def parse_hotkey(text) -> tuple[frozenset[str], int]:
    """'Ctrl+Alt+F9' -> (frozenset({'ctrl','alt'}), 67). Válido también 'F9'."""
    mods: set[str] = set()
    key = ""
    for piece in str(text).split("+"):
        token = piece.strip().lower()
        if token in MODIFIER_CODES:
            mods.add(token)
        else:
            key = token
    code = NAME_TO_CODE.get(key, 67)  # F9 por defecto
    # Un atajo no puede ser únicamente un modificador (p. ej. "ctrl izq").
    if code in MODIFIER_BY_CODE:
        return frozenset(), 67
    return frozenset(mods), code


def format_hotkey(mods, code: int) -> str:
    display = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "meta": "Meta"}
    parts = [display[name] for name in MODIFIER_ORDER if name in mods]
    parts.append(key_name(code))
    return "+".join(parts)


def key_name(code: int) -> str:
    return KEY_NAMES.get(code, f"Tecla {code}")


class HotkeyListener(threading.Thread):
    """Detecta combinaciones de teclas y permite reasignarlas."""

    # on_press(mods_nombres, code), on_release(code), on_capture(mods, code)
    def __init__(self, get_hotkey, on_press, on_release, is_capturing, on_capture):
        super().__init__(daemon=True, name="hotkey-listener")
        self._get_hotkey = get_hotkey
        self._on_press = on_press
        self._on_release = on_release
        self._is_capturing = is_capturing
        self._on_capture = on_capture
        self._stop = threading.Event()
        self._fds: dict[int, str] = {}
        self._paths: dict[str, int] = {}
        self._down: set[int] = set()
        self._mods_down: set[int] = set()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _is_keyboard(fd: int) -> bool:
        buf = fcntl.ioctl(fd, EVIOCGBIT(EV_KEY, 96), bytes(96))
        has = lambda code: buf[code // 8] & (1 << (code % 8))  # noqa: E731
        return bool(has(1) or has(28) or has(30) or has(57))  # Esc/Enter/A/Espacio

    @staticmethod
    def _device_name(fd: int) -> str:
        try:
            raw = fcntl.ioctl(fd, EVIOCGNAME(256), bytes(256))
        except OSError:
            return ""
        return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")

    def _rescan(self) -> None:
        current = set(glob.glob("/dev/input/event*"))
        for fd, path in list(self._fds.items()):
            if path not in current:
                try:
                    os.close(fd)
                except OSError:
                    pass
                del self._fds[fd]
                self._paths.pop(path, None)
        for path in sorted(current):
            if path in self._paths:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            try:
                # Ignora nuestro propio teclado virtual para no reaccionar
                # a las pulsaciones que inyectamos.
                if "dictavoz" in self._device_name(fd).lower():
                    os.close(fd)
                elif self._is_keyboard(fd):
                    self._fds[fd] = path
                    self._paths[path] = fd
                else:
                    os.close(fd)
            except OSError:
                os.close(fd)

    def _active_mods(self) -> frozenset[str]:
        return frozenset(
            MODIFIER_BY_CODE[code] for code in self._mods_down
            if code in MODIFIER_BY_CODE
        )

    def _handle(self, code: int, value: int) -> None:
        # Mantiene el estado de los modificadores.
        if code in MODIFIER_BY_CODE:
            if value == 1:
                self._mods_down.add(code)
            elif value == 0:
                self._mods_down.discard(code)
            return

        if self._is_capturing():
            if value == 1:
                self._on_capture(self._active_mods(), code)
            return

        required_mods, main_code = self._get_hotkey()
        if code != main_code:
            return
        active = self._active_mods()
        if value == 1:
            if code in self._down:
                return
            self._down.add(code)
            if required_mods.issubset(active):
                self._on_press(active, code)
        elif value == 0:
            was_down = code in self._down
            self._down.discard(code)
            if was_down:
                self._on_release(code)

    def run(self) -> None:
        last_scan = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_scan > 3.0:
                self._rescan()
                last_scan = now
            fds = list(self._fds.keys())
            if not fds:
                self._stop.wait(0.5)
                continue
            try:
                ready, _, _ = select.select(fds, [], [], 1.0)
            except OSError:
                self._rescan()
                continue
            for fd in ready:
                try:
                    data = os.read(fd, INPUT_EVENT.size * 64)
                except OSError:
                    path = self._fds.pop(fd, None)
                    if path:
                        self._paths.pop(path, None)
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    continue
                for offset in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                    _, _, event_type, code, value = INPUT_EVENT.unpack_from(data, offset)
                    if event_type == EV_KEY:
                        self._handle(code, value)