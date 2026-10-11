"""Reconocimiento de voz local con whisper.cpp.

Usa `whisper-server` (mantiene el modelo cargado en memoria, respuesta
rápida) y recurre a `whisper-cli` si el servidor no está disponible.
"""

from __future__ import annotations

import atexit
import glob
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import uuid

# Servidores whisper-server vivos, para matarlos al salir (evita huérfanos).
_RUNNING: "set[subprocess.Popen]" = set()
_RUNNING_LOCK = threading.Lock()


def _kill_all_servers() -> None:
    with _RUNNING_LOCK:
        procs = list(_RUNNING)
        _RUNNING.clear()
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass


atexit.register(_kill_all_servers)

# Directorios donde suelen vivir los modelos ggml de whisper.cpp.
MODEL_DIRS = [
    os.path.expanduser("~/.local/share/dictavoz/models"),
    os.path.expanduser("~/.local/share/whisper-cpp"),
    os.path.expanduser("~/.cache/whisper.cpp"),
    os.path.expanduser("~/.cache/whisper"),
    "/usr/share/whisper.cpp/models",
    "/usr/share/whisper-cpp/models",
    "/usr/share/whisper.cpp",
]

# Orden de preferencia por nombre de modelo (mejor calidad primero).
MODEL_PREFERENCE = ["large-v3-turbo", "medium", "small", "base", "tiny"]

# Binario de la línea de comandos de whisper.cpp (se puede apuntar a uno
# empaquetado, p. ej. en Windows: DICTAVOZ_WHISPER_CLI=/ruta/whisper-cli.exe).
WHISPER_CLI = os.environ.get("DICTAVOZ_WHISPER_CLI", "whisper-cli")


def find_model(explicit: str = "") -> str:
    """Localiza un modelo ggml; devuelve "" si no encuentra ninguno."""
    if explicit and os.path.isfile(explicit):
        return explicit
    found: list[str] = []
    for directory in MODEL_DIRS:
        found.extend(glob.glob(os.path.join(directory, "ggml-*.bin")))
    if not found:
        return ""
    for name in MODEL_PREFERENCE:
        for path in found:
            if name in os.path.basename(path):
                return path
    return sorted(found)[0]


def clean_text(text: str) -> str:
    """Limpia la salida de whisper para escribirla.

    - Colapsa espacios.
    - Elimina los puntos suspensivos ("..." o "…") que whisper añade cuando
      haces una pausa al dictar: no se escriben.
    - Descarta resultados que son sólo un marcador, p. ej. "[Pausa]".
    """
    if not text:
        return ""
    text = " ".join(text.split())
    text = text.strip(" ♪♫")
    # Sin puntos suspensivos (ni en medio ni al final).
    text = re.sub(r"…|\.{2,}", " ", text)
    text = " ".join(text.split())
    if re.fullmatch(r"[\[\(（][^\]\)）]*[\]\)）][.!…]*", text):
        return ""
    if text in {"...", "…", "-", "."}:
        return ""
    return text


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")
    for key, (filename, data) in files.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(body), boundary


class Transcriber:
    """Transcribe WAV usando whisper.cpp (servidor o CLI)."""

    def __init__(
        self,
        model: str,
        language: str = "es",
        threads: int = 0,
        port: int = 8178,
        use_server: bool = True,
    ):
        self.model = find_model(model)
        self.language = language or "auto"
        self.threads = threads or min(os.cpu_count() or 4, 8)
        self.port = port
        self.use_server = use_server
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._ready = False
        self._closed = False
        self.last_backend = ""

    @property
    def available(self) -> bool:
        return bool(self.model)

    # ----------------------------- servidor ----------------------------- #
    def preload(self) -> bool:
        """Arranca whisper-server en segundo plano y espera a que esté listo."""
        if not self.available:
            return False
        with self._lock:
            if self._closed:
                return False
            if self._ready and self._proc is not None and self._proc.poll() is None:
                return True
            proc = self._proc
            if proc is None or proc.poll() is not None:
                try:
                    proc = subprocess.Popen(
                        [
                            "whisper-server",
                            "-m", self.model,
                            "-t", str(self.threads),
                            "--host", "127.0.0.1",
                            "--port", str(self.port),
                            "--inference-path", "/inference",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError:
                    return False
                self._proc = proc
                self._ready = False
                with _RUNNING_LOCK:
                    _RUNNING.add(proc)
        # Espera fuera del lock para no bloquear a close().
        ready = self._wait_ready(proc, timeout=90)
        with self._lock:
            if self._closed or proc is not self._proc:
                self._terminate(proc)
                return False
            self._ready = ready
            return ready

    def _wait_ready(self, proc: subprocess.Popen, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{self.port}/"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(url, timeout=1):
                    return True
            except Exception:  # noqa: BLE001 - cualquier fallo = aún no listo
                time.sleep(0.25)
        return False

    def _server_transcribe(self, wav_path: str) -> str:
        with open(wav_path, "rb") as handle:
            data = handle.read()
        fields = {
            "response_format": "json",
            "language": self.language,
            "temperature": "0.0",
        }
        files = {"file": (os.path.basename(wav_path), data)}
        body, boundary = _multipart(fields, files)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/inference",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(request, timeout=900) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("text", "")

    # -------------------------------- CLI ------------------------------- #
    def _cli_transcribe(self, wav_path: str) -> str:
        prefix = wav_path + ".out"
        cmd = [
            WHISPER_CLI,
            "-m", self.model,
            "-f", wav_path,
            "-l", self.language,
            "-t", str(self.threads),
            "-nt", "--no-prints",
            "-otxt", "-of", prefix,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        txt_path = prefix + ".txt"
        text = ""
        if os.path.isfile(txt_path):
            with open(txt_path, "r", encoding="utf-8") as handle:
                text = handle.read()
            try:
                os.remove(txt_path)
            except OSError:
                pass
        if not text.strip() and result.stdout:
            text = result.stdout
        return text

    # ------------------------------ público ----------------------------- #
    def transcribe(self, wav_path: str) -> str:
        if not self.available:
            raise RuntimeError("No se encontró ningún modelo de whisper.cpp.")
        if self.use_server and self.preload():
            try:
                self.last_backend = "server"
                return clean_text(self._server_transcribe(wav_path))
            except Exception:  # noqa: BLE001 - cae al CLI si el servidor falla
                self._ready = False
        self.last_backend = "cli"
        return clean_text(self._cli_transcribe(wav_path))

    def _terminate(self, proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        with _RUNNING_LOCK:
            _RUNNING.discard(proc)
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            proc = self._proc
            self._proc = None
            self._ready = False
        self._terminate(proc)
