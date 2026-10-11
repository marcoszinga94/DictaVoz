#!/usr/bin/env python3
"""Genera el mapa caracter -> teclas para DictaVoz.

Usa `xkbcli how-to-type` para saber que tecla(s) y modificadores producen
cada caracter en un layout concreto (por defecto latam). Para los caracteres
que no tienen tecla directa (p. ej. las vocales acentuadas) genera la
secuencia de tecla muerta + letra usando la normalizacion NFC de Unicode.

Salida: dictavoz/keymaps/<layout>.json con la forma:

    {
      "layout": "latam",
      "variant": "basic",
      "chars": {
        "a": [[30, 0, 0]],
        "A": [[30, 1, 0]],
        "á": [[26, 0, 0], [30, 0, 0]]
      }
    }

Cada paso es [codigo_evdev, shift, altgr].

Uso:
    python3 tools/gen_keymap.py --layout latam --variant basic
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unicodedata

# Los keycodes de XKB son los de evdev + 8.
XKB_TO_EVDEV = 8

KEY_LEFTSHIFT = 42
KEY_RIGHTALT = 100

# Teclas muertas -> marca combinante Unicode (para NFC).
DEAD_COMBINING = {
    "dead_grave": "\u0300",
    "dead_acute": "\u0301",
    "dead_circumflex": "\u0302",
    "dead_tilde": "\u0303",
    "dead_macron": "\u0304",
    "dead_breve": "\u0306",
    "dead_abovedot": "\u0307",
    "dead_diaeresis": "\u0308",
    "dead_abovering": "\u030a",
    "dead_doubleacute": "\u030b",
    "dead_caron": "\u030c",
    "dead_cedilla": "\u0327",
    "dead_ogonek": "\u0328",
    "dead_belowdot": "\u0323",
    "dead_circumflex": "\u0302",
}

# Caracteres extra utiles (comillas tipograficas, guiones largos, etc.).
EXTRA_CODEPOINTS = [
    0x00A0, 0x00AB, 0x00BB, 0x00BF, 0x00A1, 0x00A7, 0x00BA, 0x00AA,
    0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026, 0x20AC,
    0x00B0, 0x00B7, 0x00BD, 0x00BC, 0x0152, 0x0153, 0x0178,
]


class KeymapError(RuntimeError):
    pass


def how_to_type(layout: str, variant: str, arg: str, keysym: bool = False) -> str:
    cmd = ["xkbcli", "how-to-type", "--layout", layout]
    if variant:
        cmd += ["--variant", variant]
    if keysym:
        cmd += ["--keysym"]
    cmd += ["--", arg]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout


def parse_direct(output: str) -> list[tuple[int, int, int]]:
    """Extrae las opciones de acceso directo: (evdev, shift, altgr)."""
    options: list[tuple[int, int, int]] = []
    in_direct = False
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("=== Direct access"):
            in_direct = True
            continue
        if line.startswith("=== Access via"):
            in_direct = False
            continue
        if not in_direct or not line:
            continue
        if not line[0].isdigit():
            continue
        columns = line.split()
        if len(columns) < 2:
            continue
        # columnas: CODE NAME LAYOUT LAYOUT_NAME... LEVEL# [ MODS ]
        try:
            code = int(columns[0])
        except ValueError:
            continue
        bracket = line[line.rfind("[") + 1 : line.rfind("]")]
        mods = bracket.split()
        # Las opciones que necesitan Bloq Mayús se descartan: no queremos
        # activar/desactivar el bloqueo de mayúsculas al escribir.
        if "Lock" in mods:
            continue
        shift = 1 if "Shift" in mods else 0
        altgr = 1 if ("Mod5" in mods or "LevelThree" in mods) else 0
        options.append((code - XKB_TO_EVDEV, shift, altgr))
    # Quita duplicados manteniendo el orden.
    return list(dict.fromkeys(options))


def best_option(options: list[tuple[int, int, int]]):
    if not options:
        return None
    # Prefiere menos modificadores.
    return min(options, key=lambda t: (t[1] + t[2], t[2], t[0]))


def dead_key_option(layout: str, variant: str, name: str):
    output = how_to_type(layout, variant, name, keysym=True)
    return best_option(parse_direct(output))


def charset():
    codepoints = list(range(0x20, 0x100)) + list(range(0x100, 0x180))
    codepoints += EXTRA_CODEPOINTS
    seen = set()
    for cp in codepoints:
        if cp in seen:
            continue
        seen.add(cp)
        ch = chr(cp)
        if unicodedata.category(ch).startswith("C"):
            continue
        yield ch


def generate(layout: str, variant: str) -> dict:
    if subprocess.run(["which", "xkbcli"], capture_output=True).returncode != 0:
        raise KeymapError("No se encuentra 'xkbcli' (paquete xkbcommon).")

    chars: dict[str, list[list[int]]] = {}
    for ch in charset():
        arg = f"U+{ord(ch):04X}"
        option = best_option(parse_direct(how_to_type(layout, variant, arg)))
        if option is not None:
            chars[ch] = [[option[0], option[1], option[2]]]

    # Secuencias con tecla muerta (acentos y dieresis).
    bases = {ch: steps[0] for ch, steps in chars.items() if len(ch) == 1}
    for dead_name, combining in DEAD_COMBINING.items():
        dead = dead_key_option(layout, variant, dead_name)
        if dead is None:
            continue
        dead_step = [dead[0], dead[1], dead[2]]
        for base, base_step in bases.items():
            composed = unicodedata.normalize("NFC", base + combining)
            if len(composed) != 1 or composed in chars:
                continue
            if ord(composed) < 0x80:
                continue
            chars[composed] = [list(dead_step), list(base_step)]

    return {
        "layout": layout,
        "variant": variant,
        "chars": dict(sorted(chars.items(), key=lambda kv: ord(kv[0]))),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera el mapa de teclado para DictaVoz")
    parser.add_argument("--layout", default="latam")
    parser.add_argument("--variant", default="basic")
    parser.add_argument("--output", default=None, help="ruta de salida (JSON)")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    default_out = os.path.join(here, "..", "dictavoz", "keymaps", f"{args.layout}.json")
    output = os.path.abspath(args.output or default_out)
    os.makedirs(os.path.dirname(output), exist_ok=True)

    data = generate(args.layout, args.variant)
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
        handle.write("\n")

    print(f"Mapa generado: {output}")
    print(f"Caracteres soportados: {len(data['chars'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
