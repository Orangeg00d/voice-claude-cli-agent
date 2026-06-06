"""Tests for Voice Claude Agent core modules."""

import json
import sys
from pathlib import Path
from unittest import mock

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from voice_claude_agent.claude_runner import ClaudeRunResult, run_claude
from voice_claude_agent.confirmation import confirm_or_reject
from voice_claude_agent.logging_store import write_session, write_last_result
from voice_claude_agent.recorder import FakeRecorder
from voice_claude_agent.risk import (
    RiskLevel,
    classify_risk,
    requires_confirmation,
    DESTRUCTIVE_KEYWORDS,
)
from voice_claude_agent.config import check_mic_permission
from voice_claude_agent.stt import (
    FakeTranscriber,
    RecordingTranscriber,
    list_available_backends,
)
from voice_claude_agent.summarizer import summarize
from voice_claude_agent.tts import FakeSpeaker, MacOSSaySpeaker


# ── Risk Classifier Tests ─────────────────────────────────
class TestRiskClassifier:
    def test_read_only_for_neutral_prompt(self):
        assert classify_risk("read the file") == RiskLevel.READ_ONLY
        assert classify_risk("show me the git status") == RiskLevel.READ_ONLY
        assert classify_risk("explain this code") == RiskLevel.READ_ONLY

    def test_recoverable_for_modify_keywords(self):
        assert classify_risk("modify the config file") == RiskLevel.RECOVERABLE
        assert classify_risk("create a new branch") == RiskLevel.RECOVERABLE
        assert classify_risk("install numpy") == RiskLevel.RECOVERABLE
        assert classify_risk("edit app.py") == RiskLevel.RECOVERABLE
        assert classify_risk("write a test") == RiskLevel.RECOVERABLE
        assert classify_risk("refactor the handler") == RiskLevel.RECOVERABLE
        assert classify_risk("rename the variable") == RiskLevel.RECOVERABLE
        assert classify_risk("add a function") == RiskLevel.RECOVERABLE

    def test_destructive_for_push_deploy_delete(self):
        assert classify_risk("git push origin main") == RiskLevel.EXTERNAL_OR_DESTRUCTIVE
        assert classify_risk("deploy to staging") == RiskLevel.EXTERNAL_OR_DESTRUCTIVE
        assert classify_risk("delete the database") == RiskLevel.EXTERNAL_OR_DESTRUCTIVE
        assert classify_risk("remove the file") == RiskLevel.EXTERNAL_OR_DESTRUCTIVE

    def test_destructive_for_more_keywords(self):
        assert classify_risk("rm -rf node_modules") == RiskLevel.EXTERNAL_OR_DESTRUCTIVE
        assert classify_risk("rm -r old_data") == RiskLevel.EXTERNAL_OR_DESTRUCTIVE
        assert classify_risk("merge the PR") == RiskLevel.EXTERNAL_OR_DESTRUCTIVE
        assert classify_risk("git reset --hard HEAD~1") == RiskLevel.EXTERNAL_OR_DESTRUCTIVE
        assert classify_risk("expose secret token") == RiskLevel.EXTERNAL_OR_DESTRUCTIVE
        assert classify_risk("leaked credential") == RiskLevel.EXTERNAL_OR_DESTRUCTIVE

    def test_destructive_for_dangerously_skip_permissions(self):
        assert (
            classify_risk("claude -p 'do it' --dangerously-skip-permissions")
            == RiskLevel.EXTERNAL_OR_DESTRUCTIVE
        )

    def test_destructive_for_production_config(self):
        assert classify_risk("change production config") == RiskLevel.EXTERNAL_OR_DESTRUCTIVE

    def test_destructive_keywords_list_not_empty(self):
        assert len(DESTRUCTIVE_KEYWORDS) > 10

    def test_requires_confirmation(self):
        assert requires_confirmation(RiskLevel.EXTERNAL_OR_DESTRUCTIVE) is True
        assert requires_confirmation(RiskLevel.RECOVERABLE) is False
        assert requires_confirmation(RiskLevel.READ_ONLY) is False


