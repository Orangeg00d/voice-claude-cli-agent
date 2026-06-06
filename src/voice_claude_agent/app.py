"""Phase 6 — macOS menu bar app.

Wraps the voice agent wake loop in a rumps-based system tray application.
Reuses cli.py's pipeline functions for recording, STT, Claude execution, and TTS.
"""

import threading

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

    def __init__(
        self,
        stt_backend: str = "text-input",
        *,
        _wake_target: callable | None = None,
    ):
        """Args:
            stt_backend: STT backend name.
            _wake_target: Override for _run_wake_loop (testing only).
        """
        super().__init__(
            name="Voice Agent",
            title="\U0001f3a4",  # 🎤
            icon=None,
            quit_button=None,
        )

        self.stt_backend = stt_backend
        self._wake_target = _wake_target or self._run_wake_loop

        # State
        self._wake_active = False
        self._wake_event = threading.Event()      # set to stop the loop
        self._trigger_event = threading.Event()   # set to trigger one cycle
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

    # ── Mic status ───────────────────────────────────────────

    def _update_mic_status(self) -> None:
        has_mic, detail = check_mic_permission()
        if has_mic:
            self.mic_status_item.title = "Mic: Accessible"
        else:
            short = detail[:40] + "..." if len(detail) > 40 else detail
            self.mic_status_item.title = f"Mic: Denied ({short})"

    def _refresh_mic_status_event(self, sender: rumps.MenuItem) -> None:
        self._update_mic_status()

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
        self._wake_active = True
        self._wake_event.clear()
        self._trigger_event.clear()

        self._update_mic_status()
        self._sync_menu_titles()

        self._wake_thread = threading.Thread(
            target=self._wake_target,
            daemon=True,
            name="wake-loop",
        )
        self._wake_thread.start()

    def _stop_wake(self, sender: rumps.MenuItem) -> None:
        if not self._wake_active:
            return
        self._wake_active = False
        self._wake_event.set()
        self._trigger_event.set()   # unblock any waiting trigger

        if self._wake_thread is not None and self._wake_thread.is_alive():
            self._wake_thread.join(timeout=5.0)

        self._sync_menu_titles()

    def _trigger_recording(self, sender: rumps.MenuItem) -> None:
        """Single-cycle trigger: fire one record→STT→pipeline iteration.

        If the wake loop is not running, starts it temporarily for one cycle.
        If already running, sets the trigger event so the loop picks it up.
        """
        if not self._wake_active:
            # Start the loop in single-shot mode
            self._wake_active = True
            self._wake_event.clear()
            self._trigger_event.set()  # trigger immediately

            self._update_mic_status()
            self._sync_menu_titles()

            self._wake_thread = threading.Thread(
                target=self._wake_target,
                daemon=True,
                name="wake-loop",
            )
            self._wake_thread.start()
        else:
            # Loop already running — just fire the trigger
            self._trigger_event.set()

    def _quit(self, sender: rumps.MenuItem) -> None:
        if self._wake_active:
            self._stop_wake(sender)
        rumps.quit_application()

    # ── Wake loop (background thread) ────────────────────────

    def _run_wake_loop(self) -> None:
        """Event-driven wake loop.

        Waits for _trigger_event (set by "Trigger Recording" menu item or
        _trigger_recording callback). Each trigger fires one full cycle:
        record → STT → risk → Claude → summarize → TTS → log.

        Stop is signaled via _wake_event.
        """
        while not self._wake_event.is_set():
            # Wait for a trigger (no input() — pure threading.Event)
            self._trigger_event.wait(timeout=0.5)
            if self._wake_event.is_set():
                break
            if not self._trigger_event.is_set():
                continue

            self._trigger_event.clear()

            # Run one full cycle
            self._record_and_execute()

    def _record_and_execute(self) -> None:
        """One recording cycle: mic → STT → pipeline."""
        from voice_claude_agent.cli import (
            _run_pipeline,
            _safe_real_recorder,
            _safe_record_attempt,
        )
        from voice_claude_agent.stt import RecordingTranscriber

        recorder = _safe_real_recorder()
        if recorder is None:
            self.mic_status_item.title = "Mic: Error"
            return

        audio = _safe_record_attempt(recorder, max_duration=10)
        if not audio:
            return

        transcriber = RecordingTranscriber(backend=self.stt_backend)
        transcript = transcriber.transcribe(audio)

        if not transcript.strip() or transcript.startswith("[STT error:"):
            return

        _run_pipeline(transcript, input_mode="voice", tts_fake=False)


def launch_app(stt_backend: str = "text-input") -> None:
    """Launch the Voice Claude Agent menu bar app. Blocks until quit."""
    VoiceClaudeApp(stt_backend=stt_backend).run()
