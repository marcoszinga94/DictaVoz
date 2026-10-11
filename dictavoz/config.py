"""Configuración persistente de DictaVoz (~/.config/dictavoz/config.json)."""

from __future__ import annotations

import json
import os

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "dictavoz"
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Valores por defecto. Cualquier clave ausente se rellena con esto.
DEFAULTS = {
    "hotkey": "Ctrl+Alt+Y",   # tecla/combinación del atajo global
    "mode": "hold",          # "hold" (mantener) o "toggle" (pulsar para iniciar/parar)
    "language": "es",        # idioma para whisper ("es", "en", "auto", ...)
    "model": "",             # ruta al modelo ggml; vacío = autodetectar
    "source": "",            # fuente de audio PipeWire; vacío = la del sistema
    "threads": 0,            # hilos para whisper; 0 = automático
    "layout": "latam",       # layout de teclado para el mapa de teclas
    "append_space": True,    # añadir un espacio tras cada dictado
    "server_port": 8178,     # puerto local de whisper-server
    "min_seconds": 0.4,      # duración mínima de grabación para transcribir
    "streaming": True,       # escribir el texto a medida que se habla
    "streaming_model": "base",   # modelo pequeño y rápido para el dictado continuo
    "vad_min_silence_ms": 650,   # silencio que cierra una frase (dictado continuo)
    "vad_max_utterance_ms": 10000,  # tope por frase (se escribe aunque no haya pausa)
}

MODES = ("hold", "toggle")


def load() -> dict:
    config = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return config
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in config:
                config[key] = value
    return config


def save(config: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    data = {key: config.get(key, value) for key, value in DEFAULTS.items()}
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, CONFIG_PATH)
