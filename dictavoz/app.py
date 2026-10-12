"""Aplicación GTK4 de DictaVoz: UI y lógica de dictado."""

from __future__ import annotations

import os
import queue
import signal
import tempfile
import threading
import time
import wave

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from . import config as cfg
from . import models
from .audio import Recorder, StreamingRecorder, list_sources
from .hotkey import format_hotkey, HotkeyListener, parse_hotkey
from .inject import InjectError, VirtualKeyboard, load_keymap
from .stt import Transcriber, find_model
from .vad import UtteranceSplitter

STATE_IDLE = "idle"
STATE_REC = "recording"
STATE_WORK = "working"

LANGUAGES = [
    ("Español", "es"),
    ("Inglés", "en"),
    ("Detectar", "auto"),
]

TEST_TEXT = "Hola, esto es una prueba de DictaVoz: á é í ó ú ñ ¿qué tal? ¡Adiós!"


class DictaVozApp(Gtk.Application):
    def __init__(self, settings: dict, smoke: bool = False):
        super().__init__(
            application_id="io.github.zinike.dictavoz",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.settings = settings
        self.smoke = smoke
        self.window: Gtk.ApplicationWindow | None = None

        self.state = STATE_IDLE
        self._lock = threading.Lock()
        self._capturing = False
        self._closing = False
        self._last_text = ""

        self.hotkey_mods, self.hotkey_code = parse_hotkey(settings["hotkey"])
        self.recorder = Recorder(source=settings["source"])
        self.transcriber = self._new_transcriber()
        self.keyboard: VirtualKeyboard | None = None
        self.keyboard_error: str | None = None
        self.listener: HotkeyListener | None = None

        # Estado de descarga de modelos.
        self._downloading = False
        self._download_cancel = threading.Event()
        self._suppress_model_signal = False
        self._last_progress_time = 0.0

        # Estado del dictado continuo (escribir mientras se habla).
        self.stream_transcriber: Transcriber | None = None
        self._stream_splitter: UtteranceSplitter | None = None
        self._stream_rec: StreamingRecorder | None = None
        self._stream_queue: queue.Queue | None = None
        self._stream_text = ""

        self._build_engine()

    # ------------------------------ motor ------------------------------ #
    def _new_transcriber(self) -> Transcriber:
        return Transcriber(
            model=self.settings["model"],
            language=self.settings["language"],
            threads=int(self.settings["threads"]),
            port=int(self.settings["server_port"]),
        )

    def _build_engine(self) -> None:
        try:
            keymap = load_keymap(self.settings["layout"])
            self.keyboard = VirtualKeyboard(keymap)
        except InjectError as error:
            self.keyboard_error = str(error)

        self.listener = HotkeyListener(
            get_hotkey=lambda: (self.hotkey_mods, self.hotkey_code),
            on_press=lambda mods, code: GLib.idle_add(self._engine_press, mods, code),
            on_release=lambda code: GLib.idle_add(self._engine_release),
            is_capturing=lambda: self._capturing,
            on_capture=lambda mods, code: GLib.idle_add(self._on_capture, mods, code),
        )
        self.listener.start()

    # --------------------------- flujo dictado --------------------------- #
    def _engine_press(self, _mods, _code) -> bool:
        if self._capturing or self._closing:
            return False
        if self.settings["mode"] == "toggle":
            self.toggle()
        elif self.state == STATE_IDLE:
            self.start_recording()
        return False

    def _engine_release(self) -> bool:
        if self._capturing or self._closing:
            return False
        if self.settings["mode"] == "hold" and self.state == STATE_REC:
            self.stop_recording()
        return False

    def toggle(self) -> None:
        if self.state == STATE_IDLE:
            self.start_recording()
        elif self.state == STATE_REC:
            self.stop_recording()

    def start_recording(self) -> None:
        with self._lock:
            if self.state != STATE_IDLE:
                return
            self.state = STATE_REC
        if self.settings["streaming"]:
            try:
                self._begin_stream()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self.state = STATE_IDLE
                self._set_status(f"⚠️ No se pudo empezar: {exc}")
                self._refresh_button()
            return
        try:
            self.recorder.start()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.state = STATE_IDLE
            self._set_status(f"⚠️ No se pudo acceder al micrófono: {exc}")
            self._refresh_button()
            return
        self._set_status("🔴 Grabando… habla ahora.")
        self._refresh_button()

    def stop_recording(self) -> None:
        with self._lock:
            if self.state != STATE_REC:
                return
            self.state = STATE_WORK
        if self.settings["streaming"] and self._stream_rec is not None:
            self._stop_stream()
            return
        self._set_status("⏳ Transcribiendo…")
        self._refresh_button()

        try:
            wav = self.recorder.stop()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.state = STATE_IDLE
            self._set_status(f"⚠️ Error al detener la grabación: {exc}")
            self._refresh_button()
            return
        threading.Thread(target=self._work, args=(wav,), daemon=True).start()

    # ------------------------ dictado continuo ------------------------ #
    def _stream_transcriber(self) -> Transcriber:
        """Modelo pequeño dedicado al dictado continuo (rápido)."""
        return self.stream_transcriber or self.transcriber

    def _begin_stream(self) -> None:
        splitter = UtteranceSplitter(
            min_silence_ms=int(self.settings["vad_min_silence_ms"]),
            max_utterance_ms=int(self.settings["vad_max_utterance_ms"]),
        )
        self._stream_splitter = splitter
        self._stream_text = ""
        self._stream_queue = queue.Queue()

        def on_chunk(data: bytes) -> None:
            for chunk in splitter.feed(data):
                self._stream_queue.put(chunk)

        recorder = StreamingRecorder(source=self.settings["source"])
        recorder.start(on_chunk)
        self._stream_rec = recorder

        self._stream_worker_thread = threading.Thread(
            target=self._stream_worker, daemon=True
        )
        self._stream_worker_thread.start()

        text = self._stream_transcriber().model or "…"
        self._set_status(
            f"🔴 Escuchando… escribiré a medida que hables (modelo ágil: "
            f"{os.path.basename(text)})."
        )
        self._refresh_button()

    def _stop_stream(self) -> None:
        self._set_status("⏳ Terminando de escribir…")
        self._refresh_button()
        recorder, self._stream_rec = self._stream_rec, None
        splitter = self._stream_splitter
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:  # noqa: BLE001
                pass
        if splitter is not None and self._stream_queue is not None:
            for chunk in splitter.flush():
                self._stream_queue.put(chunk)
            self._stream_queue.put(None)  # fin de sesión
        self._stream_splitter = None

    def _stream_worker(self) -> None:
        transcriber = self._stream_transcriber()
        while True:
            chunk = self._stream_queue.get()
            if chunk is None:
                break
            try:
                text = self._transcribe_chunk(chunk, transcriber)
                if text and self.keyboard is not None:
                    self.keyboard.type_text(
                        text, append_space=bool(self.settings["append_space"])
                    )
                    GLib.idle_add(self._stream_chunk_typed, text)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(
                    self._set_status, f"⚠️ Error transcribiendo: {exc}"
                )
        GLib.idle_add(self._stream_finished)

    def _transcribe_chunk(self, chunk: bytes, transcriber: Transcriber) -> str:
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="dictavoz-")
        os.close(fd)
        try:
            with wave.open(path, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(chunk)
            return transcriber.transcribe(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _stream_chunk_typed(self, text: str) -> bool:
        self._stream_text = (self._stream_text + " " + text).strip()
        if self.transcript_view is not None:
            buffer = self.transcript_view.get_buffer()
            end = buffer.get_end_iter()
            if buffer.get_char_count() > 0:
                buffer.insert(end, " ")
                end = buffer.get_end_iter()
            buffer.insert(end, text)
        short = text[:80] + ("…" if len(text) > 80 else "")
        self._set_status(f"✅ Escrito: {short}")
        return False

    def _stream_finished(self) -> bool:
        with self._lock:
            self.state = STATE_IDLE
        self._stream_queue = None
        self._stream_splitter = None
        if self._stream_text:
            self._set_status(f"✅ Dictado terminado.{' ' + self._stream_text[:80] + '…'}")
        else:
            self._set_status("No se detectó habla.")
        self._refresh_button()
        return False

    def _work(self, wav: str) -> None:
        text = ""
        error = None
        typing_note = None
        try:
            seconds = Recorder.wav_seconds(wav)
            if seconds < float(self.settings["min_seconds"]):
                text = ""
            elif not self.transcriber.available:
                error = "No se encontró ningún modelo de whisper.cpp."
            else:
                text = self.transcriber.transcribe(wav)
                if text and self.keyboard is not None:
                    try:
                        unknown = self.keyboard.type_text(
                            text, append_space=bool(self.settings["append_space"])
                        )
                        if unknown:
                            typing_note = (
                                "Caracteres no disponibles en el teclado: "
                                + "".join(sorted(unknown))
                            )
                    except Exception as exc:  # noqa: BLE001
                        typing_note = f"No se pudo escribir: {exc}"
        except Exception as exc:  # noqa: BLE001 - informamos al usuario
            error = str(exc)
        finally:
            try:
                os.remove(wav)
            except OSError:
                pass
            GLib.idle_add(self._finish_work, text, error, typing_note)

    def _finish_work(
        self, text: str, error: str | None, typing_note: str | None = None
    ) -> bool:
        with self._lock:
            self.state = STATE_IDLE
        if error:
            self._set_status(f"⚠️ Error: {error}")
            self._show_transcript(text)
        elif text:
            self._last_text = text
            self._show_transcript(text)
            self._set_status(f"✅ Escrito: {text}")
            if typing_note:
                self._set_status(f"✅ Escrito (parcial): {typing_note}")
        else:
            self._set_status("No se detectó habla.")
        self._refresh_button()
        return False

    def _show_transcript(self, text: str) -> None:
        if text and self.transcript_view is not None:
            self.transcript_view.get_buffer().set_text(text)

    # ------------------------------- atajo ------------------------------- #
    def _start_capture(self, _button) -> None:
        if self.keyboard_error:
            return
        self._capturing = True
        if self.capture_button is not None:
            self.capture_button.set_label("Pulsa una tecla…")
        self._set_status(
            "Pulsa la combinación (p. ej. Ctrl+Alt+F9). Los modificadores se "
            "detectan solos: basta con mantenerlos y pulsar la tecla."
        )

    def _on_capture(self, mods: frozenset, code: int) -> bool:
        if not self._capturing:
            return False
        self._capturing = False
        self.hotkey_mods = mods
        self.hotkey_code = code
        combo = format_hotkey(mods, code)
        self.settings["hotkey"] = combo
        self._persist()
        if self.capture_button is not None:
            self.capture_button.set_label("Cambiar")
        if self.hotkey_label is not None:
            self.hotkey_label.set_text(f"Atajo: {combo}")
        if getattr(self, "compact_hotkey_label", None) is not None:
            self.compact_hotkey_label.set_text(f"Atajo: {combo}")
        self._set_status(f"Atajo cambiado a {combo}.")
        return False

    # -------------------------------- UI -------------------------------- #
    def do_activate(self) -> None:
        if self.window is not None:
            self.window.present()
            return
        # Cierre limpio ante Ctrl-C, cierre de sesión o SIGTERM
        # (mata el whisper-server para no dejarlo huérfano).
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_signal)
        self._build_ui()
        self.window.present()
        if not self.keyboard_error and self.transcriber.available:
            threading.Thread(target=self._preload, daemon=True).start()
        if self.settings["streaming"]:
            threading.Thread(target=self._stream_background_setup, daemon=True).start()
        if self.smoke:
            GLib.timeout_add(1500, self._quit)

    def _on_signal(self) -> bool:
        self._shutdown()
        self.quit()
        return False

    def _preload(self) -> None:
        ok = self.transcriber.preload()
        if ok:
            GLib.idle_add(self._set_status, "Listo. Modelo cargado en memoria.")
        else:
            GLib.idle_add(
                self._set_status, "Listo (el modelo se cargará al primer dictado)."
            )

    def _build_ui(self) -> None:
        self.window = Gtk.ApplicationWindow(application=self, title="DictaVoz")
        self.window.set_default_size(460, -1)
        self.window.connect("close-request", self._on_close)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(16)

        title = Gtk.Label(label="DictaVoz")
        title.add_css_class("title-1")
        title.set_hexpand(True)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        icon = self._load_app_icon(44)
        if icon is not None:
            header.append(icon)
        header.append(title)
        compact_button = Gtk.Button(label="Compacto ▾")
        compact_button.set_valign(Gtk.Align.CENTER)
        compact_button.set_tooltip_text(
            "Modo compacto: una ventana pequeña para tener a mano."
        )
        compact_button.connect("clicked", lambda *_: self._show_compact())
        header.append(compact_button)
        box.append(header)

        subtitle = Gtk.Label(
            label="Habla y el texto aparece donde tengas el cursor."
        )
        subtitle.add_css_class("dim-label")
        subtitle.set_xalign(0)
        subtitle.set_wrap(True)
        box.append(subtitle)

        self.toggle_button = Gtk.Button(label="Activar dictado")
        self.toggle_button.add_css_class("suggested-action")
        self.toggle_button.add_css_class("pill")
        self.toggle_button.connect("clicked", lambda *_: self.toggle())
        box.append(self.toggle_button)

        self.status_label = Gtk.Label(label="Listo.")
        self.status_label.set_xalign(0)
        self.status_label.set_wrap(True)
        box.append(self.status_label)

        transcript_label = Gtk.Label(label="Última transcripción:")
        transcript_label.set_xalign(0)
        transcript_label.add_css_class("dim-label")
        box.append(transcript_label)

        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(70)
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.transcript_view = Gtk.TextView()
        self.transcript_view.set_editable(False)
        self.transcript_view.set_cursor_visible(False)
        self.transcript_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll.set_child(self.transcript_view)
        box.append(scroll)

        # Los ajustes secundarios van plegados para simplificar la ventana.
        settings = Gtk.Expander(label="Ajustes")
        settings.set_expanded(False)
        settings.connect("notify::expanded", self._on_settings_expanded)
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        settings_box.set_margin_top(8)
        settings.set_child(settings_box)
        box.append(settings)

        grid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        grid.append(self._row("Modo", self._mode_dropdown()))
        grid.append(self._row("Idioma", self._language_dropdown()))
        grid.append(self._row("Micrófono", self._source_dropdown()))
        grid.append(self._row("Espacio final", self._append_switch()))
        grid.append(self._row("Escritura continua", self._streaming_switch()))
        grid.append(self._hotkey_row())
        grid.append(self._models_section())
        settings_box.append(grid)

        test_label = Gtk.Label(label="Pruébalo aquí:")
        test_label.set_xalign(0)
        test_label.add_css_class("dim-label")
        settings_box.append(test_label)

        self.test_entry = Gtk.Entry()
        self.test_entry.set_placeholder_text("Haz clic aquí y usa el atajo…")
        self.test_entry.set_hexpand(True)
        settings_box.append(self.test_entry)

        test_button = Gtk.Button(label="Escribir texto de prueba")
        test_button.connect("clicked", self._on_test_write)
        settings_box.append(test_button)

        hint = Gtk.Label()
        hint.set_markup(
            f"Atajo global: <b>{format_hotkey(self.hotkey_mods, self.hotkey_code)}</b> "
            "(funciona aunque la ventana no tenga el foco)."
        )
        hint.set_xalign(0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        box.append(hint)

        footer = Gtk.Label(label="Desarrollado por Marcos Zingaretti")
        footer.add_css_class("dim-label")
        footer.set_xalign(0)
        box.append(footer)

        self.window.set_child(box)
        self._apply_initial_state()

    # --------------------- versión compacta (compañero) --------------------- #
    def _build_compact_window(self) -> None:
        """Ventana reducida tipo compañero de escritorio.

        Es una ventana propia (no una vista incrustada) para que pueda ser
        realmente pequeña: un ``Gtk.Stack`` nunca encoge por debajo del tamaño
        natural de su página más grande.
        """
        window = Gtk.Window(application=self, title="DictaVoz")
        window.set_resizable(False)
        window.connect("close-request", self._on_close)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(14)
        window.set_child(box)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        icon = self._load_app_icon(24)
        if icon is not None:
            header.append(icon)
        title = Gtk.Label(label="DictaVoz")
        title.add_css_class("title-4")
        title.set_hexpand(True)
        header.append(title)
        expand_button = Gtk.Button(label="Completo ⤢")
        expand_button.set_tooltip_text("Volver a la ventana de ajustes.")
        expand_button.connect("clicked", lambda *_: self._show_full())
        header.append(expand_button)
        box.append(header)

        self.compact_status_label = Gtk.Label(label="Listo.")
        self.compact_status_label.set_xalign(0)
        self.compact_status_label.set_wrap(True)
        self.compact_status_label.set_max_width_chars(38)
        box.append(self.compact_status_label)

        self.compact_toggle_button = Gtk.Button(label="🎙 Dictar")
        self.compact_toggle_button.add_css_class("suggested-action")
        self.compact_toggle_button.add_css_class("pill")
        self.compact_toggle_button.set_size_request(-1, 48)
        self.compact_toggle_button.connect("clicked", lambda *_: self.toggle())
        box.append(self.compact_toggle_button)

        self.compact_hotkey_label = Gtk.Label(
            label=f"Atajo: {format_hotkey(self.hotkey_mods, self.hotkey_code)}"
        )
        self.compact_hotkey_label.set_xalign(0)
        self.compact_hotkey_label.set_wrap(True)
        self.compact_hotkey_label.add_css_class("dim-label")
        box.append(self.compact_hotkey_label)

        self.compact_window = window
        self._apply_initial_state()

    def _show_compact(self) -> None:
        if getattr(self, "compact_window", None) is None:
            self._build_compact_window()
        else:
            self._apply_initial_state()
        self.window.hide()
        self.compact_window.present()

    def _show_full(self) -> None:
        if getattr(self, "compact_window", None) is not None:
            self.compact_window.hide()
        self.window.present()

    def _on_settings_expanded(self, expander, _param) -> bool:
        """GTK4 crece al desplegar Ajustes pero no encoge al plegarlos.

        Guardamos el alto previo al desplegar y lo restauramos al plegar
        (``set_default_size`` sí redimensiona una ventana ya visible).
        """
        if expander.get_expanded():
            self._collapsed_height = self.window.get_height()
        elif getattr(self, "_collapsed_height", None):
            GLib.idle_add(self._restore_collapsed_height)
        return False

    def _restore_collapsed_height(self) -> bool:
        height = getattr(self, "_collapsed_height", None)
        if self.window is None or not height:
            return False
        self.window.set_default_size(self.window.get_width(), height)
        return False

    def _apply_initial_state(self) -> None:
        broken = bool(self.keyboard_error or not self.transcriber.available)
        for button in (
            self.toggle_button,
            getattr(self, "compact_toggle_button", None),
        ):
            if button is not None:
                button.set_sensitive(not broken)
        if self.keyboard_error:
            self._set_status(f"⚠️ {self.keyboard_error}")
        elif not self.transcriber.available:
            self._set_status(
                "⚠️ No se encontró ningún modelo de whisper.cpp "
                "(instala p. ej. whisper.cpp-model-base o descarga un ggml-*.bin)."
            )

    @staticmethod
    def _row(text: str, widget: Gtk.Widget) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        label = Gtk.Label(label=text)
        label.set_xalign(0)
        label.set_hexpand(True)
        row.append(label)
        row.append(widget)
        return row

    def _load_app_icon(self, size: int):
        """Carga el icono SVG de DictaVoz para la cabecera (opcional)."""
        try:
            import gi as _gi

            _gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf  # noqa: PLC0415

            path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..",
                "icons",
                "dictavoz.svg",
            )
            if not os.path.isfile(path):
                return None
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
            picture = Gtk.Picture.new_for_pixbuf(pixbuf)
            picture.set_size_request(size, size)
            return picture
        except Exception:  # noqa: BLE001 - el icono es opcional
            return None

    def _mode_dropdown(self) -> Gtk.DropDown:
        labels = ["Mantener tecla", "Pulsar para iniciar/parar"]
        self._mode_values = ["hold", "toggle"]
        dropdown = Gtk.DropDown.new_from_strings(labels)
        dropdown.set_selected(self._mode_values.index(self.settings["mode"]))
        dropdown.connect("notify::selected", self._on_mode)
        return dropdown

    def _language_dropdown(self) -> Gtk.DropDown:
        labels = [name for name, _ in LANGUAGES]
        values = [value for _, value in LANGUAGES]
        self._language_values = values
        try:
            index = values.index(self.settings["language"])
        except ValueError:
            index = 0
        dropdown = Gtk.DropDown.new_from_strings(labels)
        dropdown.set_selected(index)
        dropdown.connect("notify::selected", self._on_language)
        return dropdown

    def _source_dropdown(self) -> Gtk.DropDown:
        self._source_values = [""]
        labels = ["Predeterminado"]
        for name, description in list_sources():
            self._source_values.append(name)
            labels.append(description)
        try:
            index = self._source_values.index(self.settings["source"])
        except ValueError:
            index = 0
        self._source_values = self._source_values[: len(labels)]
        dropdown = Gtk.DropDown.new_from_strings(labels)
        dropdown.set_selected(index)
        dropdown.connect("notify::selected", self._on_source)
        return dropdown

    def _append_switch(self) -> Gtk.Switch:
        switch = Gtk.Switch()
        switch.set_active(bool(self.settings["append_space"]))
        switch.set_halign(Gtk.Align.END)
        switch.connect("notify::active", self._on_append)
        return switch

    def _streaming_switch(self) -> Gtk.Switch:
        switch = Gtk.Switch()
        switch.set_active(bool(self.settings["streaming"]))
        switch.set_halign(Gtk.Align.END)
        switch.set_tooltip_text(
            "Escribe el texto a medida que hablas (usa un modelo pequeño y "
            "rápido). Si está apagado, transcribe todo al soltar el atajo."
        )
        switch.connect("notify::active", self._on_streaming)
        return switch

    def _hotkey_row(self) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.hotkey_label = Gtk.Label(
            label=f"Atajo: {format_hotkey(self.hotkey_mods, self.hotkey_code)}"
        )
        self.hotkey_label.set_xalign(0)
        self.hotkey_label.set_hexpand(True)
        row.append(self.hotkey_label)
        self.capture_button = Gtk.Button(label="Cambiar")
        self.capture_button.connect("clicked", self._start_capture)
        row.append(self.capture_button)
        return row

    # ----------------------------- modelos ----------------------------- #
    @staticmethod
    def _model_label(entry: dict) -> str:
        # Nombre corto (sin la descripción tras «·») para que el desplegable no
        # ensanche la ventana; la descripción completa va en el tooltip.
        short = entry["label"].split(" · ")[0]
        if models.is_downloaded(entry):
            return f"{short}  ✓  ({models.human_size(models.file_size(entry))})"
        return f"{short}  ·  {models.human_size(entry['bytes'])}"

    def _current_model_index(self) -> int:
        entry = models.entry_for_path(self.settings["model"])
        if entry is None:
            entry = models.entry_for_path(find_model(""))
        if entry is None:
            return min(2, len(models.MODELS) - 1)
        return models.MODELS.index(entry)

    def _models_section(self) -> Gtk.Box:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.model_dropdown = Gtk.DropDown.new_from_strings(
            [self._model_label(entry) for entry in models.MODELS]
        )
        self.model_dropdown.set_selected(self._current_model_index())
        self.model_dropdown.connect("notify::selected", self._on_model_selected)
        section.append(self._row("Modelo", self.model_dropdown))

        self.model_progress = Gtk.ProgressBar()
        self.model_progress.set_show_text(True)
        self.model_progress.set_visible(False)
        section.append(self.model_progress)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        self.cancel_download_button = Gtk.Button(label="Cancelar descarga")
        self.cancel_download_button.set_visible(False)
        self.cancel_download_button.connect("clicked", self._on_cancel_download)
        buttons.append(self.cancel_download_button)
        self.delete_model_button = Gtk.Button(label="Eliminar modelo")
        self.delete_model_button.add_css_class("destructive-action")
        self.delete_model_button.connect("clicked", self._on_delete_model)
        buttons.append(self.delete_model_button)
        choose_button = Gtk.Button(label="Elegir archivo…")
        choose_button.connect("clicked", self._choose_model)
        buttons.append(choose_button)
        section.append(buttons)

        self.models_total_label = Gtk.Label(label="")
        self.models_total_label.set_xalign(0)
        self.models_total_label.add_css_class("dim-label")
        section.append(self.models_total_label)
        self._refresh_models_total()
        return section

    def _refresh_models_total(self) -> bool:
        total = models.total_downloaded_bytes()
        if total:
            self.models_total_label.set_text(
                f"Ocupado por modelos descargados: {models.human_size(total)}"
            )
        else:
            self.models_total_label.set_text("No hay modelos descargados.")
        self._update_delete_button()
        return False

    def _update_delete_button(self) -> None:
        if self.delete_model_button is None:
            return
        entry = models.MODELS[self.model_dropdown.get_selected()]
        downloaded = models.is_downloaded(entry)
        self.delete_model_button.set_sensitive(bool(downloaded))
        self.delete_model_button.set_tooltip_text(
            "" if downloaded else "Este modelo no está descargado."
        )
        self.model_dropdown.set_tooltip_text(entry["label"])

    def _refresh_model_dropdown(self, select_file: str | None = None) -> None:
        labels = [self._model_label(entry) for entry in models.MODELS]
        self._suppress_model_signal = True
        self.model_dropdown.set_model(Gtk.StringList.new(labels))
        if select_file:
            entry = next((e for e in models.MODELS if e["file"] == select_file), None)
            if entry is not None:
                self.model_dropdown.set_selected(models.MODELS.index(entry))
        else:
            self.model_dropdown.set_selected(self._current_model_index())
        self._suppress_model_signal = False
        self._refresh_models_total()

    def _on_model_selected(self, dropdown, _param) -> None:
        if self._suppress_model_signal or self._downloading:
            return False
        entry = models.MODELS[dropdown.get_selected()]
        existing = models.find_existing(entry["file"])
        self._update_delete_button()
        if existing:
            self._use_model(entry, existing)
        else:
            # No está descargado: se descarga automáticamente.
            self._start_download(entry)
        return False

    def _use_model(self, entry: dict, path: str) -> None:
        self.settings["model"] = path
        self._persist()
        self._rebuild_transcriber()
        self._set_status(f"Modelo activo: {entry['label']}")

    def _start_download(self, entry: dict, activate: bool = True) -> None:
        if self._downloading:
            return
        self._downloading = True
        self._download_cancel.clear()
        self._last_progress_time = 0.0
        self.model_dropdown.set_sensitive(False)
        self.cancel_download_button.set_visible(True)
        self.model_progress.set_visible(True)
        self.model_progress.set_fraction(0.0)
        self.model_progress.set_text(f"Descargando {entry['key']}…")
        self._set_status(f"Descargando modelo «{entry['key']}»…")
        threading.Thread(
            target=self._download_worker, args=(entry, activate), daemon=True
        ).start()

    def _download_worker(self, entry: dict, activate: bool = True) -> None:
        def on_progress(done: int, total: int) -> None:
            now = time.monotonic()
            if total and done < total and now - self._last_progress_time < 0.15:
                return
            self._last_progress_time = now
            GLib.idle_add(self._update_progress, done, total)

        try:
            path = models.download(
                entry, progress=on_progress, cancel=self._download_cancel.is_set
            )
            GLib.idle_add(self._download_done, entry, path, None, activate)
        except models.DownloadCancelled:
            GLib.idle_add(self._download_done, entry, None, "cancelada", activate)
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._download_done, entry, None, str(exc), activate)

    def _update_progress(self, done: int, total: int) -> bool:
        if total:
            self.model_progress.set_fraction(min(1.0, done / total))
            percent = done * 100.0 / total
            self.model_progress.set_text(
                f"{percent:.0f}%  ·  "
                f"{models.human_size(done)} / {models.human_size(total)}"
            )
        else:
            self.model_progress.set_text(f"{models.human_size(done)} descargados")
        return False

    def _download_done(
        self, entry: dict, path: str | None, error: str | None, activate: bool = True
    ) -> bool:
        self._downloading = False
        self.model_dropdown.set_sensitive(True)
        self.cancel_download_button.set_visible(False)
        self.model_progress.set_visible(False)
        if error:
            self._set_status(f"⚠️ Descarga de «{entry['key']}»: {error}")
            self._refresh_model_dropdown()
        elif activate:
            self._refresh_model_dropdown(select_file=entry["file"])
            self._use_model(entry, path)
        else:
            # Descarga auxiliar (p. ej. el modelo ágil del dictado continuo):
            # no cambia el modelo principal.
            self._refresh_model_dropdown()
            self._refresh_models_total()
            if export_name := self._maybe_stream_downloaded(path):
                threading.Thread(
                    target=self._start_stream_transcriber_threaded, daemon=True
                ).start()
                self._set_status(
                    f"Modelo ágil «{export_name}» listo para escritura continua."
                )
            else:
                self._set_status(f"Modelo descargado: {os.path.basename(path)}")
        return False

    def _maybe_stream_downloaded(self, path: str) -> str | None:
        """Si `path` es el modelo del dictado continuo, prepara su Transcriber."""
        entry = models.BY_KEY.get(self.settings["streaming_model"])
        if entry is None or entry["file"] != os.path.basename(path):
            return None
        return entry["key"]

    def _start_stream_transcriber_threaded(self) -> None:
        transcriber = self._ensure_stream_transcriber()
        if transcriber is None:
            return
        transcriber.preload()

    def _on_cancel_download(self, _button) -> None:
        self._download_cancel.set()
        self._set_status("Cancelando descarga…")

    # --------------------------- eliminar modelo --------------------------- #
    def _on_delete_model(self, _button) -> None:
        entry = models.MODELS[self.model_dropdown.get_selected()]
        if not models.is_downloaded(entry):
            return
        size = models.human_size(models.file_size(entry))
        in_use = (
            models.entry_for_path(self.settings["model"]) == entry
            or (
                self.stream_transcriber is not None
                and models.entry_for_path(self.stream_transcriber.model) == entry
            )
        )
        try:
            # Nota: Gtk.AlertDialog.new() no existe en PyGObject (constructor
            # varargs marcado como no introspectable); se construye vacío y se
            # configura el mensaje con set_message().
            dialog = Gtk.AlertDialog()
            dialog.set_message("¿Eliminar modelo?")
            dialog.set_buttons(["Cancelar", "Eliminar"])
            detail = f"Se borrará «{entry['key']}» ({size})."
            if in_use:
                detail += "\nEs un modelo en uso: se cambiará a otro disponible."
            dialog.set_detail(detail)
            dialog.choose(
                self.window,
                None,
                lambda dlg, res: self._after_delete_choice(
                    dlg, res, entry, in_use
                ),
            )
        except Exception:  # noqa: BLE001 - si no hay diálogo, no borramos sin confirmar
            self._set_status("⚠️ No se pudo mostrar la confirmación de borrado.")

    def _after_delete_choice(self, dialog, result, entry: dict, in_use: bool) -> None:
        try:
            if dialog.choose_finish(result) != 1:
                return  # "Cancelar"
        except GLib.Error:
            return
        removed = models.delete(entry)
        if not removed:
            self._set_status(
                f"⚠️ No se pudo eliminar «{entry['key']}» (¿permisos o ruta de sistema?)."
            )
            self._refresh_model_dropdown()
            self._refresh_models_total()
            return

        # Si era el modelo principal activo, volvemos a autodetectar otro.
        if models.entry_for_path(self.settings["model"]) == entry:
            self.settings["model"] = ""
            self.transcriber.close()
            self.transcriber = self._new_transcriber()
            self._persist()
            if self.transcriber.available:
                threading.Thread(target=self._preload, daemon=True).start()
        # Y cierro el motor ágil si usaba ese modelo.
        if (
            self.stream_transcriber is not None
            and models.entry_for_path(self.stream_transcriber.model) == entry
        ):
            old = self.stream_transcriber
            self.stream_transcriber = None
            threading.Thread(target=old.close, daemon=True).start()

        self._refresh_model_dropdown()
        self._refresh_models_total()
        if self.transcriber.available:
            self._set_status(
                f"✅ Modelo «{entry['key']}» eliminado ({len(removed)} archivo)."
            )
        else:
            self._set_status("✅ Modelo eliminado. Ya no queda ningún modelo.")

    # ----------------------------- callbacks ----------------------------- #
    def _persist(self) -> None:
        try:
            cfg.save(self.settings)
        except OSError:
            pass

    def _on_mode(self, dropdown, _param) -> None:
        self.settings["mode"] = self._mode_values[dropdown.get_selected()]
        self._persist()
        return False

    def _on_language(self, dropdown, _param) -> None:
        language = self._language_values[dropdown.get_selected()]
        self.settings["language"] = language
        # El idioma se envía en cada petición: no hace falta reiniciar el motor.
        self.transcriber.language = language or "auto"
        self._persist()
        return False

    def _on_source(self, dropdown, _param) -> None:
        self.settings["source"] = self._source_values[dropdown.get_selected()]
        self._persist()
        self.recorder.source = self.settings["source"]
        return False

    def _on_append(self, switch, _param) -> None:
        self.settings["append_space"] = switch.get_active()
        self._persist()
        return False

    def _rebuild_transcriber(self) -> None:
        old = self.transcriber
        self.transcriber = self._new_transcriber()
        threading.Thread(target=old.close, daemon=True).start()
        if not self.transcriber.available:
            self._set_status("⚠️ No se encontró ningún modelo de whisper.cpp.")
        else:
            self._set_status("⏳ Cargando modelo…")
            threading.Thread(target=self._preload, daemon=True).start()

    # -------------------- modelo ágil para dictado continuo -------------------- #
    def _stream_model_path(self) -> str | None:
        entry = models.BY_KEY.get(self.settings["streaming_model"])
        if entry is None:
            return None
        return models.find_existing(entry["file"])

    def _ensure_stream_transcriber(self) -> Transcriber | None:
        """Transcriber con el modelo pequeño (rápido) para escribir mientras se habla."""
        path = self._stream_model_path()
        if not path:
            return None
        if self.stream_transcriber is None:
            self.stream_transcriber = Transcriber(
                model=path,
                language=self.settings["language"],
                threads=int(self.settings["threads"]),
                port=int(self.settings["server_port"]) + 1,
            )
        return self.stream_transcriber

    def _stream_background_setup(self) -> None:
        """Asegura el modelo pequeño (descarga si falta) y lo precarga."""
        entry = models.BY_KEY.get(self.settings["streaming_model"])
        if entry is None:
            return
        if not models.is_downloaded(entry):
            GLib.idle_add(self._start_download, entry, False)
            return
        transcriber = self._ensure_stream_transcriber()
        if transcriber is None:
            return
        ok = transcriber.preload()
        GLib.idle_add(
            self._set_status,
            "Listo. Escritura continua con modelo ágil ("
            f"{os.path.basename(transcriber.model)})."
            if ok
            else "Escritura continua: el modelo ágil se cargará al empezar.",
        )

    def _on_streaming(self, switch, _param) -> None:
        enabled = switch.get_active()
        self.settings["streaming"] = bool(enabled)
        self._persist()
        if enabled:
            self._set_status("Activando la escritura continua…")
            threading.Thread(target=self._stream_background_setup, daemon=True).start()
        else:
            old, self.stream_transcriber = self.stream_transcriber, None
            if old is not None:
                threading.Thread(target=old.close, daemon=True).start()
            self._set_status("Escritura continua desactivada.")
        return False

    def _on_test_write(self, _button) -> None:
        if self.keyboard is None:
            self._set_status("⚠️ El teclado virtual no está disponible.")
            return
        threading.Thread(
            target=self._test_write, daemon=True
        ).start()

    def _test_write(self) -> None:
        unknown = self.keyboard.type_text(TEST_TEXT, append_space=True)
        message = f"Escribiendo prueba: {TEST_TEXT}"
        if unknown:
            message += f" (sin soporte: {''.join(sorted(unknown))})"
        GLib.idle_add(self._set_status, message)

    def _choose_model(self, _button) -> None:
        try:
            dialog = Gtk.FileDialog.new()
            dialog.set_title("Elegir modelo de whisper.cpp")
            filter_ = Gtk.FileFilter()
            filter_.set_name("Modelos GGML (*.bin)")
            filter_.add_pattern("*.bin")
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(filter_)
            dialog.set_filters(filters)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"No se pudo abrir el selector: {exc}")
            return

        def on_open(dlg, result):
            try:
                gfile = dlg.open_finish(result)
            except GLib.Error:
                return
            path = gfile.get_path()
            if not path:
                return
            self.settings["model"] = path
            self._persist()
            self._rebuild_transcriber()
            self._set_status(f"Modelo: {os.path.basename(path)}")

        dialog.open(self.window, None, on_open)

    # ------------------------------ utilidades --------------------------- #
    def _set_status(self, text: str) -> bool:
        if self.status_label is not None:
            self.status_label.set_text(text)
        if getattr(self, "compact_status_label", None) is not None:
            self.compact_status_label.set_text(text)
        return False

    def _refresh_button(self) -> bool:
        # Botón de la vista completa.
        if self.toggle_button is not None:
            if self.state == STATE_REC:
                self.toggle_button.set_label("Detener y escribir")
                self.toggle_button.remove_css_class("suggested-action")
                self.toggle_button.add_css_class("destructive-action")
            else:
                self.toggle_button.set_label("Activar dictado")
                self.toggle_button.remove_css_class("destructive-action")
                self.toggle_button.add_css_class("suggested-action")
        # Botón grande de la vista compacta.
        if getattr(self, "compact_toggle_button", None) is not None:
            if self.state == STATE_REC:
                self.compact_toggle_button.set_label("⏹ Detener")
                self.compact_toggle_button.remove_css_class("suggested-action")
                self.compact_toggle_button.add_css_class("destructive-action")
            else:
                self.compact_toggle_button.set_label("🎙 Dictar")
                self.compact_toggle_button.remove_css_class("destructive-action")
                self.compact_toggle_button.add_css_class("suggested-action")
        return False

    def _quit(self) -> bool:
        self.quit()
        return False

    def _on_close(self, *_args) -> bool:
        # Cualquiera de las dos ventanas cierra la aplicación completa.
        self._shutdown()
        self.quit()
        return False

    def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.listener:
            self.listener.stop()
        if self.recorder.recording:
            self.recorder.stop()
        rec = self._stream_rec
        if rec is not None and rec.recording:
            try:
                rec.stop()
            except Exception:  # noqa: BLE001
                pass
        self._stream_rec = None
        if self.keyboard:
            self.keyboard.close()
            self.keyboard = None
        self.transcriber.close()
        stream = self.stream_transcriber
        self.stream_transcriber = None
        if stream is not None:
            stream.close()
