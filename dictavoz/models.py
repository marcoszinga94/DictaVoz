"""Catálogo y descarga de modelos de whisper.cpp.

Los modelos se guardan en ~/.local/share/dictavoz/models/ y se descargan
automáticamente desde el repositorio oficial ggml en Hugging Face.
"""

from __future__ import annotations

import os
import urllib.request

MODEL_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "dictavoz",
    "models",
)
BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

# Directorios donde puede haber modelos ya instalados.
SEARCH_DIRS = [
    MODEL_DIR,
    os.path.expanduser("~/.local/share/whisper-cpp"),
    os.path.expanduser("~/.cache/whisper.cpp"),
    os.path.expanduser("~/.cache/whisper"),
    "/usr/share/whisper.cpp/models",
    "/usr/share/whisper-cpp/models",
    "/usr/share/whisper.cpp",
]

# Catálogo de modelos multilingües (sirven para español).
MODELS = [
    {"key": "tiny", "label": "Tiny · muy rápido, calidad básica",
     "file": "ggml-tiny.bin", "bytes": 77691713},
    {"key": "base", "label": "Base · rápido, calidad aceptable",
     "file": "ggml-base.bin", "bytes": 147951465},
    {"key": "small", "label": "Small · equilibrado (recomendado)",
     "file": "ggml-small.bin", "bytes": 487601967},
    {"key": "medium", "label": "Medium · más preciso, más lento",
     "file": "ggml-medium.bin", "bytes": 1533776805},
    {"key": "large-v3-turbo", "label": "Large v3 Turbo · muy preciso y rápido",
     "file": "ggml-large-v3-turbo.bin", "bytes": 1624555275},
    {"key": "large-v3-turbo-q5_0", "label": "Large v3 Turbo Q5 · preciso, ocupa menos",
     "file": "ggml-large-v3-turbo-q5_0.bin", "bytes": 574075153},
    {"key": "large-v3", "label": "Large v3 · máxima precisión",
     "file": "ggml-large-v3.bin", "bytes": 3094623691},
]

BY_KEY = {entry["key"]: entry for entry in MODELS}


class DownloadCancelled(RuntimeError):
    pass


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit != "GB" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def url_for(entry: dict) -> str:
    return f"{BASE_URL}/{entry['file']}"


def find_existing(filename: str) -> str | None:
    """Busca un modelo por nombre de fichero en los directorios conocidos."""
    for directory in SEARCH_DIRS:
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return candidate
    return None


def local_path(entry: dict) -> str:
    return os.path.join(MODEL_DIR, entry["file"])


def is_downloaded(entry: dict) -> bool:
    return find_existing(entry["file"]) is not None


def entry_for_path(path: str) -> dict | None:
    if not path:
        return None
    name = os.path.basename(path)
    for entry in MODELS:
        if entry["file"] == name:
            return entry
    return None


# Directorios de sistema: no se borran modelos de aquí (requieren sudo).
SYSTEM_DIRS = [
    "/usr/share/whisper.cpp/models",
    "/usr/share/whisper-cpp/models",
    "/usr/share/whisper.cpp",
]


def file_paths(entry: dict) -> list[str]:
    """Rutas donde está instalado el fichero de este modelo."""
    return [
        os.path.join(directory, entry["file"])
        for directory in SEARCH_DIRS
        if os.path.isfile(os.path.join(directory, entry["file"]))
    ]


def file_size(entry: dict) -> int:
    """Tamaño total (bytes) que ocupa este modelo en disco."""
    return sum(os.path.getsize(path) for path in file_paths(entry))


def total_downloaded_bytes() -> int:
    """Tamaño total de todos los modelos descargados (sin duplicar rutas)."""
    seen: set[str] = set()
    total = 0
    for entry in MODELS:
        for path in file_paths(entry):
            if path not in seen:
                seen.add(path)
                total += os.path.getsize(path)
    return total


def delete(entry: dict) -> list[str]:
    """Borra un modelo de los directorios de usuario; devuelve el listado borrado.

    Nunca toca los directorios del sistema (/usr/share/...).
    """
    removed: list[str] = []
    for path in file_paths(entry):
        if os.path.dirname(path) in SYSTEM_DIRS:
            continue
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    part = os.path.join(MODEL_DIR, entry["file"] + ".part")
    if os.path.isfile(part):
        try:
            os.remove(part)
        except OSError:
            pass
    return removed


def download(entry: dict, progress=None, cancel=None) -> str:
    """Descarga un modelo (con reanudación). Devuelve la ruta final.

    progress(hecho, total) se llama periódicamente; cancel() -> bool aborta.
    """
    os.makedirs(MODEL_DIR, exist_ok=True)
    dest = local_path(entry)
    if os.path.isfile(dest):
        return dest
    part = dest + ".part"
    offset = os.path.getsize(part) if os.path.isfile(part) else 0

    headers = {"User-Agent": "DictaVoz/0.1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url_for(entry), headers=headers)

    with urllib.request.urlopen(request, timeout=60) as response:
        if offset and response.status != 206:
            # El servidor ignoró el rango: empezamos de cero.
            offset = 0
        remaining = response.headers.get("Content-Length")
        total = (int(remaining) + offset) if remaining else entry["bytes"]
        mode = "ab" if offset else "wb"
        done = offset
        with open(part, mode) as handle:
            while True:
                if cancel is not None and cancel():
                    raise DownloadCancelled("descarga cancelada")
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)

    os.replace(part, dest)
    return dest
