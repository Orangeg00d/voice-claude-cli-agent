"""Phase 6 — macOS menu bar app.

Wraps the voice agent wake loop in a rumps-based system tray application.
Reuses cli.py's pipeline functions for recording, STT, Claude execution, and TTS.
"""

import os
import threading
import time

import rumps

from voice_claude_agent.config import check_mic_permission, get_agent_state_dir

# Silence rumps debug output during tests
import logging
logging.getLogger("rumps").setLevel(logging.WARNING)


class VoiceClaudeApp(rumps.App):
    """macOS menu bar app for Voice Claude Agent.

    Menu items:
      Start Wake        — begins the continuous wake loop
      Stop Wake         — stops the wake loop
      Trigger Recording — fires a single record → STT → Claude → TTS cycle
      Mic Diagnostic    — shows detailed mic/recording diagnostic info
      Mic Status        — shows current microphone permission
      Quit              — exits the application (stops wake loop first)
    """

    DEFAULT_RECORD_SECONDS = 5
    RECORD_WORKER_GRACE_SECONDS = 3

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

        # F053: VOICE_RECORD_SECONDS env var override
        self.record_seconds = self.DEFAULT_RECORD_SECONDS
        env_val = os.environ.get("VOICE_RECORD_SECONDS", "").strip()
        if env_val:
            try:
                parsed = int(env_val)
                if parsed > 0:
                    self.record_seconds = parsed
            except ValueError:
                pass  # use default

        self._wake_active = False
        self._wake_event = threading.Event()
        self._trigger_event = threading.Event()
        self._wake_thread: threading.Thread | None = None
        self._single_trigger_mode = False
        self._cycle_in_progress = False  # F043: non-reentrant guard
        self._cycle_lock = threading.Lock()

        # F046: last transcript/summary for menu bar inspection
        self._last_transcript: str = ""
        self._last_summary: str = ""

        self.start_item = rumps.MenuItem("Start Wake", callback=self._start_wake)
        self.stop_item = rumps.MenuItem("Stop Wake", callback=self._stop_wake)
        self.trigger_item = rumps.MenuItem(
            "Trigger Recording", callback=self._trigger_recording
        )
        self.diagnostic_item = rumps.MenuItem(
            "Mic Diagnostic", callback=self._run_mic_diagnostic
        )
        self.mic_status_item = rumps.MenuItem(
            "Mic Status: checking...", callback=self._refresh_mic_status_event
        )
        self.transcript_item = rumps.MenuItem(
            "Last Transcript: (none)", callback=self._show_last_transcript
        )
        self.summary_item = rumps.MenuItem(
            "Last Summary: (none)", callback=self._show_last_summary
        )
        self.quit_item = rumps.MenuItem("Quit", callback=self._quit)

        # F052: View Logs
        self.logs_item = rumps.MenuItem("View Logs", callback=self._show_logs)

        self.menu = [
            self.start_item,
            self.stop_item,
            self.trigger_item,
            None,
            self.diagnostic_item,
            self.mic_status_item,
            None,
            self.transcript_item,
            self.summary_item,
            None,
            self.logs_item,
            None,
            self.quit_item,
        ]

        self._update_mic_status()
        self._sync_menu_titles()
        self._validate_stt_backend()

    # ── STT backend validation ───────────────────────────────

    def _validate_stt_backend(self) -> None:
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

    # ── F046: Show last transcript / summary ─────────────────

    def _show_last_transcript(self, sender: rumps.MenuItem) -> None:
        text = self._last_transcript or "(no transcript yet)"
        self._alert(title="Last Transcript", message=text)

    def _show_last_summary(self, sender: rumps.MenuItem) -> None:
        text = self._last_summary or "(no summary yet)"
        self._alert(title="Last Summary", message=text)

    # ── F052: View Logs ─────────────────────────────────────

    def _show_logs(self, sender: rumps.MenuItem) -> None:
        """Show recent app_events and last_result in an alert dialog."""
        import json

        from voice_claude_agent.config import get_agent_state_dir

        lines = ["=== View Logs ===", ""]
        state_dir = get_agent_state_dir()

        # ── app_events ──
        events_path = state_dir / "app_events.jsonl"
        if events_path.exists():
            try:
                raw = events_path.read_text(encoding="utf-8")
                all_events = [json.loads(line) for line in raw.strip().split("\n") if line]
                recent = all_events[-10:]
                lines.append(f"Recent app-events ({len(recent)} of {len(all_events)} total):")
                for ev in recent:
                    timestamp = str(ev.get("timestamp", ""))
                    ts = timestamp.split("T", 1)[1] if "T" in timestamp else timestamp
                    ts = ts.split("+", 1)[0].split("Z", 1)[0][:8]
                    evt = ev.get("event", "?")
                    extra = ""
                    if "elapsed" in ev:
                        extra = f" @{ev['elapsed']}"
                    if "audio_bytes" in ev:
                        extra += f" bytes={ev['audio_bytes']}"
                    lines.append(f"  {ts} {evt}{extra}")
            except Exception as e:
                lines.append(f"Error reading app_events: {e}")
        else:
            lines.append("(no app_events log yet)")

        lines.append("")

        # ── last_result ──
        result_path = state_dir / "last_result.json"
        if result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
                lines.append("Last result:")
                lines.append(f"  prompt: {data.get('prompt', '?')}")
                lines.append(f"  exit_code: {data.get('exit_code', '?')}")
                summary = data.get("summary", data.get("spoken_summary", "?"))
                lines.append(f"  summary: {summary[:200]}")
            except json.JSONDecodeError:
                lines.append("(last_result.json is corrupted — not valid JSON)")
            except Exception as e:
                lines.append(f"Error reading last_result: {e}")
        else:
            lines.append("(no last_result.json yet)")

        self._alert(title="View Logs", message="\n".join(lines))

    # ── Mic Diagnostic ───────────────────────────────────────

    def _run_mic_diagnostic(self, sender: rumps.MenuItem) -> None:
        """Run a full mic/recording diagnostic and show the results in an alert."""
        import platform
        import sys

        lines = ["=== Mic Diagnostic ===", ""]

        # Bundle / app info
        lines.append("Bundle ID: com.voiceclaude.agent")
        lines.append(f"Python: {platform.python_version()}")
        lines.append(f"Agent state dir: {get_agent_state_dir()}")
        lines.append(f"Whisper language: {os.environ.get('WHISPER_CPP_LANGUAGE', 'zh')}")
        lines.append("Python path containing _sounddevice_data:")
        sounddevice_data_paths = [p for p in sys.path if "_sounddevice_data" in p or "python3.14" in p]
        if sounddevice_data_paths:
            lines.extend(f"- {p}" for p in sounddevice_data_paths[:3])
        else:
            lines.append("- not present in sys.path sample")

        # Input device info
        device_name = "unknown"
        portaudio_load_ok = False
        try:
            import _sounddevice_data
            import sounddevice as sd
            default_input = sd.query_devices(kind="input")
            device_name = default_input.get("name", "unknown")
            portaudio_load_ok = True
            data_path = next(iter(_sounddevice_data.__path__), "unknown")
            lines.append(f"_sounddevice_data path: {data_path}")
            lines.append(f"Default input device: {device_name}")
            lines.append(f"Input channels: {default_input.get('max_input_channels', '?')}")
            lines.append(f"Default sample rate: {default_input.get('default_samplerate', '?')} Hz")
        except Exception as e:
            lines.append(f"sounddevice query error: {e}")

        # PortAudio library path
        lines.append("")
        lines.append(f"PortAudio loaded: {'YES' if portaudio_load_ok else 'NO'}")
        try:
            import sounddevice as sd
            lib_path = sd._libname if hasattr(sd, '_libname') else "unknown"
            lines.append(f"PortAudio library: {lib_path}")
        except Exception:
            lines.append("PortAudio library: unable to determine")

        lines.append("")

        # TCC / permission info
        has_mic, detail = check_mic_permission()
        lines.append(f"Mic permission check: {'ACCESSIBLE' if has_mic else 'DENIED'}")
        if detail:
            lines.append(f"  Detail: {detail}")

        lines.append("")
        lines.append("If the .app does not appear in System Settings > Privacy & Security > Microphone:")
        lines.append("- The bundle must contain NSMicrophoneUsageDescription in Info.plist")
        lines.append("- The .app must be code-signed (ad-hoc is sufficient for TCC)")
        lines.append("- The .app must be launched from /Applications, ~/Applications, or via Finder")
        lines.append("- Running directly from dist/ or Terminal may not register TCC")
        lines.append("- Use: 'tccutil reset Microphone com.voiceclaude.agent' to reset permissions")

        self._alert(title="Mic Diagnostic", message="\n".join(lines))

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
        # F048: check and set the non-reentrant guard atomically.
        with self._cycle_lock:
            if self._cycle_in_progress:
                return

            self._update_mic_status()
            if not self._check_mic_or_alert():
                return

            self._cycle_in_progress = True
        if not self._wake_active:
            self._wake_active = True
            self._single_trigger_mode = True
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
            try:
                self._record_and_execute()
            finally:
                with self._cycle_lock:
                    self._cycle_in_progress = False
                if self._single_trigger_mode:
                    self._single_trigger_mode = False
                    self._wake_active = False
                    self._wake_event.set()
                    self._sync_menu_titles()

    # ── Non-interactive recording (NO input()) ───────────────

    def _record_and_execute(self) -> None:
        import time as _time

        from voice_claude_agent.cli import _run_pipeline
        from voice_claude_agent.stt import RecordingTranscriber

        cycle_start = _time.monotonic()
        self._append_runtime_event("trigger")
        try:
            recorder = self._open_mic_or_alert()
            if recorder is None:
                self._append_runtime_event("record_open_failed")
                return

            self.trigger_item.title = "Recording..."
            self._append_runtime_event("record_start", elapsed=f"{_time.monotonic() - cycle_start:.3f}s")
            audio, diag = self._record_with_timeout(recorder)
            self._append_runtime_event(
                "record_stop",
                elapsed=f"{_time.monotonic() - cycle_start:.3f}s",
                audio_bytes=len(audio),
            )
            if not audio:
                self.mic_status_item.title = "Mic: No audio captured"
                self._alert(
                    title="No Audio",
                    message=(
                        "No audio was captured. Check your microphone connection.\n\n"
                        f"{diag}"
                    ),
                )
                return

            self.trigger_item.title = "Transcribing..."
            stt_start = _time.monotonic()
            self._append_runtime_event("stt_start", elapsed=f"{stt_start - cycle_start:.3f}s")
            transcriber = RecordingTranscriber(backend=self.stt_backend)
            transcript = transcriber.transcribe(audio)
            self._append_runtime_event(
                "stt_done",
                elapsed=f"{_time.monotonic() - cycle_start:.3f}s",
                duration=f"{_time.monotonic() - stt_start:.3f}s",
                transcript_preview=transcript[:120],
            )

            if not transcript.strip():
                self.mic_status_item.title = "Mic: Empty transcript"
                self._alert(
                    title="No Speech Detected",
                    message="No speech was detected in the recording.",
                )
                return

            if transcript.startswith("[STT error:"):
                self.mic_status_item.title = "Mic: STT Error"
                self._alert(title="STT Error", message=transcript)
                return

            self.trigger_item.title = "Running Claude..."
            claude_start = _time.monotonic()
            self._append_runtime_event("claude_start", elapsed=f"{claude_start - cycle_start:.3f}s")
            _run_pipeline(transcript, input_mode="voice", tts_fake=False)

            # Capture summary from last_result.json written by _run_pipeline
            import json as _json
            try:
                from voice_claude_agent.config import get_last_result_path
                last_json = get_last_result_path().read_text(encoding="utf-8")
                last_data = _json.loads(last_json)
            except Exception:
                last_data = {}
            self._last_transcript = transcript
            self._last_summary = last_data.get("summary", "")
            self.transcript_item.title = f"Last Transcript: {transcript[:60]}{'...' if len(transcript) > 60 else ''}"
            self.summary_item.title = f"Last Summary: {self._last_summary[:60]}{'...' if len(self._last_summary) > 60 else ''}"
            self._append_runtime_event(
                "claude_done",
                elapsed=f"{_time.monotonic() - cycle_start:.3f}s",
                duration=f"{_time.monotonic() - claude_start:.3f}s",
            )

            # TTS is handled inside _run_pipeline via MacOSSaySpeaker
            self._append_runtime_event("tts_done")

            self.trigger_item.title = "Done ✓"
            self._append_runtime_event(
                "cycle_done",
                total_elapsed=f"{_time.monotonic() - cycle_start:.3f}s",
            )
            threading.Timer(1.5, lambda: setattr(self.trigger_item, "title", "Trigger Recording")).start()
        except Exception as e:
            self.mic_status_item.title = "Mic: Runtime error"
            self._append_runtime_event(f"record_cycle_error {type(e).__name__}: {e}")
            self._alert(title="Recording Runtime Error", message=str(e))
        finally:
            if self.trigger_item.title not in {"Done ✓", "Trigger Recording"}:
                self.trigger_item.title = "Trigger Recording"

    def _record_with_timeout(self, recorder) -> tuple[bytes, str]:
        """Run the recorder path with a hard timeout so the menu app can recover."""
        result: list[tuple[bytes, str]] = []
        error: list[BaseException] = []

        def _target() -> None:
            try:
                result.append(self._record_fixed_duration_with_diag(recorder))
            except BaseException as e:
                error.append(e)

        worker = threading.Thread(target=_target, daemon=True, name="record-worker")
        worker.start()
        timeout = self._record_timeout_seconds()
        worker.join(timeout=timeout)

        if worker.is_alive():
            self._append_runtime_event(f"record_timeout after={timeout:.1f}s")
            self._wake_event.set()
            threading.Thread(
                target=self._best_effort_stop_recorder,
                args=(recorder,),
                daemon=True,
                name="record-stop-cleanup",
            ).start()
            return (
                b"",
                (
                    f"Recording timed out after {timeout:.1f}s and was interrupted.\n"
                    "The menu app recovered, but the audio backend may need an app restart "
                    "if the microphone remains busy."
                ),
            )

        if error:
            raise error[0]
        if result:
            return result[0]
        return b"", "Recording ended without producing a result."

    def _record_timeout_seconds(self) -> float:
        return self.record_seconds + self.RECORD_WORKER_GRACE_SECONDS

    def _best_effort_stop_recorder(self, recorder) -> None:
        try:
            recorder.stop()
            self._append_runtime_event("record_timeout_cleanup_stop_ok")
        except Exception as e:
            self._append_runtime_event(f"record_timeout_cleanup_stop_error {type(e).__name__}: {e}")

    def _append_runtime_event(self, message: str, **fields) -> None:
        try:
            from voice_claude_agent.logging_store import write_app_event

            write_app_event(message, **fields)
        except Exception:
            pass

    def _open_mic_or_alert(self):
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

    def _record_fixed_duration_with_diag(self, recorder) -> tuple[bytes, str]:
        """Record for record_seconds. Returns (audio_bytes, diagnostic_string)."""
        diag_parts = []
        device_name = "unknown"

        try:
            import sounddevice as sd
            dev = sd.query_devices(kind="input")
            device_name = dev.get("name", "unknown")
        except Exception as e:
            device_name = f"query error: {e}"
        diag_parts.append(f"Input device: {device_name}")

        frames_count = 0
        audio_len = 0

        try:
            recorder.start()
        except Exception as e:
            diag_parts.append(f"recorder.start: FAILED ({e})")
            return b"", "\n".join(diag_parts)

        diag_parts.append("recorder.start: OK")

        deadline = time.monotonic() + self.record_seconds
        while time.monotonic() < deadline:
            if self._wake_event.is_set():
                diag_parts.append("recording interrupted by stop event")
                break
            time.sleep(0.1)

        try:
            self._append_runtime_event("recorder_stop")
            recorder.stop()
        except Exception as e:
            diag_parts.append(f"recorder.stop: FAILED ({e})")
            return b"", "\n".join(diag_parts)

        diag_parts.append("recorder.stop: OK")

        if hasattr(recorder, "_frames"):
            frames_count = len(recorder._frames)
        diag_parts.append(f"Frames captured: {frames_count}")

        audio = recorder.get_audio()
        audio_len = len(audio)
        diag_parts.append(f"Audio bytes: {audio_len} ({audio_len / 2 / 16000:.1f}s at 16kHz)")

        if not audio:
            diag_parts.append("RESULT: EMPTY AUDIO")
            diag_parts.append("")
            diag_parts.append("Possible causes:")
            diag_parts.append("- Mic permission denied for bundle. Check System Settings > Privacy & Security > Microphone.")
            diag_parts.append("- Bundle ID is com.voiceclaude.agent — verify this appears in the mic permissions list.")
            diag_parts.append("- If built via py2app, ensure NSMicrophoneUsageDescription is in Info.plist.")
            diag_parts.append("- The .app must be code-signed (ad-hoc is fine).")

        return audio, "\n".join(diag_parts)


def launch_app(stt_backend: str = "text-input") -> None:
    VoiceClaudeApp(stt_backend=stt_backend).run()
