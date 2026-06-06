"""Phase 6 — macOS menu bar app.

Wraps the voice agent wake loop in a rumps-based system tray application.
Reuses cli.py's pipeline functions for recording, STT, Claude execution, and TTS.
"""

import threading

import rumps

from voice_claude_agent.config import check_mic_permission


class VoiceClaudeApp(rumps.App):
    """macOS menu bar app for Voice Claude Agent.

    Menu items:
      Start Wake  — begins the push-to-talk wake loop
      Stop Wake   — stops the wake loop
      Mic Status  — shows current microphone permission state (click to refresh)
      Quit        — exits the application (stops wake loop first)
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

        self._wake_active = False
        self._wake_event = threading.Event()
        self._wake_thread: threading.Thread | None = None

        # Stable menu item references — NEVER use dynamic title lookups
        self.start_item = rumps.MenuItem("Start Wake", callback=self._start_wake)
        self.stop_item = rumps.MenuItem("Stop Wake", callback=self._stop_wake)
        self.mic_status_item = rumps.MenuItem(
            "Mic Status: checking...", callback=self._refresh_mic_status_event
        )
        self.quit_item = rumps.MenuItem("Quit", callback=self._quit)

        self.menu = [
            self.start_item,
            self.stop_item,
            None,
            self.mic_status_item,
            None,
            self.quit_item,
        ]

        self._update_mic_status()
        self._sync_menu_titles()

    # ── Mic status ───────────────────────────────────────────

    def _update_mic_status(self) -> None:
        """Check microphone permission and update the status menu item."""
        has_mic, detail = check_mic_permission()
        if has_mic:
            self.mic_status_item.title = "Mic: Accessible"
        else:
            short = detail[:40] + "..." if len(detail) > 40 else detail
            self.mic_status_item.title = f"Mic: Denied ({short})"

    def _refresh_mic_status_event(self, sender: rumps.MenuItem) -> None:
        """Click handler: refresh mic status on demand."""
        self._update_mic_status()

    # ── Menu title sync ──────────────────────────────────────

    def _sync_menu_titles(self) -> None:
        """Sync start/stop item titles with current state."""
        if self._wake_active:
            self.start_item.title = "Start Wake (running)"
            self.start_item.set_callback(None)
        else:
            self.start_item.title = "Start Wake"
            self.start_item.set_callback(self._start_wake)

    # ── Start / Stop ─────────────────────────────────────────

    def _start_wake(self, sender: rumps.MenuItem) -> None:
        """Begin the wake loop on a background daemon thread."""
        if self._wake_active:
            return
        self._wake_active = True
        self._wake_event.clear()

        self._update_mic_status()
        self._sync_menu_titles()

        self._wake_thread = threading.Thread(
            target=self._wake_target,
            daemon=True,
            name="wake-loop",
        )
        self._wake_thread.start()

    def _stop_wake(self, sender: rumps.MenuItem) -> None:
        """Signal the wake loop to stop and wait for it to finish."""
        if not self._wake_active:
            return
        self._wake_active = False
        self._wake_event.set()

        if self._wake_thread is not None and self._wake_thread.is_alive():
            self._wake_thread.join(timeout=5.0)

        self._sync_menu_titles()

    def _quit(self, sender: rumps.MenuItem) -> None:
        """Stop the wake loop and exit cleanly."""
        if self._wake_active:
            self._stop_wake(sender)
        rumps.quit_application()

    # ── Wake loop (background thread) ────────────────────────

    def _run_wake_loop(self) -> None:
        """The wake loop runs on a daemon thread.

        Each iteration:
          1. Wait for push-to-talk trigger (Enter in terminal for now)
          2. Record from mic
          3. STT transcribe
          4. Run pipeline (risk → Claude → summarize → TTS → log)
        """
        while not self._wake_event.is_set():
            try:
                input("[Wake] Press Enter to trigger...")
            except (EOFError, KeyboardInterrupt):
                break

            if self._wake_event.is_set():
                break

            from voice_claude_agent.cli import (
                _run_pipeline,
                _safe_real_recorder,
                _safe_record_attempt,
            )
            from voice_claude_agent.stt import RecordingTranscriber

            recorder = _safe_real_recorder()
            if recorder is None:
                self.mic_status_item.title = "Mic: Error"
                continue

            audio = _safe_record_attempt(recorder, max_duration=10)
            if not audio:
                continue

            transcriber = RecordingTranscriber(backend=self.stt_backend)
            transcript = transcriber.transcribe(audio)

            if not transcript.strip() or transcript.startswith("[STT error:"):
                continue

            _run_pipeline(transcript, input_mode="voice", tts_fake=False)


def launch_app(stt_backend: str = "text-input") -> None:
    """Launch the Voice Claude Agent menu bar app. Blocks until quit."""
    VoiceClaudeApp(stt_backend=stt_backend).run()
