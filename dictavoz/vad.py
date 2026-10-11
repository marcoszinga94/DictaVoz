"""División del audio en frases usando detección de silencio (VAD de energía).

Funciona sobre PCM s16le mono 16 kHz en vivo: acumula habla mientras hay voz
y emite la frase cuando llega el silencio (o cada máximo de duración, para que
textos muy largos se escriban igualmente de forma continua).
"""

from __future__ import annotations

import math
import struct


class UtteranceSplitter:
    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        min_silence_ms: int = 650,
        max_utterance_ms: int = 10000,
        min_speech_ms: int = 140,
    ):
        self.frame_bytes = int(sample_rate * frame_ms / 1000) * 2  # 2 bytes/muestra
        self.frame_ms = frame_ms
        self.min_silence_ms = min_silence_ms
        self.max_utterance_ms = max_utterance_ms
        self.min_speech_ms = min_speech_ms

        self._speech = False
        self._buf = bytearray()
        self._speech_ms = 0
        self._silence_ms = 0

        # Umbral adaptativo: ruido de fondo medido en los momentos de silencio.
        self._noise = 200.0
        self._noise_acc = 0.0
        self._noise_n = 0
        self._on_threshold = 0.0
        self._off_threshold = 0.0
        self._recompute_threshold()

    def _recompute_threshold(self) -> None:
        # La voz suele estar muy por encima del piso de ruido.
        self._on_threshold = max(self._noise * 4.0, 160.0)
        self._off_threshold = self._on_threshold * 0.75

    def _feed_noise(self, rms: float) -> None:
        self._noise_acc += rms
        self._noise_n += 1
        if self._noise_n >= 25:
            candidate = self._noise_acc / self._noise_n
            self._noise_acc = 0.0
            self._noise_n = 0
            if 0 < candidate < self._noise:
                self._noise = candidate
                self._recompute_threshold()

    @staticmethod
    def _frame_rms(frame: bytes) -> float:
        n = len(frame) // 2
        if n == 0:
            return 0.0
        total = 0
        for index in range(0, n * 2, 2):
            value = struct.unpack_from("<h", frame, index)[0]
            total += value * value
        return math.sqrt(total / n)

    def feed(self, data: bytes) -> list[bytes]:
        """Procesa un bloque de PCM; devuelve las frases completadas."""
        out: list[bytes] = []
        offset = 0
        while offset < len(data):
            frame = bytes(data[offset : offset + self.frame_bytes])
            offset += self.frame_bytes
            if len(frame) < self.frame_bytes:
                # Último frame corto: se guarda para la siguiente llamada.
                if self._speech:
                    self._buf += frame
                break
            completed = self._process_frame(frame)
            if completed is not None:
                out.append(completed)
        return out

    def _process_frame(self, frame: bytes) -> bytes | None:
        rms = self._frame_rms(frame)

        if self._speech:
            self._buf += frame
            self._speech_ms += self.frame_ms
            if rms < self._off_threshold:
                self._silence_ms += self.frame_ms
            else:
                self._silence_ms = 0

            if (
                self._silence_ms >= self.min_silence_ms
                or self._speech_ms >= self.max_utterance_ms
            ):
                return self._emit()
            return None

        # Silencio: medimos el piso de ruido.
        self._feed_noise(rms)
        if rms > self._on_threshold:
            self._speech = True
            self._buf = bytearray(frame)
            self._speech_ms = self.frame_ms
            self._silence_ms = 0
        return None

    def _emit(self) -> bytes | None:
        chunk = None
        if self._speech_ms >= self.min_speech_ms:
            chunk = bytes(self._buf)
        self._speech = False
        self._buf = bytearray()
        self._speech_ms = 0
        self._silence_ms = 0
        return chunk

    def flush(self) -> list[bytes]:
        """Emite la frase en curso (al parar la grabación)."""
        if not self._speech:
            return []
        chunk = self._emit()
        return [chunk] if chunk else []

    @property
    def in_speech(self) -> bool:
        return self._speech