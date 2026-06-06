"""Phase 6 — macOS menu bar app.

Wraps the voice agent wake loop in a rumps-based system tray application.
Reuses cli.py's pipeline functions for recording, STT, Claude execution, and TTS.
"""

import threading
import time

import rumps

from voice_claude_agent.config import check_mic_permission

# Silence rumps debug output during tests
import logging
logging.getLogger("rumps").setLevel(logging.WARNING)


class VoiceClaudeApp(rumps.App):
    """macOS menu bar app for Voice Claude Agent.

    Menu items:
      Start Wake      — begins the continuous wake loop
      Stop Wake       — stops the wake loop
      Trigger Record  — fires a single record → STT → Claude → TTS cycle
      Mic Status      — shows current microphone permission
      Quit            — exits the application (stops wake loop first)
    """

    DEFAULT_RECORD_SECONDS = 5

    def __init__(
        self,
        stt_backend: str = "text-input",
        *,
        _wake_target: callable | None = None,
        _alert_patch: callable | None = None,
    ):
        super().__init__(
            name="Voice Agent",
            title="\U0001f3a4",
            icon=None,
            quit_button=None,
        )

        self.stt_backend = stt_backend
        self._wake_target = _wake_target or self._run_wake_loop
        self._alert = _alert_patch or self._rumps_alert

        # State
        self._wake_active = False
        self._wake_event = threading.Event()
        self._trigger_event = threading.Event()
        self._wake_thread: threading.Thread | None = None

        # Stable MenuItem refs
        self.start_item = rumps.MenuItem("Start Wake", callback=self._start_wake)
        self.stop_item = rumps.MenuItem("Stop Wake", callback=self._stop_wake)
        self.trigger_item = rumps.MenuItem(
            "Trigger Recording", callback=self._trigger_recording
        )
        self.mic_status_item = rumps.MenuItem(
            "Mic Status: checking...", callback=self._refresh_mic_status_event
        )
        self.quit_item = rumps.MenuItem("Quit", callback=self._quit)

        self.menu = [
            self.start_item,
            self.stop_item,
            self.trigger_item,
            None,
            self.mic_status_item,
            None,
            self.quit_item,
        ]

        self._update_mic_status()
        self._sync_menu_titles()

        # Validate STT backend on startup
        self._validate_stt_backend()

    # ── STT backend validation ───────────────────────────────

    def _validate_stt_backend(self) -> None:
        """Check that the configured STT backend is usable.

        For whisper-cli: verify binary + model. Shows alert + updates
        mic_status_item with a specific error message on failure.
        Does NOT crash — the app stays alive.
        """
        if self.stt_backend != "whisper-cli":
            return

        from voice_claude_agent.stt import _find_whisper_cpp_binary, _resolve_whisper_model

        binary = _find_whisper_cpp_binary()
        if not binary:
            self.mic_status_item.title = "STT: whisper-cli not installed"
            self._alert(
                title="STT Backend Unavailable",
                message=(
                    "whisper-cli is the default STT backend, "
                    "but whisper.cpp is not installed.\n\n"
                    "Install it: brew install whisper-cpp\n"
                    "Or set VOICE_STT_BACKEND=text-input and restart."
                ),
            )
            return

        model, model_err = _resolve_whisper_model()
        if model is None:
            self.mic_status_item.title = "STT: model not found"
            self._alert(
                title="Whisper Model Not Found",
                message=(
                    f"{model_err}\n\n"
                    "Set WHISPER_CPP_MODEL to a downloaded GGML model file,\n"
                    "or set VOICE_STT_BACKEND=text-input and restart."
                ),
            )
            return

    # ── Mic permission helpers ────────────────────────────────

    def _update_mic_status(self) -> None:
        has_mic, detail = check_mic_permission()
        if has_mic:
            self.mic_status_item.title = "Mic: Accessible"
        else:
            short = detail[:40] + "..." if len(detail) > 40 else detail
            self.mic_status_item.title = f"Mic: Denied ({short})"

    def _refresh_mic_status_event(self, sender: rumps.MenuItem) -> None:
        self._update_mic_status()

    @staticmethod
    def _rumps_alert(title: str, message: str) -> None:
        rumps.alert(title=title, message=message)

    def _check_mic_or_alert(self) -> bool:
        has_mic, detail = check_mic_permission()
        if not has_mic:
            self.mic_status_item.title = "Mic: Denied"
            self._alert(
                title="Microphone Not Available",
                message=(
                    f"Cannot start recording: {detail}\n\n"
                    "Grant microphone access in:\n"
                    "System Settings > Privacy & Security > Microphone,\n"
                    "then restart this app."
                ),
            )
            return False
        return True

    # ── Menu title sync ──────────────────────────────────────

    def _sync_menu_titles(self) -> None:
        if self._wake_active:
            self.start_item.title = "Start Wake (running)"
            self.start_item.set_callback(None)
        else:
            self.start_item.title = "Start Wake"
            self.start_item.set_callback(self._start_wake)

    # ── Start / Stop ─────────────────────────────────────────

    def _start_wake(self, sender: rumps.MenuItem) -> None:
        if self._wake_active:
            return
        self._update_mic_status()
        if not self._check_mic_or_alert():
            return
        self._wake_active = True
        self._wake_event.clear()
        self._trigger_event.clear()
        self._sync_menu_titles()
        self._wake_thread = threading.Thread(
            target=self._wake_target, daemon=True, name="wake-loop"
        )
        self._wake_thread.start()

    def _stop_wake(self, sender: rumps.MenuItem) -> None:
        if not self._wake_active:
            return
        self._wake_active = False
        self._wake_event.set()
        self._trigger_event.set()
        if self._wake_thread is not None and self._wake_thread.is_alive():
            self._wake_thread.join(timeout=5.0)
        self._sync_menu_titles()

    def _trigger_recording(self, sender: rumps.MenuItem) -> None:
        """Single-cycle trigger, gated on mic permission."""
        self._update_mic_status()
        if not self._check_mic_or_alert():
            return

        if not self._wake_active:
            self._wake_active = True
            self._wake_event.clear()
            self._trigger_event.set()
            self._sync_menu_titles()
            self._wake_thread = threading.Thread(
                target=self._wake_target, daemon=True, name="wake-loop"
            )
            self._wake_thread.start()
        else:
            self._trigger_event.set()

    def _quit(self, sender: rumps.MenuItem) -> None:
        if self._wake_active:
            self._stop_wake(sender)
        rumps.quit_application()

    # ── Wake loop (background thread) ────────────────────────

    def _run_wake_loop(self) -> None:
        while not self._wake_event.is_set():
            self._trigger_event.wait(timeout=0.5)
            if self._wake_event.is_set():
                break
            if not self._trigger_event.is_set():
                continue
            self._trigger_event.clear()
            self._record_and_execute()

    # ── Non-interactive recording (NO input()) ───────────────

    def _record_and_execute(self) -> None:
        """One full cycle: record N seconds → STT → Claude → TTS → log.

        Uses time-based recording (DEFAULT_RECORD_SECONDS) instead of input().
        Menu status updates at each stage so the user sees progress.
        """
        from voice_claude_agent.cli import _run_pipeline
        from voice_claude_agent.stt import RecordingTranscriber

        # 1. Open mic
        recorder = self._open_mic_or_alert()
        if recorder is None:
            return

        # 2. Record
        self.trigger_item.title = "Recording..."
        audio = self._record_fixed_duration(recorder)
        if not audio:
            self.mic_status_item.title = "Mic: No audio captured"
            self.trigger_item.title = "Trigger Recording"
            self._alert(
                title="No Audio",
                message="No audio was captured. Check your microphone connection.",
            )
            return

        # 3. STT
        self.trigger_item.title = "Transcribing..."
        transcriber = RecordingTranscriber(backend=self.stt_backend)
        transcript = transcriber.transcribe(audio)

        if not transcript.strip():
            self.mic_status_item.title = "Mic: Empty transcript"
            self.trigger_item.title = "Trigger Recording"
            self._alert(
                title="No Speech Detected",
                message="No speech was detected in the recording.",
            )
            return

        if transcript.startswith("[STT error:"):
            self.mic_status_item.title = "Mic: STT Error"
            self.trigger_item.title = "Trigger Recording"
            self._alert(
                title="STT Error",
                message=transcript,
            )
            return

        # 4. Claude pipeline
        self.trigger_item.title = "Running Claude..."
        _run_pipeline(transcript, input_mode="voice", tts_fake=False)

        # 5. Done
        self.trigger_item.title = "Done ✓"
        # Reset after a short visible delay
        threading.Timer(1.5, lambda: setattr(self.trigger_item, "title", "Trigger Recording")).start()

    def _open_mic_or_alert(self):
        """Open the sounddevice recorder. Returns recorder or None (with alert)."""
        from voice_claude_agent.cli import _safe_real_recorder

        recorder = _safe_real_recorder()
        if recorder is None:
            self.mic_status_item.title = "Mic: Error"
            self.trigger_item.title = "Trigger Recording"
            self._alert(
                title="Recording Failed",
                message=(
                    "Could not access the microphone.\n\n"
                    "Check: System Settings > Privacy & Security > Microphone"
                ),
            )
            return None
        return recorder

    def _record_fixed_duration(self, recorder) -> bytes:
        """Record for DEFAULT_RECORD_SECONDS. Returns audio bytes or empty."""
        try:
            recorder.start()
        except Exception as e:
            self.mic_status_item.title = f"Mic: start error ({e})"
            return b""

        time.sleep(self.DEFAULT_RECORD_SECONDS)

        try:
            recorder.stop()
        except Exception as e:
            self.mic_status_item.title = f"Mic: stop error ({e})"
            return b""

        audio = recorder.get_audio()
        return audio or b""


def launch_app(stt_backend: str = "text-input") -> None:
    VoiceClaudeApp(stt_backend=stt_backend).run()
