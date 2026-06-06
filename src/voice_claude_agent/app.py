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
      Mic Status  — shows current microphone permission state
      Quit        — exits the application
    """

    def __init__(self, stt_backend: str = "text-input"):
        super().__init__(
            name="Voice Agent",
            title="🎤",
            icon=None,
            quit_button=None,
        )

        self.stt_backend = stt_backend
        self._wake_active = False
        self._wake_event = threading.Event()
        self._wake_thread: threading.Thread | None = None

        # Build menu
        self.menu = [
            rumps.MenuItem("Start Wake", callback=self._start_wake),
            rumps.MenuItem("Stop Wake", callback=self._stop_wake),
            None,  # separator
            rumps.MenuItem("Mic Status: checking...", callback=None),
            None,  # separator
            rumps.MenuItem("Quit", callback=self._quit),
        ]

        # Update mic status asynchronously
        self._mic_status_item = self.menu["Mic Status: checking..."]
        self._update_mic_status()

    def _update_mic_status(self) -> None:
        """Check microphone permission and update the status menu item."""
        has_mic, detail = check_mic_permission()
        if has_mic:
            self._mic_status_item.title = "Mic: Accessible"
        else:
            self._mic_status_item.title = "Mic: Denied"
            if detail:
                self._mic_status_item.title += f" ({detail})"

    # ── Menu callbacks ──────────────────────────────────────

    def _start_wake(self, sender: rumps.MenuItem) -> None:
        """Begin the wake loop on a background daemon thread."""
        if self._wake_active:
            return
        self._wake_active = True
        self._wake_event.clear()
        sender.title = "Start Wake (active)"
        print("[VoiceClaudeApp] Wake loop starting...")
        self._wake_thread = threading.Thread(
            target=self._run_wake_loop,
            daemon=True,
            name="wake-loop",
        )
        self._wake_thread.start()

    def _stop_wake(self, sender: rumps.MenuItem) -> None:
        """Signal the wake loop to stop."""
        if not self._wake_active:
            return
        self._wake_active = False
        self._wake_event.set()
        print("[VoiceClaudeApp] Wake loop stopping...")
        if self._wake_thread and self._wake_thread.is_alive():
            self._wake_thread.join(timeout=3.0)
        sender.title = "Stop Wake"
        # Reset Start Wake title
        self.menu["Start Wake (active)"].title = "Start Wake"

    def _quit(self, sender: rumps.MenuItem) -> None:
        """Stop the wake loop and exit cleanly."""
        if self._wake_active:
            self._stop_wake(sender)
        rumps.quit_application()

    # ── Wake loop (background thread) ───────────────────────

    def _run_wake_loop(self) -> None:
        """The wake loop runs on a daemon thread.

        Each iteration:
          1. Wait for push-to-talk trigger (Enter in terminal for now)
          2. Record from mic
          3. STT transcribe
          4. Run pipeline (risk → Claude → summarize → TTS → log)
        """
        print("[VoiceClaudeApp] Wake loop started. Press Enter in terminal to trigger.")

        while not self._wake_event.is_set():
            try:
                # Push-to-talk trigger
                input("[Wake] Press Enter to record...")
            except (EOFError, KeyboardInterrupt):
                break

            if self._wake_event.is_set():
                break

            # Import here to avoid circular imports at module load
            from voice_claude_agent.cli import (
                _run_pipeline,
                _safe_real_recorder,
                _safe_record_attempt,
            )
            from voice_claude_agent.stt import RecordingTranscriber

            # Check mic
            recorder = _safe_real_recorder()
            if recorder is None:
                print("[VoiceClaudeApp] Mic unavailable. Skipping this wake.")
                continue

            # Record
            audio = _safe_record_attempt(recorder, max_duration=10)
            if not audio:
                print("[VoiceClaudeApp] No audio captured.")
                continue

            # STT
            transcriber = RecordingTranscriber(backend=self.stt_backend)
            transcript = transcriber.transcribe(audio)
            print(f"[VoiceClaudeApp] Transcript: {transcript}")

            if not transcript.strip() or transcript.startswith("[STT error:"):
                print("[VoiceClaudeApp] STT failed or empty. Skipping.")
                continue

            # Run the full pipeline
            _run_pipeline(transcript, input_mode="voice", tts_fake=False)

        print("[VoiceClaudeApp] Wake loop stopped.")


def launch_app(stt_backend: str = "text-input") -> None:
    """Launch the Voice Claude Agent menu bar app. Blocks until quit."""
    VoiceClaudeApp(stt_backend=stt_backend).run()
