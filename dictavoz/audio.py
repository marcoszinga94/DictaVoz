"""Captura de audio del micrófono usando parec (PipeWire/PulseAudio)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import wave

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # s16le


def list_sources() -> list[tuple[str, str]]:
    """Devuelve [(nombre, descripción)] de las fuentes de audio disponibles."""
    try:
        result = subprocess.run(
            ["pactl", "list", "sources"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return []
    sources: list[tuple[str, str]] = []
    name = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Description:") and name:
            sources.append((name, line.split(":", 1)[1].strip()))
            name = None
    return sources


class Recorder:
    """Graba del micrófono en segundo plano y produce un WAV al parar."""

    def __init__(self, source: str = "", sample_rate: int = SAMPLE_RATE):
        self.source = source
        self.sample_rate = sample_rate
        self.channels = CHANNELS
        self._proc: subprocess.Popen | None = None
        self._buffer = bytearray()
        self._reader: threading.Thread | None = None
        self._first_data = threading.Event()
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.recording:
            return
        cmd = [
            "parec",
            "--format=s16le",
            f"--rate={self.sample_rate}",
            f"--channels={self.channels}",
            "--raw",
        ]
        if self.source:
            cmd.append(f"--device={self.source}")
        with self._lock:
            self._buffer = bytearray()
            self._first_data.clear()
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        self._reader = threading.Thread(target=self._read, daemon=True, name="recorder")
        self._reader.start()

    def wait_ready(self, timeout: float = 1.5) -> bool:
        """Espera a que empiecen a llegar datos (el micrófono puede tardar)."""
        return self._first_data.wait(timeout)

    def _read(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            try:
                chunk = proc.stdout.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            with self._lock:
                self._buffer.extend(chunk)
            self._first_data.set()

    def stop(self) -> str:
        """Detiene la grabación y devuelve la ruta de un WAV temporal."""
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        if self._reader is not None:
            self._reader.join(timeout=1.0)
        self._proc = None
        with self._lock:
            data = bytes(self._buffer)
            self._buffer = bytearray()

        fd, path = tempfile.mkstemp(suffix=".wav", prefix="dictavoz-")
        os.close(fd)
        with wave.open(path, "wb") as handle:
            handle.setnchannels(self.channels)
            handle.setsampwidth(SAMPLE_WIDTH)
            handle.setframerate(self.sample_rate)
            handle.writeframes(data)
        return path

    @staticmethod
    def wav_seconds(path: str) -> float:
        try:
            with wave.open(path, "rb") as handle:
                if handle.getframerate() == 0:
                    return 0.0
                return handle.getnframes() / float(handle.getframerate())
        except (OSError, wave.Error):
            return 0.0


class StreamingRecorder:
    """Graba en vivo y entrega cada bloque PCM a un callback (para dictado continuo)."""

    def __init__(self, source: str = "", sample_rate: int = SAMPLE_RATE):
        self.source = source
        self.sample_rate = sample_rate
        self.channels = CHANNELS
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._on_chunk = None
        self._first_data = threading.Event()

    @property
    def recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, on_chunk) -> None:
        if self.recording:
            return
        self._on_chunk = on_chunk
        self._first_data.clear()
        cmd = [
            "parec",
            "--format=s16le",
            f"--rate={self.sample_rate}",
            f"--channels={self.channels}",
            "--raw",
        ]
        if self.source:
            cmd.append(f"--device={self.source}")
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        self._reader = threading.Thread(target=self._read, daemon=True, name="recorder")
        self._reader.start()

    def wait_ready(self, timeout: float = 1.5) -> bool:
        return self._first_data.wait(timeout)

    def _read(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        on_chunk = self._on_chunk
        while True:
            try:
                chunk = proc.stdout.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            self._first_data.set()
            if on_chunk is not None:
                try:
                    on_chunk(chunk)
                except Exception:  # noqa: BLE001 - nunca romper la captura
                    pass

    def stop(self) -> None:
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        if self._reader is not None:
            self._reader.join(timeout=1.5)
        self._proc = None
