"""Punto de entrada de DictaVoz (python3 -m dictavoz)."""

from __future__ import annotations

import argparse
import sys

from . import __version__, config as cfg
from . import models
from .audio import list_sources
from .inject import InjectError, VirtualKeyboard, available_layouts, load_keymap
from .hotkey import parse_hotkey
from .stt import find_model


def _download_model(value: str) -> str:
    """Resuelve una clave/fichero de modelo, descargándolo si hace falta."""
    entry = models.BY_KEY.get(value) or models.entry_for_path(value)
    if entry is None:
        raise SystemExit(f"Modelo desconocido: {value!r}")
    existing = models.find_existing(entry["file"])
    if existing:
        return existing
    print(f"Descargando {entry['file']} ({models.human_size(entry['bytes'])})…")

    def progress(done: int, total: int) -> None:
        if total:
            print(f"\r  {done * 100 / total:5.1f}%  "
                  f"{models.human_size(done)} / {models.human_size(total)}",
                  end="", flush=True)

    path = models.download(entry, progress=progress)
    print(f"\r  completado: {path}")
    return path


def _self_test(args) -> int:
    print("DictaVoz - autocomprobación")
    status = 0

    # 1) Teclado virtual.
    try:
        keymap = load_keymap(args.layout)
        keyboard = VirtualKeyboard(keymap)
        keyboard.close()
        print(f"  [ok] Teclado virtual creado (layout '{args.layout}', "
              f"{len(keymap['chars'])} caracteres).")
    except (InjectError, OSError) as error:
        print(f"  [FALLO] Teclado virtual: {error}")
        status = 1

    # 2) Modelo / STT.
    model = find_model(args.model)
    if model:
        print(f"  [ok] Modelo encontrado: {model}")
    else:
        print("  [FALLO] No se encontró ningún modelo ggml de whisper.cpp.")
        status = 1

    # 3) Micrófono.
    sources = list_sources()
    if sources:
        print(f"  [ok] Fuentes de audio: {len(sources)}")
    else:
        print("  [aviso] No se pudieron listar fuentes de audio (¿pactl?).")

    return status


def _type_text(args) -> int:
    keymap = load_keymap(args.layout)
    keyboard = VirtualKeyboard(keymap)
    try:
        unknown = keyboard.type_text(args.type, append_space=args.append_space)
        if unknown:
            print(f"Caracteres sin soporte: {''.join(sorted(unknown))}")
        print("Texto escrito.")
    finally:
        keyboard.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dictavoz",
        description="Dictado por voz que escribe donde tengas el cursor (Linux/Wayland).",
    )
    parser.add_argument("--version", action="version", version=f"DictaVoz {__version__}")
    parser.add_argument("--hotkey", help="tecla o combinación del atajo (p. ej. Ctrl+Alt+Y, F9)")
    parser.add_argument("--mode", choices=["hold", "toggle"],
                        help="hold = mantener la tecla; toggle = pulsar para iniciar/parar")
    parser.add_argument("--language", help="idioma para whisper (es, en, auto, ...)")
    parser.add_argument("--model", help="ruta a un modelo ggml-*.bin")
    parser.add_argument("--source", help="fuente de audio de PipeWire (nombre de pactl)")
    parser.add_argument("--layout", help="layout de teclado (por defecto latam)")
    parser.add_argument("--threads", type=int, help="hilos para whisper (0 = automático)")
    parser.add_argument("--list-sources", action="store_true",
                        help="lista los micrófonos disponibles y sale")
    parser.add_argument("--list-layouts", action="store_true",
                        help="lista los mapas de teclado disponibles y sale")
    parser.add_argument("--type", metavar="TEXTO",
                        help="escribe TEXTO con el teclado virtual y sale (prueba)")
    parser.add_argument("--append-space", action="store_true",
                        help="añade un espacio tras el texto de --type")
    parser.add_argument("--self-test", action="store_true",
                        help="comprueba teclado virtual, modelo y micrófono")
    parser.add_argument("--smoke-test", action="store_true",
                        help="abre la ventana y la cierra en 1,5 s")
    parser.add_argument("--list-models", action="store_true",
                        help="lista los modelos disponibles y sale")
    parser.add_argument("--download", metavar="MODELO",
                        help="descarga un modelo del catálogo y sale (p. ej. small)")
    args = parser.parse_args(argv)

    settings = cfg.load()
    for key in ("hotkey", "mode", "language", "model", "source", "layout", "threads"):
        value = getattr(args, key)
        if value is not None:
            settings[key] = value

    if args.list_models:
        for entry in models.MODELS:
            if models.is_downloaded(entry):
                state = models.human_size(models.file_size(entry))
            else:
                state = models.human_size(entry["bytes"])
            print(f"{entry['key']}\t{entry['label']}\t{state}")
        print(f"\nTotal ocupado: {models.human_size(models.total_downloaded_bytes())}")
        return 0

    if args.download:
        _download_model(args.download)
        return 0

    # --model acepta tanto una ruta como una clave del catálogo (p. ej. "small").
    if args.model:
        settings["model"] = _download_model(args.model)

    # Para las rutas de auto-comprobación usamos los valores ya resueltos.
    args.layout = settings["layout"]
    args.model = settings["model"]

    if args.list_sources:
        for name, description in list_sources():
            print(f"{name}\t{description}")
        return 0

    if args.list_layouts:
        for layout in available_layouts():
            print(layout)
        return 0

    if args.self_test:
        return _self_test(args)

    if args.type is not None:
        return _type_text(args)

    # Comprobaciones de arranque antes de abrir la ventana.
    try:
        parse_hotkey(settings["hotkey"])
    except Exception:  # noqa: BLE001
        print(f"Aviso: atajo '{settings['hotkey']}' inválido; usando Ctrl+Alt+Y.",
              file=sys.stderr)
        settings["hotkey"] = "Ctrl+Alt+Y"

    from .app import DictaVozApp  # importa GTK sólo cuando hace falta

    app = DictaVozApp(settings, smoke=args.smoke_test)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
