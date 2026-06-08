"""Phase 6 — macOS menu bar app.

Wraps the voice agent wake loop in a rumps-based system tray application.
Reuses cli.py's pipeline functions for recording, STT, Claude execution, and TTS.
"""

import threading
import time

import rumps

from voice_claude_agent.config import (
    check_mic_permission,
    get_agent_state_dir,
    get_config_path,
    get_config_value,
    get_claude_workdir,
    load_config,
    mask_credential,
)

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
        stt_backend: str | None = None,
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

        self.stt_backend = stt_backend or get_config_value("VOICE_STT_BACKEND", "text-input")
        self._wake_target = _wake_target or self._run_wake_loop
        self._alert = _alert_patch or self._rumps_alert

        # F053 + F059: record_seconds = env > config.json > default
        self.record_seconds = self.DEFAULT_RECORD_SECONDS
        record_seconds = get_config_value("VOICE_RECORD_SECONDS")
        if record_seconds:
            try:
                parsed = int(record_seconds)
                if parsed > 0:
                    self.record_seconds = parsed
            except ValueError:
                pass

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

        # F058: Health Check
        self.health_item = rumps.MenuItem("Health Check", callback=self._run_health_check)

        # F068: Settings
        self.settings_item = rumps.MenuItem("Settings...", callback=self._show_settings)
        self.reset_settings_item = rumps.MenuItem(
            "Reset Settings", callback=self._show_reset_settings
        )

        self.menu = [
            self.start_item,
            self.stop_item,
            self.trigger_item,
            None,
            self.diagnostic_item,
            self.mic_status_item,
            None,
            self.settings_item,
            self.reset_settings_item,
            None,
            self.transcript_item,
            self.summary_item,
            None,
            self.logs_item,
            self.health_item,
            None,
            self.quit_item,
        ]

        self._update_mic_status()
        self._sync_menu_titles()
        self._validate_stt_backend()

    # ── F068: Settings UI ───────────────────────────────────

    _VALID_BACKENDS = {"text-input", "whisper-cli", "apple-speech", "volcengine-doubao"}

    def _show_settings(self, sender: rumps.MenuItem) -> None:
        """Display a settings dialog chain: view → edit → save."""
        import json

        cfg_path = get_config_path()
        cfg = load_config()

        # Step 1: show current config
        setting_keys = [
            "VOICE_RECORD_SECONDS",
            "VOICE_STT_BACKEND",
            "VOICE_TTS_BACKEND",
            "VOICE_CLAUDE_WORKDIR",
            "WHISPER_CPP_MODEL",
            "WHISPER_CPP_LANGUAGE",
            "VOLCENGINE_ASR_API_KEY",
            "VOLCENGINE_ASR_APP_ID",
            "VOLCENGINE_ASR_ACCESS_TOKEN",
            "VOLCENGINE_ASR_RESOURCE_ID",
            "VOLCENGINE_ASR_CLUSTER",
            "VOLCENGINE_ASR_LANGUAGE",
            "VOLCENGINE_ASR_ENDPOINT",
            "VOLCENGINE_TTS_API_KEY",
            "VOLCENGINE_TTS_RESOURCE_ID",
            "VOLCENGINE_TTS_VOICE_TYPE",
            "VOLCENGINE_TTS_AUDIO_FORMAT",
            "VOLCENGINE_TTS_ENDPOINT",
        ]
        credential_keys = {"VOLCENGINE_ASR_API_KEY", "VOLCENGINE_ASR_ACCESS_TOKEN",
                           "VOLCENGINE_TTS_API_KEY"}
        keep_secret = "<keep existing secret>"

        def _masked(key, val):
            if key in credential_keys and val:
                return mask_credential(val)
            return val

        editable = "\n".join(
            f"{k}={keep_secret if k in credential_keys and cfg.get(k) else cfg.get(k, '')}"
            for k in setting_keys
        )
        current_lines = [f"Config path: {cfg_path}\n"]
        for k in setting_keys:
            current_lines.append(f"{k}: {_masked(k, cfg.get(k, ''))}")
        current_lines.append("\nEdit as KEY=value lines. Blank values remove that key.")
        current = "\n".join(current_lines)
        response = rumps.Window(
            message=current,
            title="Settings — View",
            default_text=editable,
            dimensions=(480, 300),
        ).run()

        if not response.clicked or response.text is None:
            return  # cancelled

        # Step 2: parse input
        raw_text = response.text or ""
        if not raw_text.strip():
            self._alert_on_main(
                title="Settings Validation Error",
                message="No settings were entered. Config was not changed.",
            )
            return

        new_cfg = {}
        errors = []
        recognized_lines = 0
        for line in raw_text.strip().split("\n"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if key not in ("VOICE_RECORD_SECONDS", "VOICE_STT_BACKEND",
                           "VOICE_TTS_BACKEND",
                           "VOICE_CLAUDE_WORKDIR",
                           "WHISPER_CPP_MODEL", "WHISPER_CPP_LANGUAGE",
                           "VOLCENGINE_ASR_API_KEY", "VOLCENGINE_ASR_APP_ID",
                           "VOLCENGINE_ASR_ACCESS_TOKEN",
                           "VOLCENGINE_ASR_RESOURCE_ID", "VOLCENGINE_ASR_CLUSTER",
                           "VOLCENGINE_ASR_LANGUAGE", "VOLCENGINE_ASR_ENDPOINT",
                           "VOLCENGINE_TTS_API_KEY", "VOLCENGINE_TTS_RESOURCE_ID",
                           "VOLCENGINE_TTS_VOICE_TYPE", "VOLCENGINE_TTS_AUDIO_FORMAT",
                           "VOLCENGINE_TTS_ENDPOINT"):
                continue
            recognized_lines += 1

            if key in credential_keys and val == keep_secret and key in cfg:
                continue

            new_cfg[key] = val

        if recognized_lines == 0:
            self._alert_on_main(
                title="Settings Validation Error",
                message="No recognized settings were entered. Config was not changed.",
            )
            return

        # Validate
        if "VOICE_RECORD_SECONDS" in new_cfg:
            if new_cfg["VOICE_RECORD_SECONDS"]:
                try:
                    secs = int(new_cfg["VOICE_RECORD_SECONDS"])
                    if secs <= 0:
                        errors.append("Record seconds must be a positive integer.")
                    else:
                        new_cfg["VOICE_RECORD_SECONDS"] = str(secs)
                except ValueError:
                    errors.append("Record seconds must be an integer.")

        if "VOICE_STT_BACKEND" in new_cfg:
            if new_cfg["VOICE_STT_BACKEND"] and new_cfg["VOICE_STT_BACKEND"] not in self._VALID_BACKENDS:
                errors.append(
                    f"STT backend must be one of: {', '.join(sorted(self._VALID_BACKENDS))}"
                )

        if errors:
            self._alert_on_main(
                title="Settings Validation Error",
                message="\n".join(errors),
            )
            return

        # Step 3: save atomically so a failed write cannot leave a 0-byte config.
        merged = {**cfg, **new_cfg}
        merged = {k: v for k, v in merged.items() if v}
        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = cfg_path.with_name(f"{cfg_path.name}.tmp")
            tmp_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(cfg_path)
        except Exception as e:
            self._alert_on_main(
                title="Settings Save Error",
                message=f"Could not save config: {e}",
            )
            return

        # Step 4: reload into app
        self._reload_from_config(merged)

        self._alert_on_main(
            title="Settings Saved",
            message="Config saved and reloaded. Changes take effect immediately.",
        )

    def _show_reset_settings(self, sender: rumps.MenuItem) -> None:
        """Reset config.json to defaults."""
        cfg_path = get_config_path()
        if cfg_path.exists():
            cfg_path.unlink()
        self._reload_from_config({})
        self._alert_on_main(
            title="Settings Reset",
            message="Config reset to defaults. Restart recommended.",
        )

    def _reload_from_config(self, cfg: dict) -> None:
        """Apply config dict values to the running app instance."""
        # record_seconds
        self.record_seconds = self.DEFAULT_RECORD_SECONDS
        if "VOICE_RECORD_SECONDS" in cfg:
            try:
                v = int(cfg["VOICE_RECORD_SECONDS"])
                if v > 0:
                    self.record_seconds = v
            except (ValueError, TypeError):
                pass
        # stt_backend
        self.stt_backend = "text-input"
        if "VOICE_STT_BACKEND" in cfg and cfg["VOICE_STT_BACKEND"] in self._VALID_BACKENDS:
            self.stt_backend = cfg["VOICE_STT_BACKEND"]

    # ── STT backend validation ───────────────────────────────

    def _validate_stt_backend(self) -> None:
        if self.stt_backend != "whisper-cli":
            return
        from voice_claude_agent.stt import _find_whisper_cpp_binary, _resolve_whisper_model

        binary = _find_whisper_cpp_binary()
        if not binary:
            self.mic_status_item.title = "STT: whisper-cli not installed"
            self._alert_on_main(
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
            self._alert_on_main(
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

    def _alert_on_main(self, title: str, message: str) -> None:
        """Dispatch alert to the main thread. Safe to call from any thread.

        rumps.alert / NSWindow must only be instantiated on the main thread.
        When called from a background thread (e.g. wake loop), this uses
        PyObjC's AppHelper.callAfter to schedule the alert on the Cocoa main loop.
        In tests, _alert_patch is used directly (no rumps involved).
        """
        import threading as _threading
        if _threading.current_thread() is _threading.main_thread():
            self._alert(title=title, message=message)
        else:
            from PyObjCTools import AppHelper

            AppHelper.callAfter(self._alert, title=title, message=message)

    def _check_mic_or_alert(self) -> bool:
        has_mic, detail = check_mic_permission()
        if not has_mic:
            self.mic_status_item.title = "Mic: Denied"
            self._alert_on_main(
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
        self._alert_on_main(title="Last Transcript", message=text)

    def _show_last_summary(self, sender: rumps.MenuItem) -> None:
        text = self._last_summary or "(no summary yet)"
        self._alert_on_main(title="Last Summary", message=text)

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
                    if "reason" in ev:
                        extra += f" reason={ev['reason']}"
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
                if data.get("claude_cwd"):
                    lines.append(f"  claude_cwd: {data.get('claude_cwd')}")
                if data.get("tts_backend"):
                    lines.append(f"  tts_backend: {data.get('tts_backend')}")
                if data.get("tts_duration_seconds") is not None:
                    lines.append(f"  tts_duration_seconds: {data.get('tts_duration_seconds')}")
                if data.get("tts_fallback_used") is not None:
                    lines.append(f"  tts_fallback_used: {data.get('tts_fallback_used')}")
                lines.append(f"  exit_code: {data.get('exit_code', '?')}")
                summary = data.get("summary", data.get("spoken_summary", "?"))
                lines.append(f"  summary: {summary[:200]}")
            except json.JSONDecodeError:
                lines.append("(last_result.json is corrupted — not valid JSON)")
            except Exception as e:
                lines.append(f"Error reading last_result: {e}")
        else:
            lines.append("(no last_result.json yet)")

        self._alert_on_main(title="View Logs", message="\n".join(lines))

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
        lines.append(f"Config path: {get_config_path()}")
        lines.append(f"STT backend: {self.stt_backend}")
        lines.append(f"Claude workdir: {get_claude_workdir()}")
        # TTS backend status
        from voice_claude_agent.tts import _resolve_tts_backend, _check_volcengine_tts_credentials, _mask_tts_credential
        lines.append(f"TTS backend: {_resolve_tts_backend()}")
        tts_creds, _tts_err = _check_volcengine_tts_credentials()
        if tts_creds:
            lines.append("Volcengine TTS: CONFIGURED")
            for tk in ("VOLCENGINE_TTS_API_KEY", "VOLCENGINE_TTS_RESOURCE_ID",
                        "VOLCENGINE_TTS_VOICE_TYPE", "VOLCENGINE_TTS_AUDIO_FORMAT",
                        "VOLCENGINE_TTS_ENDPOINT"):
                lines.append(f"  {tk}: {_mask_tts_credential(tk, tts_creds.get(tk, ''))}")
        else:
            lines.append("Volcengine TTS: not configured")
        lines.append(f"Record duration: {self.record_seconds}s")
        lines.append(f"Whisper model: {get_config_value('WHISPER_CPP_MODEL', '(not set)')}")
        lines.append(f"Whisper language: {get_config_value('WHISPER_CPP_LANGUAGE', 'zh')}")
        # Volcengine ASR status
        from voice_claude_agent.stt import _check_volcengine_credentials, _mask_credential as _ve_mask
        ve_creds, _ve_err = _check_volcengine_credentials()
        if ve_creds:
            lines.append("Volcengine ASR: CONFIGURED")
            for vk in ("VOLCENGINE_ASR_API_KEY", "VOLCENGINE_ASR_APP_ID",
                        "VOLCENGINE_ASR_ACCESS_TOKEN",
                        "VOLCENGINE_ASR_RESOURCE_ID", "VOLCENGINE_ASR_CLUSTER",
                        "VOLCENGINE_ASR_LANGUAGE", "VOLCENGINE_ASR_ENDPOINT"):
                lines.append(f"  {vk}: {_ve_mask(vk, ve_creds.get(vk, ''))}")
        else:
            lines.append("Volcengine ASR: not configured")
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

        self._alert_on_main(title="Mic Diagnostic", message="\n".join(lines))

    # ─
    # ── F058: Health Check ──────────────────────────────────

    @staticmethod
    def _check_item(name, ok, detail="", fix_hint=""):
        status = "PASS" if ok else "FAIL"
        line = f"[{status}] {name}"
        if detail:
            line += f" — {detail}"
        if not ok and fix_hint:
            line += f"\n     → {fix_hint}"
        return line

    def _run_health_check(self, sender):
        from voice_claude_agent.config import (
            check_apple_speech_available, check_mic_permission,
            find_claude_executable, get_agent_state_dir,
        )
        from voice_claude_agent.stt import _resolve_whisper_model, _find_whisper_cpp_binary

        lines = ["=== Health Check ===", ""]
        
        claude = find_claude_executable()
        lines.append(self._check_item("Claude CLI", claude is not None,
            claude or "not found", "Install: brew install claude"))
        lines.append("")
        
        wb = _find_whisper_cpp_binary()
        lines.append(self._check_item("whisper-cli", wb is not None,
            wb or "not found", "Install: brew install whisper-cpp"))
        lines.append("")
        
        m, me = _resolve_whisper_model()
        lines.append(self._check_item("Whisper model", m is not None,
            m or me, "Set WHISPER_CPP_MODEL=/path/to/ggml-base.bin"))
        lines.append("")
        
        hm, md = check_mic_permission()
        lines.append(self._check_item("Microphone", hm, md,
            "Check System Settings > Privacy & Security > Microphone"))
        lines.append("")
        
        po = False
        pd = ""
        try:
            import sounddevice as sd
            sd.query_devices(kind="input")
            po = True
            pd = "loaded"
        except Exception as e:
            pd = str(e)[:100]
        lines.append(self._check_item("PortAudio", po, pd,
            "Rebuild: python setup.py py2app"))
        lines.append("")
        
        sd = get_agent_state_dir().exists()
        lines.append(self._check_item("Agent state dir", sd,
            str(get_agent_state_dir()), "Created on first use"))
        lines.append("")
        
        sk = check_apple_speech_available()
        lines.append(self._check_item("macOS say (TTS)", sk,
            "available" if sk else "not found", ""))
        lines.append("")

        # F062: Record duration
        lines.append(self._check_item(
            "Record duration", True, f"{self.record_seconds}s", ""))
        lines.append("")

        cfg = load_config()
        lines.append(self._check_item(
            "Config file",
            True,
            f"{get_config_path()} ({len(cfg)} setting{'s' if len(cfg) != 1 else ''})",
            "",
        ))
        lines.append(self._check_item("STT backend", True, self.stt_backend, ""))

        # TTS backend health
        from voice_claude_agent.tts import _resolve_tts_backend, _check_volcengine_tts_credentials
        tts_backend = _resolve_tts_backend()
        tts_creds, _tts_err = _check_volcengine_tts_credentials()
        tts_healthy = tts_backend == "macos-say" or bool(tts_creds)
        tts_detail = f"{tts_backend}" + (" (configured)" if tts_healthy else " (API key missing)")
        lines.append(self._check_item("TTS backend", tts_healthy, tts_detail,
            "Set VOLCENGINE_TTS_API_KEY or use VOICE_TTS_BACKEND=macos-say" if not tts_healthy else ""))
        lines.append("")

        claude_workdir = get_claude_workdir()
        claude_workdir_ok = (
            claude_workdir.exists()
            and claude_workdir.is_dir()
            and ".app/Contents/Resources" not in str(claude_workdir)
        )
        lines.append(self._check_item(
            "Claude workdir",
            claude_workdir_ok,
            str(claude_workdir),
            "Set VOICE_CLAUDE_WORKDIR to an existing project directory",
        ))

        # Volcengine ASR health
        from voice_claude_agent.stt import _check_volcengine_credentials
        ve_creds, _ve_err = _check_volcengine_credentials()
        ve_required = self.stt_backend == "volcengine-doubao"
        ve_ok = bool(ve_creds)
        ve_healthy = ve_ok or not ve_required
        ve_detail = "configured" if ve_ok else ("not configured" if ve_required else "not selected")
        lines.append(self._check_item("Volcengine ASR", ve_healthy, ve_detail,
            "Set VOLCENGINE_ASR_API_KEY, or APP_ID + ACCESS_TOKEN" if ve_required and not ve_ok else ""))
        lines.append("")

        all_ok = all([claude, wb, m, hm, po, sd, sk, tts_healthy, claude_workdir_ok, ve_healthy])
        lines.append("Overall: " + ("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED — see hints above"))
        self._alert_on_main(title="Health Check", message="\n".join(lines))

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
                self._alert_on_main(
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
                self._alert_on_main(
                    title="No Speech Detected",
                    message="No speech was detected in the recording.",
                )
                return

            if transcript.startswith("[STT error:"):
                self.mic_status_item.title = "Mic: STT Error"
                self._alert_on_main(
                    title="STT Error",
                    message=(
                        f"{transcript}\n\n"
                        "Try switching STT backend, for example set "
                        "VOICE_STT_BACKEND=text-input for debugging or "
                        "VOICE_STT_BACKEND=whisper-cli after installing whisper.cpp."
                    ),
                )
                return

            # ── F064: Voice confirmation for high-risk actions ──
            from voice_claude_agent.risk import classify_risk, requires_confirmation as _req_conf
            from voice_claude_agent.confirmation import is_voice_confirm

            risk = classify_risk(transcript)
            if _req_conf(risk):
                self._append_runtime_event("risk_high_confirm_start")
                # TTS: ask user to confirm
                from voice_claude_agent.tts import MacOSSaySpeaker
                speaker = MacOSSaySpeaker()
                speaker.speak(f"检测到高风险动作：{transcript[:60]}。请说同意以继续，或说取消以拒绝。")
                self._append_runtime_event("risk_high_confirm_tts")

                # Record confirmation audio
                self.trigger_item.title = "Confirm? Say 同意 or 取消..."
                conf_audio, conf_diag = self._record_with_timeout(recorder)
                self._append_runtime_event(
                    "risk_high_confirm_recorded",
                    audio_bytes=len(conf_audio) if conf_audio else 0,
                )
                if not conf_audio:
                    self.mic_status_item.title = "Mic: No confirmation audio"
                    self._alert_on_main(
                        title="Confirmation Failed",
                        message="No audio captured for confirmation. Action aborted.",
                    )
                    return

                conf_transcriber = RecordingTranscriber(backend=self.stt_backend)
                conf_text = conf_transcriber.transcribe(conf_audio)
                self._append_runtime_event(
                    "risk_high_confirm_stt",
                    transcript_preview=conf_text[:60],
                )

                verdict = is_voice_confirm(conf_text)
                if verdict is True:
                    self._append_runtime_event("risk_high_confirm_accepted")
                elif verdict is False:
                    self._append_runtime_event("risk_high_confirm_rejected")
                    self.mic_status_item.title = "Mic: Action rejected"
                    speaker.speak("高风险动作已被拒绝，未执行。")
                    return
                else:
                    self._append_runtime_event("risk_high_confirm_unclear")
                    self.mic_status_item.title = "Mic: Confirmation unclear"
                    self._alert_on_main(
                        title="Confirmation Unclear",
                        message=(
                            f"Heard: '{conf_text}'\n\n"
                            "Could not determine yes/no. Action aborted for safety."
                        ),
                    )
                    return

            # ── End F064 ────────────────────────────────────────

            self.trigger_item.title = "Running Claude..."
            claude_start = _time.monotonic()
            self._append_runtime_event("claude_start", elapsed=f"{claude_start - cycle_start:.3f}s")
            _run_pipeline(
                transcript,
                input_mode="voice",
                tts_fake=False,
                confirmation_override=True if _req_conf(risk) else None,
            )

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
            self._last_tts_backend = last_data.get("tts_backend", "macos-say")
            self.transcript_item.title = f"Last Transcript: {transcript[:60]}{'...' if len(transcript) > 60 else ''}"
            self.summary_item.title = f"Last Summary: {self._last_summary[:60]}{'...' if len(self._last_summary) > 60 else ''}"
            self._append_runtime_event(
                "claude_done",
                elapsed=f"{_time.monotonic() - cycle_start:.3f}s",
                duration=f"{_time.monotonic() - claude_start:.3f}s",
            )

            # TTS is handled inside _run_pipeline via create_speaker().
            self._append_runtime_event(
                "tts_done",
                tts_backend=last_data.get("tts_backend", "macos-say"),
                tts_duration_seconds=last_data.get("tts_duration_seconds"),
                tts_fallback_used=last_data.get("tts_fallback_used", False),
            )

            self.trigger_item.title = "Done ✓"
            self._append_runtime_event(
                "cycle_done",
                total_elapsed=f"{_time.monotonic() - cycle_start:.3f}s",
            )
            threading.Timer(1.5, lambda: setattr(self.trigger_item, "title", "Trigger Recording")).start()
        except Exception as e:
            self.mic_status_item.title = "Mic: Runtime error"
            self._append_runtime_event(f"record_cycle_error {type(e).__name__}: {e}")
            self._alert_on_main(title="Recording Runtime Error", message=str(e))
        finally:
            if self.trigger_item.title not in {"Done ✓", "Trigger Recording"}:
                self.trigger_item.title = "Trigger Recording"
            self._cycle_in_progress = False  # F061: always clear guard on cycle end

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
            rescued_audio = b""
            rescue_diag = []
            try:
                rescued_audio = recorder.get_audio()
                rescue_diag.append(f"Rescued audio bytes: {len(rescued_audio)}")
            except Exception as e:
                rescue_diag.append(f"Could not rescue audio before cleanup: {e}")
            threading.Thread(
                target=self._best_effort_stop_recorder,
                args=(recorder,),
                daemon=True,
                name="record-stop-cleanup",
            ).start()
            if rescued_audio:
                self._append_runtime_event(
                    "record_timeout_rescued_audio",
                    audio_bytes=len(rescued_audio),
                )
                return (
                    rescued_audio,
                    (
                        f"Recording timed out after {timeout:.1f}s while stopping the microphone.\n"
                        "Captured audio was recovered and processing will continue.\n"
                        + "\n".join(rescue_diag)
                    ),
                )
            return (
                b"",
                (
                    f"Recording timed out after {timeout:.1f}s and was interrupted.\n"
                    "The menu app recovered, but the audio backend may need an app restart "
                    "if the microphone remains busy.\n"
                    + "\n".join(rescue_diag)
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
            self._alert_on_main(
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


def launch_app(stt_backend: str | None = None) -> None:
    VoiceClaudeApp(stt_backend=stt_backend).run()