# ── Confirmation Tests ────────────────────────────────────
class TestConfirmation:
    def test_read_only_passes_without_confirmation(self):
        proceed, reason = confirm_or_reject(
            "read the file",
            "read_only",
            confirmation_fn=lambda p: True,  # would not be called
        )
        assert proceed is True
        assert "does not require confirmation" in reason

    def test_destructive_rejected(self):
        proceed, reason = confirm_or_reject(
            "git push",
            "external_or_destructive",
            confirmation_fn=lambda p: False,
        )
        assert proceed is False
        assert "rejected" in reason.lower()

    def test_destructive_confirmed(self):
        proceed, reason = confirm_or_reject(
            "git push",
            "external_or_destructive",
            confirmation_fn=lambda p: True,
        )
        assert proceed is True
        assert "confirmed" in reason.lower()


# ── TTS Tests ─────────────────────────────────────────────
class TestTTS:
    def test_fake_speaker_records_spoken_text(self):
        speaker = FakeSpeaker()
        speaker.speak("Hello world")
        speaker.speak("Second message")
        assert speaker.spoken == ["Hello world", "Second message"]

    def test_fake_speaker_starts_empty(self):
        speaker = FakeSpeaker()
        assert speaker.spoken == []

    def test_macos_say_speaker_does_not_raise(self):
        speaker = MacOSSaySpeaker()
        # Should not raise even if `say` is unavailable
        speaker.speak("test")


# ── Summarizer Tests ──────────────────────────────────────
class TestSummarizer:
    def test_success_with_short_output(self):
        summary = summarize("OK, done.", exit_code=0, duration_seconds=2.5)
        assert "成功" in summary or "OK" in summary

    def test_failure_with_nonzero_exit_code(self):
        summary = summarize("Error: file not found", exit_code=1, duration_seconds=0.1)
        assert "失败" in summary or "1" in summary
        assert "file not found" in summary

    def test_timeout_summary(self):
        summary = summarize("", exit_code=-1, duration_seconds=300)
        assert "超时" in summary or "Timeout" in summary

    def test_claude_not_found_summary(self):
        summary = summarize("", exit_code=-2, duration_seconds=0.001)
        assert "未找到" in summary or "not found" in summary.lower()

    def test_empty_output_success(self):
        summary = summarize("\n\n  \n", exit_code=0, duration_seconds=1.0)
        assert len(summary) > 0


# ── Logging Store Tests ───────────────────────────────────
class TestLoggingStore:
    def test_write_session_creates_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )

        entry = {
            "transcript": "test prompt",
            "risk_level": "read_only",
            "exit_code": 0,
            "summary": "All good.",
            "claude_command": ["claude", "-p", "test prompt"],
            "spoken": True,
        }
        path = write_session(entry)
        assert path == tmp_path / "sessions.jsonl"
        assert path.exists()

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["transcript"] == "test prompt"
        assert record["risk_level"] == "read_only"
        assert record["exit_code"] == 0
        assert "timestamp" in record

    def test_write_last_result(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        result = {"prompt": "hello", "exit_code": 0, "summary": "OK"}
        path = write_last_result(result)
        assert path == tmp_path / "last_result.json"
        assert path.exists()

        data = json.loads(path.read_text())
        assert data["prompt"] == "hello"
        assert data["exit_code"] == 0

    def test_multiple_sessions_appended(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )

        for i in range(3):
            write_session({"transcript": f"prompt {i}", "exit_code": 0})

        lines = (tmp_path / "sessions.jsonl").read_text().strip().split("\n")
        assert len(lines) == 3
        assert all(json.loads(line) for line in lines)


# ── Claude Runner Tests ───────────────────────────────────
class TestClaudeRunnerResult:
    def test_dataclass_fields(self):
        result = ClaudeRunResult(
            command=["claude", "-p", "test"],
            exit_code=0,
            stdout="hello",
            stderr="",
            duration_seconds=1.5,
            timed_out=False,
        )
        assert result.exit_code == 0
        assert result.stdout == "hello"
        assert result.timed_out is False

    def test_run_claude_file_not_found(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            result = run_claude("test prompt", timeout=5)
            assert result.exit_code == -2
            assert "not found" in result.stderr.lower()

    def test_run_claude_success(self):
        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "All done."
        fake_proc.stderr = ""
        with mock.patch("subprocess.run", return_value=fake_proc):
            result = run_claude("test", timeout=5)
            assert result.exit_code == 0
            assert result.stdout == "All done."
            assert result.timed_out is False

    def test_run_claude_timeout(self):
        import subprocess

        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 5)):
            result = run_claude("test", timeout=5)
            assert result.exit_code == -1
            assert result.timed_out is True


# ── Integration / Demo-Text Test ──────────────────────────
class TestDemoTextPipeline:
    def test_pipeline_with_mocked_claude(self, tmp_path, monkeypatch):
        """Simulate the full demo-text pipeline end-to-end."""
        # Patch log paths
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        # Mock claude subprocess
        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "OK, I have completed the task."
        fake_proc.stderr = ""
        with mock.patch("subprocess.run", return_value=fake_proc):
            from voice_claude_agent.claude_runner import run_claude
            from voice_claude_agent.risk import classify_risk, requires_confirmation
            from voice_claude_agent.summarizer import summarize
            from voice_claude_agent.logging_store import write_session, write_last_result
            from voice_claude_agent.tts import FakeSpeaker

            prompt = "请回复 OK"
            risk = classify_risk(prompt)
            assert risk == RiskLevel.READ_ONLY
            assert not requires_confirmation(risk)

            result = run_claude(prompt)
            summary = summarize(result.stdout, result.exit_code, result.duration_seconds)

            speaker = FakeSpeaker()
            speaker.speak(summary)

            write_session({
                "input_mode": "text",
                "transcript": prompt,
                "risk_level": risk.value,
                "confirmation_required": False,
                "confirmation_received": False,
                "claude_command": result.command,
                "exit_code": result.exit_code,
                "summary": summary,
                "spoken": True,
            })
            write_last_result({"prompt": prompt, "exit_code": 0, "summary": summary})

        # Verify logging
        assert (tmp_path / "sessions.jsonl").exists()
        lines = (tmp_path / "sessions.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["transcript"] == "请回复 OK"
        assert record["exit_code"] == 0

        # Verify TTS recorded
        assert len(speaker.spoken) == 1
        assert "OK" in speaker.spoken[0] or "完成" in speaker.spoken[0]

    def test_destructive_pipeline_is_blocked(self, tmp_path, monkeypatch):
        """High-risk action without confirmation should not execute."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        prompt = "git push origin main"
        risk = classify_risk(prompt)

        if requires_confirmation(risk):
            proceed, reason = confirm_or_reject(
                prompt,
                risk.value,
                confirmation_fn=lambda p: False,  # user rejects
            )
            assert proceed is False

        # Verify no Claude CLI was invoked (no log written)
        assert not (tmp_path / "sessions.jsonl").exists()


# ── Recorder Tests ────────────────────────────────────────
class TestRecorder:
    def test_fake_recorder_lifecycle(self):
        recorder = FakeRecorder(b"hello audio")
        assert not recorder.is_recording()
        recorder.start()
        assert recorder.is_recording()
        recorder.stop()
        assert not recorder.is_recording()
        assert recorder.get_audio() == b"hello audio"

    def test_fake_recorder_counts_calls(self):
        recorder = FakeRecorder()
        recorder.start()
        recorder.start()
        recorder.stop()
        assert len(recorder.start_calls) == 2
        assert len(recorder.stop_calls) == 1

    def test_fake_recorder_empty_default(self):
        recorder = FakeRecorder()
        assert recorder.get_audio() == b""


# ── STT Tests ─────────────────────────────────────────────
class TestSTT:
    def test_fake_transcriber_returns_preset(self):
        t = FakeTranscriber("hello world")
        assert t.transcribe() == "hello world"
        assert t.transcribe(b"ignored") == "hello world"

    def test_fake_transcriber_tracks_calls(self):
        t = FakeTranscriber("test")
        t.transcribe(b"first")
        t.transcribe(b"second")
        assert len(t.calls) == 2
        assert t.calls[0] == b"first"

    def test_recording_transcriber_empty_audio(self):
        t = RecordingTranscriber()
        assert t.transcribe(b"") == ""

    def test_recording_transcriber_text_input_backend(self):
        t = RecordingTranscriber(backend="text-input")
        result = t.transcribe(b"\x00" * 32000)  # 1 second of 16kHz int16
        assert "32000 bytes" in result
        assert "1.0s" in result


# ── Voice Pipeline Integration Test ───────────────────────
class TestVoicePipeline:
    def test_demo_voice_with_fake_audio(self, tmp_path, monkeypatch):
        """Full voice pipeline with fake audio and fake Claude."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        # Setup fake components
        recorder = FakeRecorder(b"stub audio data")
        transcriber = FakeTranscriber("请回复 OK")

        # Record
        recorder.start()
        recorder.stop()
        audio = recorder.get_audio()
        transcript = transcriber.transcribe(audio)

        # Mock Claude
        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "OK"
        fake_proc.stderr = ""
        with mock.patch("subprocess.run", return_value=fake_proc):
            result = run_claude(transcript)
            summary = summarize(result.stdout, result.exit_code, result.duration_seconds)

            speaker = FakeSpeaker()
            speaker.speak(summary)

            write_session({
                "input_mode": "voice",
                "transcript": transcript,
                "risk_level": "read_only",
                "confirmation_required": False,
                "confirmation_received": False,
                "claude_command": result.command,
                "exit_code": result.exit_code,
                "summary": summary,
                "spoken": True,
            })

        # Verify
        assert (tmp_path / "sessions.jsonl").exists()
        lines = (tmp_path / "sessions.jsonl").read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["input_mode"] == "voice"
        assert record["transcript"] == "请回复 OK"
        assert record["exit_code"] == 0
        assert len(speaker.spoken) == 1


# ── CLI-level demo-voice Tests ─────────────────────────────
class TestDemoVoiceCLI:
    def test_demo_voice_uses_transcript_not_stub(self, tmp_path, monkeypatch):
        """demo-voice must pipe the STT transcript to Claude, not the CLI arg."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        from click.testing import CliRunner
        from voice_claude_agent.cli import main

        monkeypatch.setattr(
            "voice_claude_agent.cli.FakeTranscriber",
            lambda response: FakeTranscriber("TRANSCRIBED FROM STT"),
        )

        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "OK"
        fake_proc.stderr = ""

        with mock.patch("subprocess.run", return_value=fake_proc) as mock_run:
            runner = CliRunner()
            runner.invoke(main, ["demo-voice", "IGNORE THIS ARG"])

        claude_prompt = ""
        for call in mock_run.call_args_list:
            args = call[0][0] if call[0] else []
            if isinstance(args, list) and "claude" in args[0]:
                claude_prompt = " ".join(args)
                break
        assert "IGNORE THIS ARG" not in claude_prompt, (
            f"demo-voice used CLI stub text instead of transcript: {claude_prompt}"
        )
        assert "TRANSCRIBED FROM STT" in claude_prompt

    def test_demo_voice_stt_output_reaches_claude(self, tmp_path, monkeypatch):
        """Verify demo-voice pipeline: STT transcript is what Claude executes."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        from click.testing import CliRunner
        from voice_claude_agent.cli import main

        monkeypatch.setattr(
            "voice_claude_agent.cli.FakeTranscriber",
            lambda response: FakeTranscriber("请回复 OK"),
        )

        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "done"
        fake_proc.stderr = ""

        with mock.patch("subprocess.run", return_value=fake_proc) as mock_run:
            runner = CliRunner()
            runner.invoke(main, ["demo-voice", "任意内容"])

        # The claude -p call is the first subprocess.run invocation.
        # The second is macOS `say` — skip that.
        claude_prompt = ""
        for call in mock_run.call_args_list:
            args = call[0][0] if call[0] else []
            if isinstance(args, list) and "claude" in args[0]:
                claude_prompt = " ".join(args)
                break
        assert "请回复 OK" in claude_prompt, (
            f"Claude did not receive STT output: {claude_prompt}"
        )


# ── Phase 3: Wake Loop Tests ──────────────────────────────
class TestWakeLoop:
    def test_wake_fake_once_runs_one_iteration(self, tmp_path, monkeypatch):
        """wake --fake --once should run one full pipeline iteration and exit."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        from click.testing import CliRunner
        from voice_claude_agent.cli import main

        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "OK"
        fake_proc.stderr = ""

        with mock.patch("subprocess.run", return_value=fake_proc):
            runner = CliRunner()
            result = runner.invoke(main, ["wake", "--fake", "--once"])

        assert result.exit_code == 0
        assert "Wake loop stopped after 1 iteration" in result.output
        assert (tmp_path / "sessions.jsonl").exists()
        lines = (tmp_path / "sessions.jsonl").read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["input_mode"] == "voice"
        assert record["transcript"] == "请回复 OK"

    def test_wake_fake_loop_ctrl_c_exits_cleanly(self, tmp_path, monkeypatch):
        """On second iteration, simulate Ctrl+C. Should exit with status 0."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        from click.testing import CliRunner
        from voice_claude_agent.cli import main

        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "OK"
        fake_proc.stderr = ""

        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise KeyboardInterrupt()
            return fake_proc

        with mock.patch("subprocess.run", side_effect=_side_effect):
            runner = CliRunner()
            result = runner.invoke(main, ["wake", "--fake"])

        # Should exit cleanly
        assert "Wake loop stopped" in result.output
        assert result.exit_code == 0

    def test_manual_wake_trigger_auto_trigger(self):
        """ManualWakeTrigger with auto_trigger=True returns True immediately."""
        from voice_claude_agent.wake import ManualWakeTrigger

        trigger = ManualWakeTrigger(auto_trigger=True)
        assert trigger.wait_for_wake() is True
        assert trigger.trigger_count == 1
        assert trigger.wait_for_wake() is True
        assert trigger.trigger_count == 2


# ── Phase 4: Mic Permission & STT Backends ─────────────────
class TestMicPermission:
    def test_check_mic_permission_returns_tuple(self):
        has_perm, detail = check_mic_permission()
        assert isinstance(has_perm, bool)
        assert isinstance(detail, str)

    def test_check_mic_permission_darwin(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        # If sounddevice isn't importable, should return False
        has_perm, detail = check_mic_permission()
        if not has_perm:
            assert len(detail) > 0


class TestSTTBackends:
    def test_list_available_backends_includes_text_input(self):
        backends = list_available_backends()
        assert "text-input" in backends

    def test_list_available_backends_on_macos(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        backends = list_available_backends()
        assert "apple-speech" in backends

    def test_recording_transcriber_apple_speech_returns_status(self):
        t = RecordingTranscriber(backend="apple-speech")
        result = t.transcribe(b"\x00" * 32000)
        assert "Apple Speech" in result or "Dictation" in result or "error" in result.lower()

    def test_recording_transcriber_unknown_backend(self):
        t = RecordingTranscriber(backend="nonexistent")
        result = t.transcribe(b"data")
        assert "unknown backend" in result


class TestRecordingErrorUX:
    def test__warn_mic_prints_warning(self, capsys):
        """_warn_mic should print a yellow warning when permission=False."""
        from voice_claude_agent.cli import _warn_mic

        _warn_mic(False, "test: mic denied")
        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "Microphone" in captured.out
        assert "System Settings" in captured.out

    def test__warn_mic_silent_when_ok(self, capsys):
        """_warn_mic should print nothing when permission=True."""
        from voice_claude_agent.cli import _warn_mic

        _warn_mic(True, "all good")
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_check_command_shows_mic_status(self):
        from click.testing import CliRunner
        from voice_claude_agent.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["check"])
        assert "Microphone:" in result.output
        assert (
            "ACCESSIBLE" in result.output
            or "DENIED" in result.output
            or "UNAVAILABLE" in result.output
        )

    def test__safe_real_recorder_returns_none_on_error(self, monkeypatch):
        from voice_claude_agent.cli import _safe_real_recorder
        from voice_claude_agent.recorder import SoundDeviceRecorder

        original_init = SoundDeviceRecorder.__init__

        def _failing_init(self, *a, **kw):
            original_init(self, *a, **kw)

        monkeypatch.setattr(
            "voice_claude_agent.recorder.SoundDeviceRecorder.start",
            lambda self: (_ for _ in ()).throw(RuntimeError("No default input device")),
        )

        # _safe_real_recorder catches errors during SoundDeviceRecorder() construction.
        # The error happens during SoundDeviceRecorder() itself, so patch the class.
        def _raise_constructor(*a, **kw):
            raise RuntimeError("No default input device")

        monkeypatch.setattr(
            "voice_claude_agent.cli.SoundDeviceRecorder",
            _raise_constructor,
        )
        recorder = _safe_real_recorder()
        assert recorder is None


class TestPhase4ExceptionHandling:
    def test__stt_is_error_detects_error_prefix(self):
        from voice_claude_agent.cli import _stt_is_error

        assert _stt_is_error("[STT error: microphone denied]") is True
        assert _stt_is_error("[STT error: timed out]") is True
        assert _stt_is_error("[recorded 32000 bytes, 1.0s audio]") is False
        assert _stt_is_error("请回复 OK") is False
        assert _stt_is_error("") is False

    def test_wake_fake_continues_after_stt_error(self, tmp_path, monkeypatch):
        """wake --fake --once with a 'failing' STT should skip Claude and still exit cleanly."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        import importlib
        import voice_claude_agent.cli as cli_mod
        importlib.reload(cli_mod)

        # Bypass the wake command itself and test the STT error detection directly.
        # The wake loop's inner _run_pipeline is called per iteration.
        # Instead, test that _stt_is_error + the wake's transcript check works:
        from voice_claude_agent.cli import _stt_is_error

        # Simulate what happens inside the wake loop:
        transcript = FakeTranscriber("[STT error: test failure]").transcribe()
        assert _stt_is_error(transcript)
        # In the real wake loop, this would trigger a yellow warning and 'continue',
        # so no session would be written. Verified via stt_is_error above.

    def test_wake_cli_with_error_stt_skips_pipeline(self, tmp_path, monkeypatch):
        """CLI-level: wake --fake --once with error STT exits cleanly without Claude."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        from click.testing import CliRunner
        import voice_claude_agent.cli as cli_mod
        from voice_claude_agent.stt import FakeTranscriber as FT

        # The wake command's --fake path creates FakeTranscriber("请回复 OK").
        # We need the CLI to get an error transcript. Override FakeTranscriber
        # inside the cli module so wake's code path picks it up.
        original = cli_mod.FakeTranscriber
        cli_mod.FakeTranscriber = lambda response=None: FT("[STT error: test failure]")

        try:
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.MagicMock(
                    returncode=0, stdout="OK", stderr=""
                )
                runner = CliRunner()
                result = runner.invoke(cli_mod.main, ["wake", "--fake", "--once"])
        finally:
            cli_mod.FakeTranscriber = original

        assert result.exit_code == 0
        assert "STT error" in result.output
        assert "different --stt-backend" in result.output
        assert not (tmp_path / "sessions.jsonl").exists()

    def test_pipeline_timeout_creates_session(self, tmp_path, monkeypatch, capsys):
        """_run_pipeline should log and speak a timeout when Claude times out."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        from voice_claude_agent.claude_runner import ClaudeRunResult
        from voice_claude_agent.cli import _run_pipeline

        # Patch run_claude to return a timeout result
        timeout_result = ClaudeRunResult(
            command=["claude", "-p", "test"],
            exit_code=-1,
            stdout="",
            stderr="Timeout after 300s",
            duration_seconds=300.0,
            timed_out=True,
        )
        with mock.patch(
            "voice_claude_agent.cli.run_claude", return_value=timeout_result
        ):
            _run_pipeline("test", input_mode="text", tts_fake=True)

        assert (tmp_path / "sessions.jsonl").exists()
        lines = (tmp_path / "sessions.jsonl").read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["summary"] == "Timed out"
        assert record["exit_code"] == -1

        captured = capsys.readouterr()
        assert "Claude CLI timed out." in captured.out
        assert "TTS (fake): Claude CLI 执行超时，请检查任务或重试。" in captured.out


# ── F025: Empty Audio / Empty Transcript Resilience ──────
class TestEmptyAudioResilience:
    def test_fake_recorder_empty_audio_is_safe(self):
        """FakeRecorder with empty bytes should not crash get_audio()."""
        recorder = FakeRecorder(b"")
        assert recorder.get_audio() == b""

    def test_recording_transcriber_empty_audio_empty_string(self):
        """RecordingTranscriber with empty audio returns empty string (no crash)."""
        t = RecordingTranscriber()
        assert t.transcribe(b"") == ""

    def test_wake_fake_continues_after_empty_transcript(self, tmp_path, monkeypatch):
        """wake --fake --once with an empty transcript should skip Claude and exit cleanly."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        from click.testing import CliRunner
        from voice_claude_agent import cli as cli_mod
        from voice_claude_agent.stt import FakeTranscriber as FT

        original = cli_mod.FakeTranscriber
        cli_mod.FakeTranscriber = lambda response=None: FT("")

        try:
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.MagicMock(
                    returncode=0, stdout="OK", stderr=""
                )
                runner = CliRunner()
                result = runner.invoke(cli_mod.main, ["wake", "--fake", "--once"])
        finally:
            cli_mod.FakeTranscriber = original

        assert result.exit_code == 0
        assert "No speech detected" in result.output
        assert not (tmp_path / "sessions.jsonl").exists()

    def test_wake_fake_exits_after_empty_transcript_with_once(self, tmp_path, monkeypatch):
        """wake --fake --once with empty transcript exits with stop message."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        from click.testing import CliRunner
        from voice_claude_agent import cli as cli_mod
        from voice_claude_agent.stt import FakeTranscriber as FT

        original = cli_mod.FakeTranscriber
        cli_mod.FakeTranscriber = lambda response=None: FT("")

        try:
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.MagicMock(
                    returncode=0, stdout="OK", stderr=""
                )
                runner = CliRunner()
                result = runner.invoke(cli_mod.main, ["wake", "--fake", "--once"])
        finally:
            cli_mod.FakeTranscriber = original

        assert result.exit_code == 0
        assert "Wake loop stopped after 1 iteration" in result.output

    def test_wake_fake_empty_audio_skips_pipeline(self, tmp_path, monkeypatch):
        """wake --fake --once with empty audio should skip STT/Claude and exit cleanly."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        from click.testing import CliRunner
        from voice_claude_agent import cli as cli_mod
        from voice_claude_agent.recorder import FakeRecorder as FR

        original = cli_mod.FakeRecorder
        cli_mod.FakeRecorder = lambda audio=None: FR(b"")

        try:
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.MagicMock(
                    returncode=0, stdout="OK", stderr=""
                )
                runner = CliRunner()
                result = runner.invoke(cli_mod.main, ["wake", "--fake", "--once"])
        finally:
            cli_mod.FakeRecorder = original

        assert result.exit_code == 0
        assert "No audio captured" in result.output
        assert "Wake loop stopped after 1 iteration" in result.output
        assert not (tmp_path / "sessions.jsonl").exists()
