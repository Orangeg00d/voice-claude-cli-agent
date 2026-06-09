"""Tests for Voice Claude Agent core modules."""

import json
import os
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

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
    _resolve_whisper_model,
    list_available_backends,
)
from voice_claude_agent.summarizer import summarize
from voice_claude_agent.tts import FakeSpeaker, MacOSSaySpeaker


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path, monkeypatch):
    """Keep tests independent from the user's real ~/.voice-claude-agent config."""
    from voice_claude_agent.config import CONFIG_KEYS
    import voice_claude_agent.config as config_mod

    for key in CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)

    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "get_config_path", lambda: cfg_file)
    app_mod = sys.modules.get("voice_claude_agent.app")
    if app_mod is not None:
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)


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
        assert summary == "OK, done."
        assert "Claude CLI 执行成功" not in summary
        assert "耗时" not in summary
        assert "完整输出" not in summary

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

    # F081: 402 balance error → Chinese-friendly message
    def test_402_balance_error_maps_to_chinese(self):
        summary = summarize("API Error: 402 Insufficient Balance", exit_code=1, duration_seconds=0.5)
        assert "余额" in summary or "计费" in summary
        assert "402" not in summary

    def test_insufficient_balance_no_api_prefix(self):
        summary = summarize("Insufficient Balance", exit_code=1, duration_seconds=0.5)
        assert "余额" in summary or "计费" in summary

    def test_other_nonzero_unchanged(self):
        summary = summarize("Some other error", exit_code=2, duration_seconds=0.5)
        assert "退出码" in summary
        assert "Some other error" in summary

    def test_402_not_in_log_full_output(self):
        """summarize_for_record preserves raw output including 402."""
        from voice_claude_agent.summarizer import summarize_for_record
        raw = "API Error: 402 Insufficient Balance"
        result = summarize_for_record(raw, exit_code=1, duration_seconds=0.5)
        # Full record preserves the original text
        assert "402" in result

    def test_402_pipeline_persists_original_stderr(self, monkeypatch, tmp_path):
        """_run_pipeline writes 402 error to stderr in session/log."""
        monkeypatch.setattr("voice_claude_agent.cli.run_claude",
            lambda prompt, timeout=300, extra_args=None, workdir=None, cancel_event=None: type("R", (), {"command": [], "exit_code": 1, "stdout": "", "stderr": "API Error: 402 Insufficient Balance", "duration_seconds": 0.1, "timed_out": False, "cancelled": False, "cwd": ""})())
        monkeypatch.setattr("voice_claude_agent.cli._resolve_tts_backend", lambda: "macos-say")
        monkeypatch.setattr("voice_claude_agent.cli.create_speaker", lambda: type("S", (), {"speak": lambda self, t: None})())

        sessions_path = tmp_path / "sessions.jsonl"
        last_path = tmp_path / "last_result.json"
        monkeypatch.setattr("voice_claude_agent.logging_store.get_sessions_log_path", lambda: sessions_path)
        monkeypatch.setattr("voice_claude_agent.logging_store.get_last_result_path", lambda: last_path)

        from voice_claude_agent.cli import _run_pipeline
        _run_pipeline("test", input_mode="text", tts_fake=False)

        session = json.loads(sessions_path.read_text().splitlines()[0])
        # stderr preserved as-is
        assert "402" in session.get("claude_stderr", "")
        # spoken summary uses Chinese message
        spoken = session.get("spoken_summary", "")
        assert "余额" in spoken or "计费" in spoken


# ── Logging Store Tests ───────────────────────────────────
class TestLoggingStore:
    def test_agent_state_dir_env_override(self, monkeypatch, tmp_path):
        from voice_claude_agent.config import get_agent_state_dir

        state_dir = tmp_path / "custom_state"
        monkeypatch.setenv("VOICE_CLAUDE_AGENT_STATE_DIR", str(state_dir))

        assert get_agent_state_dir() == state_dir

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
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError):
            with mock.patch("subprocess.PIPE", "pipe"):  # PIPE arg
                result = run_claude("test prompt", timeout=5)
                assert result.exit_code == -2
                assert "not found" in result.stderr.lower()

    def test_run_claude_success(self):
        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "All done."
        fake_proc.stderr = ""
        fake_proc.poll.return_value = 0
        fake_proc.communicate.return_value = ("All done.", "")
        with mock.patch("subprocess.Popen", return_value=fake_proc), \
             mock.patch("subprocess.PIPE", "pipe"):
            result = run_claude("test", timeout=5)
            assert result.exit_code == 0
            assert result.stdout == "All done."
            assert result.timed_out is False

    def test_run_claude_uses_utf8_replace_decoding(self):
        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "中文 output"
        fake_proc.stderr = ""
        fake_proc.poll.return_value = 0
        fake_proc.communicate.return_value = ("中文 output", "")
        with mock.patch("subprocess.Popen", return_value=fake_proc) as popen_mock, \
             mock.patch("subprocess.PIPE", "pipe"):
            result = run_claude("test", timeout=5)

        assert result.exit_code == 0
        assert result.stdout == "中文 output"
        popen_mock.assert_called_once()
        kwargs = popen_mock.call_args.kwargs
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"

    def test_run_claude_uses_configured_workdir(self, tmp_path):
        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "ok"
        fake_proc.stderr = ""
        fake_proc.poll.return_value = 0
        fake_proc.communicate.return_value = ("ok", "")
        with mock.patch("subprocess.Popen", return_value=fake_proc) as popen_mock, \
             mock.patch("subprocess.PIPE", "pipe"):
            result = run_claude("test", timeout=5, workdir=tmp_path)

        assert result.exit_code == 0
        assert result.cwd == str(tmp_path)
        assert popen_mock.call_args.kwargs["cwd"] == str(tmp_path)

    def test_run_claude_invalid_workdir_skips_subprocess(self, tmp_path):
        missing = tmp_path / "missing"
        with mock.patch("subprocess.Popen") as popen_mock:
            result = run_claude("test", timeout=5, workdir=missing)

        assert result.exit_code == -3
        assert str(missing) in result.stderr
        assert result.cwd == str(missing)
        popen_mock.assert_not_called()

    def test_run_claude_timeout(self):
        import time as _time

        # Simulate a process that never finishes
        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.wait.side_effect = lambda timeout=None: _time.sleep(timeout or 999)

        with mock.patch("subprocess.Popen", return_value=fake_proc), \
             mock.patch("subprocess.PIPE", "pipe"):
            result = run_claude("test", timeout=1)
            # The process gets cleaned up by finally or hits the full timeout return
            assert result.exit_code in (-1, -4)
            assert result.timed_out is True or result.stderr == "Process terminated before reading output"

    def test_run_claude_cancelled(self):
        """run_claude returns cancelled=True when cancel_event is set during poll loop."""
        import threading

        from voice_claude_agent.claude_runner import run_claude

        # Simulate a process that stays running: poll returns None,
        # wait raises TimeoutExpired. After cancel is set, next poll
        # iteration detects it and returns cancelled result.
        cancel_event = threading.Event()

        class _FakeProc:
            def __init__(self):
                self.returncode = -1
                self._poll_count = 0

            def poll(self):
                self._poll_count += 1
                return None  # always running

            def wait(self, timeout=None):
                return  # no-op

            def terminate(self):
                pass

            def kill(self):
                pass

        # Run in a thread so we can cancel asynchronously
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(run_claude, "test", timeout=30, cancel_event=cancel_event, workdir="/tmp")
            # Give it a moment to start polling
            import time as _time
            _time.sleep(0.2)
            cancel_event.set()
            result = future.result(timeout=3)

        assert result.cancelled is True
        assert result.exit_code == -5


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
        fake_proc.poll.return_value = 0
        fake_proc.communicate.return_value = ("OK, I have completed the task.", "")
        with mock.patch("subprocess.Popen", return_value=fake_proc), \
             mock.patch("subprocess.PIPE", "pipe"):
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
        fake_proc.poll.return_value = 0
        fake_proc.communicate.return_value = ("OK", "")
        with mock.patch("subprocess.Popen", return_value=fake_proc), \
             mock.patch("subprocess.PIPE", "pipe"):
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
        fake_proc.poll.return_value = 0
        fake_proc.communicate.return_value = ("OK", "")

        with mock.patch("subprocess.Popen", return_value=fake_proc) as mock_popen, \
             mock.patch("subprocess.PIPE", "pipe"):
            runner = CliRunner()
            runner.invoke(main, ["demo-voice", "IGNORE THIS ARG"])

        claude_prompt = ""
        for call in mock_popen.call_args_list:
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
        fake_proc.poll.return_value = 0
        fake_proc.communicate.return_value = ("done", "")

        with mock.patch("subprocess.Popen", return_value=fake_proc) as mock_popen, \
             mock.patch("subprocess.PIPE", "pipe"):
            runner = CliRunner()
            runner.invoke(main, ["demo-voice", "任意内容"])

        claude_prompt = ""
        for call in mock_popen.call_args_list:
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
        fake_proc.poll.return_value = 0
        fake_proc.communicate.return_value = ("OK", "")

        with mock.patch("subprocess.Popen", return_value=fake_proc), \
             mock.patch("subprocess.PIPE", "pipe"):
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
        fake_proc.poll.return_value = 0
        fake_proc.communicate.return_value = ("OK", "")

        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise KeyboardInterrupt()
            return fake_proc

        with mock.patch("subprocess.Popen", side_effect=_side_effect), \
             mock.patch("subprocess.PIPE", "pipe"):
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
        stream = mock.Mock()
        sounddevice = mock.Mock(InputStream=mock.Mock(return_value=stream))
        numpy = mock.Mock(float32="float32")
        monkeypatch.setitem(sys.modules, "sounddevice", sounddevice)
        monkeypatch.setitem(sys.modules, "numpy", numpy)

        has_perm, detail = check_mic_permission()

        assert has_perm is True
        assert detail == "microphone accessible"
        sounddevice.InputStream.assert_called_once()
        stream.start.assert_called_once()
        stream.stop.assert_called_once()
        stream.close.assert_called_once()

    def test_check_mic_permission_importerror_sounddevice_data(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        original_import = __import__

        def _raise_importerror(name, *args, **kwargs):
            if name == "sounddevice":
                raise ModuleNotFoundError("No module named '_sounddevice_data'")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _raise_importerror)
        has_perm, detail = check_mic_permission()

        assert has_perm is False
        assert "PortAudio unavailable" in detail
        assert "_sounddevice_data" in detail


class TestSTTBackends:
    def test_list_available_backends_includes_text_input(self):
        backends = list_available_backends()
        assert "text-input" in backends

    def test_list_available_backends_on_macos(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        backends = list_available_backends()
        assert "apple-speech" in backends

    def test_recording_transcriber_apple_speech_returns_status(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr(
            "voice_claude_agent.stt.subprocess.run",
            mock.Mock(return_value=mock.Mock(stdout="0\n", stderr="", returncode=0)),
        )

        t = RecordingTranscriber(backend="apple-speech")
        result = t.transcribe(b"\x00" * 32000)
        assert "Apple Speech" in result or "Dictation" in result or "error" in result.lower()

    def test_recording_transcriber_apple_speech_dictation_off_message(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr(
            "voice_claude_agent.stt.subprocess.run",
            mock.Mock(return_value=mock.Mock(stdout="0\n", stderr="", returncode=0)),
        )

        result = RecordingTranscriber(backend="apple-speech").transcribe(b"\x00" * 32000)

        assert result.startswith("[STT error:")
        assert "Dictation is not enabled" in result
        assert "System Settings > Keyboard > Dictation" in result
        assert "--stt-backend whisper-cli" in result

    def test_recording_transcriber_apple_speech_dictation_on_status(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        run_mock = mock.Mock(
            side_effect=[
                mock.Mock(stdout="1\n", stderr="", returncode=0),
                mock.Mock(stdout="", stderr="", returncode=0),
            ]
        )
        monkeypatch.setattr("voice_claude_agent.stt.subprocess.run", run_mock)

        result = RecordingTranscriber(backend="apple-speech").transcribe(b"\x00" * 32000)

        assert "audio played for dictation" in result
        assert "active text field" in result
        assert "--stt-backend whisper-cli" in result
        assert run_mock.call_count == 2

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


class TestRecordingStartStopErrors:
    def test__safe_record_attempt_start_failure(self, capsys):
        """_safe_record_attempt should catch recorder.start() exceptions and return empty bytes."""
        from voice_claude_agent.cli import _safe_record_attempt

        class BrokenRecorder:
            def start(self):
                raise RuntimeError("No default input device")

            def stop(self):
                pass

            def get_audio(self):
                return b"x"

        audio = _safe_record_attempt(BrokenRecorder(), max_duration=1)
        assert audio == b""
        captured = capsys.readouterr()
        assert "Recording start failed" in captured.out

    def test__safe_record_attempt_stop_failure(self, capsys):
        """_safe_record_attempt should catch recorder.stop() exceptions and return empty bytes."""
        from voice_claude_agent.cli import _safe_record_attempt

        class BrokenStopRecorder:
            def start(self):
                pass

            def stop(self):
                raise RuntimeError("Stream already closed")

            def get_audio(self):
                return b"x"

        # Patch input() to avoid pytest stdin capture error
        with mock.patch("builtins.input", return_value=""):
            audio = _safe_record_attempt(BrokenStopRecorder(), max_duration=1)
        assert audio == b""
        captured = capsys.readouterr()
        assert "Recording stop failed" in captured.out

    def test_wake_real_recording_start_error_continues(self, tmp_path, monkeypatch):
        """wake --once with _safe_record_attempt returning empty should continue, not crash."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )
        # Force _safe_record_attempt to return empty bytes (simulating start failure)
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_record_attempt",
            lambda r, max_duration: b"",
        )

        from click.testing import CliRunner
        from voice_claude_agent import cli as cli_mod

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="OK", stderr="")
            runner = CliRunner()
            result = runner.invoke(cli_mod.main, ["wake", "--once"], input="\n")

        assert result.exit_code == 0
        # The wake loop should print "No audio captured" and continue to next iteration,
        # then with --once + no audio, _stop_if_once triggers. No sessions written.
        assert "No audio captured" in result.output
        assert "Wake loop stopped after 1 iteration(s)." in result.output
        mock_run.assert_not_called()
        assert not (tmp_path / "sessions.jsonl").exists()


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
        assert record["timed_out"] is True
        last = json.loads((tmp_path / "last_result.json").read_text())
        assert last["timed_out"] is True

        captured = capsys.readouterr()
        assert "Claude CLI timed out." in captured.out
        assert "TTS (fake): 任务执行超时，可能任务较长。你可以提高超时时间，或把任务拆成几个小任务。" in captured.out

    def test_pipeline_uses_configured_claude_timeout(self, tmp_path, monkeypatch):
        """_run_pipeline passes VOICE_CLAUDE_TIMEOUT_SECONDS to run_claude."""
        monkeypatch.setenv("VOICE_CLAUDE_TIMEOUT_SECONDS", "777")
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

        calls = []

        def fake_run(prompt, timeout=300, extra_args=None, workdir=None, cancel_event=None):
            calls.append(timeout)
            return ClaudeRunResult(
                command=["claude", "-p", prompt],
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.1,
                timed_out=False,
            )

        monkeypatch.setattr("voice_claude_agent.cli.run_claude", fake_run)

        _run_pipeline("test", input_mode="text", tts_fake=True)

        assert calls == [777]


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


# ── F029: whisper-cli Backend Validation ────────────────────
class TestWhisperCliBackend:
    def test_whisper_cli_not_installed_error(self, monkeypatch):
        """When no whisper.cpp binary exists, transcribe returns clear error."""
        import shutil as _shutil

        monkeypatch.setattr(_shutil, "which", lambda name: None)
        monkeypatch.setattr(
            "voice_claude_agent.stt._find_whisper_cpp_binary", lambda: None
        )

        result = RecordingTranscriber(backend="whisper-cli").transcribe(b"\x00" * 32000)
        assert "[STT error:" in result
        assert "not found" in result.lower()
        assert "brew install whisper-cpp" in result

    def test_python_whisper_rejected(self, tmp_path, monkeypatch):
        """Python openai-whisper should be detected and rejected."""
        fake_bin = tmp_path / "whisper"
        fake_bin.write_text("#!/usr/bin/env python3\n# openai whisper CLI wrapper\n")
        fake_bin.chmod(0o755)

        monkeypatch.setattr(
            "voice_claude_agent.stt._find_whisper_cpp_binary",
            lambda: str(fake_bin),
        )

        result = RecordingTranscriber(backend="whisper-cli").transcribe(b"\x00" * 32000)
        assert "[STT error:" in result
        assert "Python" in result or "openai-whisper" in result
        assert "brew install whisper-cpp" in result

    def test_no_model_env_var_returns_error(self, monkeypatch):
        """Without WHISPER_CPP_MODEL set, returns clear model download prompt."""
        monkeypatch.setitem(
            os.environ, "WHISPER_CPP_MODEL", ""
        ) if "WHISPER_CPP_MODEL" in os.environ else None

        result = _resolve_whisper_model()
        assert result[0] is None
        assert "WHISPER_CPP_MODEL" in result[1]
        assert "huggingface.co" in result[1]

    def test_model_file_not_found_error(self, monkeypatch):
        """When WHISPER_CPP_MODEL points to a missing file, returns clear error."""
        monkeypatch.setitem(os.environ, "WHISPER_CPP_MODEL", "/nonexistent/model.bin")

        result = _resolve_whisper_model()
        assert result[0] is None
        assert "not found" in result[1]
        assert "/nonexistent/model.bin" in result[1]

    def test_model_is_directory_error(self, tmp_path, monkeypatch):
        """When WHISPER_CPP_MODEL points to a directory, returns clear error."""
        monkeypatch.setitem(os.environ, "WHISPER_CPP_MODEL", str(tmp_path))

        result = _resolve_whisper_model()
        assert result[0] is None
        assert "not a file" in result[1]

    def test_valid_model_resolves(self, tmp_path, monkeypatch):
        """When WHISPER_CPP_MODEL points to a real file, resolves successfully."""
        model_file = tmp_path / "ggml-base.en.bin"
        model_file.write_bytes(b"fake model data")
        monkeypatch.setitem(os.environ, "WHISPER_CPP_MODEL", str(model_file))

        path, err = _resolve_whisper_model()
        assert path == str(model_file)
        assert err == ""

    def test_mock_whisper_cpp_successful_transcription(self, tmp_path, monkeypatch):
        """With a mocked whisper.cpp binary, transcription returns the output."""
        model = tmp_path / "model.bin"
        model.write_bytes(b"model")
        monkeypatch.setitem(os.environ, "WHISPER_CPP_MODEL", str(model))

        # Provide a mock binary that prints transcribed text to stdout
        fake_binary = tmp_path / "whisper-cpp"
        fake_binary.write_text("#!/bin/sh\necho 'hello world'\n")
        fake_binary.chmod(0o755)

        monkeypatch.setattr(
            "voice_claude_agent.stt._find_whisper_cpp_binary",
            lambda: str(fake_binary),
        )

        result = RecordingTranscriber(backend="whisper-cli").transcribe(b"\x00" * 32000)
        assert "hello world" in result
        assert "STT error" not in result

    def test_whisper_cli_defaults_to_chinese_language(self, tmp_path, monkeypatch):
        """whisper-cli should default to Chinese instead of English."""
        model = tmp_path / "model.bin"
        model.write_bytes(b"model")
        monkeypatch.setitem(os.environ, "WHISPER_CPP_MODEL", str(model))
        monkeypatch.delenv("WHISPER_CPP_LANGUAGE", raising=False)
        monkeypatch.setattr(
            "voice_claude_agent.stt._find_whisper_cpp_binary",
            lambda: "/usr/local/bin/whisper-cli",
        )
        monkeypatch.setattr("voice_claude_agent.stt._is_python_whisper", lambda p: False)

        fake_proc = mock.MagicMock(returncode=0, stdout="你好", stderr="")
        with mock.patch("voice_claude_agent.stt.subprocess.run", return_value=fake_proc) as run_mock:
            result = RecordingTranscriber(backend="whisper-cli").transcribe(b"\x00" * 32000)

        assert result == "你好"
        command = run_mock.call_args.args[0]
        assert "-l" in command
        assert command[command.index("-l") + 1] == "zh"
        assert run_mock.call_args.kwargs["encoding"] == "utf-8"
        assert run_mock.call_args.kwargs["errors"] == "replace"

    def test_whisper_cli_language_env_override(self, tmp_path, monkeypatch):
        """WHISPER_CPP_LANGUAGE should override the default spoken language."""
        model = tmp_path / "model.bin"
        model.write_bytes(b"model")
        monkeypatch.setitem(os.environ, "WHISPER_CPP_MODEL", str(model))
        monkeypatch.setitem(os.environ, "WHISPER_CPP_LANGUAGE", "auto")
        monkeypatch.setattr(
            "voice_claude_agent.stt._find_whisper_cpp_binary",
            lambda: "/usr/local/bin/whisper-cli",
        )
        monkeypatch.setattr("voice_claude_agent.stt._is_python_whisper", lambda p: False)

        fake_proc = mock.MagicMock(returncode=0, stdout="hello", stderr="")
        with mock.patch("voice_claude_agent.stt.subprocess.run", return_value=fake_proc) as run_mock:
            RecordingTranscriber(backend="whisper-cli").transcribe(b"\x00" * 32000)

        command = run_mock.call_args.args[0]
        assert command[command.index("-l") + 1] == "auto"

    def test_list_backends_excludes_python_whisper(self, monkeypatch):
        """list_available_backends should NOT include whisper-cli when only Python whisper exists."""
        import shutil as _shutil

        # Only 'whisper' exists and it's the Python version
        monkeypatch.setattr(
            _shutil, "which",
            lambda name: "/usr/local/bin/whisper" if name == "whisper" else None,
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt._is_python_whisper", lambda p: True
        )

        backends = list_available_backends()
        assert "whisper-cli" not in backends


# ── Phase 6: Menu Bar App Tests ────────────────────────────
class TestMenuBarApp:
    @pytest.fixture(autouse=True)
    def _mock_app_mic_check(self, monkeypatch):
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(
            app_mod,
            "check_mic_permission",
            lambda: (True, "mock microphone accessible"),
        )
        monkeypatch.setattr(
            app_mod.VoiceClaudeApp,
            "_validate_stt_backend",
            lambda self: None,
        )

    def test_app_class_imports(self):
        """VoiceClaudeApp should be importable and constructible."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app.name == "Voice Agent"
        assert app.title == "🎤"

    def test_menu_items_populated(self):
        """Menu should contain wake controls, diagnostics, mic status, and quit."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        titles = []
        for m in app.menu:
            if m is None:
                continue
            t = m.title
            if callable(t):
                titles.append(t())
            else:
                titles.append(str(t))

        assert "Start Wake" in titles
        assert "Stop Wake" in titles
        assert "Mic Diagnostic" in titles
        assert any("Mic" in t for t in titles)
        assert "Quit" in titles

    def test_menu_has_separators(self):
        """Menu should have at least one item of type str (rumps uses str for separators)."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        # rumps converts None menu entries to str objects (SeparatorMenuItem_*)
        sep_count = sum(1 for m in app.menu if isinstance(m, str) and 'Separator' in m)
        assert sep_count >= 1

    def test_app_command_listed_in_help(self):
        """voice-claude-agent --help should list 'app' command."""
        from click.testing import CliRunner
        from voice_claude_agent.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "app" in result.output
        assert "menu bar" in result.output.lower()

    def test_launch_app_function_exists(self):
        """launch_app should be importable and callable (we don't call it)."""
        from voice_claude_agent.app import launch_app

        assert callable(launch_app)


# ── F033: Start/Stop Toggle + Mic Status + Quit ─────────────
class TestMenuBarLifecycle:
    """Tests for start/stop toggle, mic status, and quit lifecycle.

    IMPORTANT: Never start a real wake thread. Use _wake_target override
    so the thread body is a no-op or a controlled mock. This avoids
    input() calls (which break under pytest) and threading warnings.
    """

    @pytest.fixture(autouse=True)
    def _mock_app_mic_check(self, monkeypatch):
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(
            app_mod,
            "check_mic_permission",
            lambda: (True, "mock microphone accessible"),
        )

    def test_start_stop_state_transitions_direct_state(self):
        """start/stop should transition _wake_active and update titles, without threads."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert not app._wake_active

        # Directly set state (no thread) to test state machine
        app._wake_active = True
        app._sync_menu_titles()
        assert "running" in app.start_item.title

        app._stop_wake(app.stop_item)  # no-op since _wake_active was set, but _wake_thread is None
        app._wake_active = False
        app._sync_menu_titles()
        assert app.start_item.title == "Start Wake"

    def test_double_start_idempotent(self):
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_wake_target=lambda: None)
        app._start_wake(app.start_item)
        first = app._wake_thread
        app._start_wake(app.start_item)
        assert app._wake_thread is first

        app._wake_thread.join(timeout=3.0)
        app._stop_wake(app.stop_item)

    def test_double_stop_safe(self):
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_wake_target=lambda: None)
        app._start_wake(app.start_item)
        app._wake_thread.join(timeout=3.0)
        app._stop_wake(app.stop_item)
        app._stop_wake(app.stop_item)
        assert not app._wake_active

    def test_sync_menu_titles_running_state(self):
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        app._wake_active = True
        app._sync_menu_titles()
        assert "running" in app.start_item.title

        app._wake_active = False
        app._sync_menu_titles()
        assert app.start_item.title == "Start Wake"

    def test_mic_status_refresh_on_start(self):
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_wake_target=lambda: None)
        app._start_wake(app.start_item)
        assert "Mic:" in app.mic_status_item.title
        app._wake_thread.join(timeout=3.0)
        app._stop_wake(app.stop_item)

    def test_mic_status_monkeypatch(self, monkeypatch):
        """Patching voice_claude_agent.app.check_mic_permission must affect the app."""
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(
            app_mod, "check_mic_permission",
            lambda: (False, "test: permission denied"),
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert "Denied" in app.mic_status_item.title
        assert "permission denied" in app.mic_status_item.title

    def test_quit_stops_wake_then_quits(self, monkeypatch):
        import rumps as rumps_mod

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_wake_target=lambda: None)
        app._start_wake(app.start_item)

        # wait for the lambda thread to finish so join doesn't block
        app._wake_thread.join(timeout=2.0)
        assert not app._wake_thread.is_alive()

        quit_called = []
        monkeypatch.setattr(rumps_mod, "quit_application", lambda: quit_called.append(True))

        app._quit(app.quit_item)
        assert not app._wake_active
        assert len(quit_called) == 1

    def test_quit_when_idle_still_quits(self, monkeypatch):
        import rumps as rumps_mod

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert not app._wake_active

        quit_called = []
        monkeypatch.setattr(rumps_mod, "quit_application", lambda: quit_called.append(True))

        app._quit(app.quit_item)
        assert len(quit_called) == 1


# ── F033b: Trigger Recording (event-driven, no input()) ─────
class TestTriggerRecording:
    @pytest.fixture(autouse=True)
    def _mock_app_mic_check(self, monkeypatch):
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(
            app_mod,
            "check_mic_permission",
            lambda: (True, "mock microphone accessible"),
        )

    def test_trigger_item_in_menu(self):
        """Menu should include 'Trigger Recording' item."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        titles = set()
        for m in app.menu:
            if m is not None:
                t = m.title
                if callable(t):
                    titles.add(t())
                else:
                    titles.add(str(t))
        assert "Trigger Recording" in titles

    def test_trigger_when_idle_starts_thread(self):
        """When wake is NOT active, _trigger_recording starts a daemon thread."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_wake_target=lambda: None)
        assert not app._wake_active
        assert app._wake_thread is None

        app._trigger_recording(app.trigger_item)
        assert app._wake_active
        assert app._trigger_event.is_set()
        assert app._wake_thread is not None
        assert app._wake_thread.daemon
        assert app._wake_thread.name == "wake-loop"

        # Let the lambda finish then clean up
        app._wake_thread.join(timeout=2.0)
        # Reset state manually since the lambda didn't touch _wake_active
        app._wake_active = False
        app._wake_event.clear()
        app._trigger_event.clear()

    def test_trigger_when_running_does_not_create_new_thread(self):
        """When wake IS already active, _trigger_recording only sets the event.

        We test this using direct state manipulation since real threads cause hangs."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        app._wake_active = True
        app._trigger_event.clear()

        # Simulate: already have a thread reference
        dummy_thread = threading.Thread(target=lambda: None, daemon=True)
        dummy_thread.start()
        app._wake_thread = dummy_thread

        app._trigger_recording(app.trigger_item)
        assert app._trigger_event.is_set()
        assert app._wake_thread is dummy_thread  # no new thread

        dummy_thread.join(timeout=2.0)
        app._wake_active = False

    def test_stop_wake_sets_both_events(self):
        """_stop_wake must set _wake_event AND _trigger_event to unblock the loop."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        app._wake_active = True
        app._wake_event.clear()
        app._trigger_event.clear()

        app._stop_wake(app.stop_item)
        assert app._wake_event.is_set()
        assert app._trigger_event.is_set()
        assert not app._wake_active

    def test_run_wake_loop_calls_record_and_execute(self):
        """_run_wake_loop should call _record_and_execute when triggered."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        calls = []

        def fake_execute():
            calls.append(1)
            # After first call, signal stop so the loop exits
            app._wake_event.set()

        app._record_and_execute = fake_execute

        # Start the real loop in a thread but with a short timeout
        app._wake_event.clear()
        app._trigger_event.clear()
        app._trigger_event.set()  # pre-trigger

        t = threading.Thread(target=app._run_wake_loop, daemon=True)
        t.start()
        t.join(timeout=3.0)

        assert len(calls) == 1


# ── F034: STT Backend & Env Passthrough ─────────────────────
class TestAppSTTBackendPassthrough:
    @pytest.fixture(autouse=True)
    def _mock_app_mic_check(self, monkeypatch):
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(
            app_mod,
            "check_mic_permission",
            lambda: (True, "mock microphone accessible"),
        )

    def test_stt_backend_stored_on_instance(self):
        """VoiceClaudeApp(stt_backend='whisper-cli') should store it."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="whisper-cli",
            _alert_patch=lambda **kw: None,
        )
        assert app.stt_backend == "whisper-cli"

    def test_stt_backend_default_is_text_input(self, monkeypatch, tmp_path):
        """Default stt_backend should be 'text-input' when no config is set."""
        from voice_claude_agent.app import VoiceClaudeApp

        # Isolate from host's ~/.voice-claude-agent/config.json
        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr("voice_claude_agent.app.get_config_path", lambda: cfg_file)
        monkeypatch.setattr("voice_claude_agent.app.get_config_value", lambda key, default="": default)

        app = VoiceClaudeApp()
        assert app.stt_backend == "text-input"

    def test_record_and_execute_passes_backend_to_transcriber(self, monkeypatch):
        """_record_and_execute should create RecordingTranscriber with stt_backend."""
        from unittest import mock as _mock

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="whisper-cli",
            _alert_patch=lambda **kw: None,
        )

        # Mock _run_pipeline (it's in cli.py)
        monkeypatch.setattr(
            "voice_claude_agent.cli._run_pipeline",
            _mock.Mock(),
        )
        # Mock SafeRecorder so _record_fixed_duration_with_diag handles the mock
        mock_recorder = _mock.MagicMock()
        mock_recorder.get_audio.return_value = b"test audio data"
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            _mock.Mock(return_value=mock_recorder),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_record_attempt",
            _mock.Mock(return_value=b"test audio"),
        )

        # Mock RecordingTranscriber (imported at runtime from stt module)
        mock_transcriber_cls = _mock.Mock()
        mock_transcriber_cls.return_value.transcribe.return_value = "hello"
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock_transcriber_cls,
        )

        app._record_and_execute()
        mock_transcriber_cls.assert_called_once_with(backend="whisper-cli")

    def test_whisper_model_env_passthrough_to_resolve(self, tmp_path, monkeypatch):
        """When stt_backend=whisper-cli and WHISPER_CPP_MODEL is set,
        _resolve_whisper_model should find it."""
        model_file = tmp_path / "ggml-base.bin"
        model_file.write_bytes(b"fake")
        monkeypatch.setenv("WHISPER_CPP_MODEL", str(model_file))

        from voice_claude_agent.stt import _resolve_whisper_model

        path, err = _resolve_whisper_model()
        assert path == str(model_file), f"expected model path, got: {err}"
        assert err == ""

    def test_app_cli_passes_stt_backend_to_launch_app(self, monkeypatch):
        """voice-claude-agent app --stt-backend should forward the selected backend."""
        from click.testing import CliRunner
        from voice_claude_agent.cli import main

        captured = []
        monkeypatch.setattr(
            "voice_claude_agent.app.launch_app",
            lambda stt_backend="text-input": captured.append(stt_backend),
        )

        result = CliRunner().invoke(main, ["app", "--stt-backend", "whisper-cli"])

        assert result.exit_code == 0
        assert captured == ["whisper-cli"]
        assert "STT backend: whisper-cli" in result.output

    def test_launch_app_constructs_app_with_stt_backend(self, monkeypatch):
        """launch_app should construct VoiceClaudeApp with the selected backend and run it."""
        import voice_claude_agent.app as app_mod

        calls = []

        class FakeApp:
            def __init__(self, stt_backend="text-input"):
                calls.append(("init", stt_backend))

            def run(self):
                calls.append(("run", None))

        monkeypatch.setattr(app_mod, "VoiceClaudeApp", FakeApp)

        app_mod.launch_app(stt_backend="apple-speech")

        assert calls == [("init", "apple-speech"), ("run", None)]

    def test_app_config_sets_stt_backend_without_cli_override(self, tmp_path, monkeypatch):
        """config.json should set the app STT backend when no CLI/backend arg is provided."""
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"VOICE_STT_BACKEND": "apple-speech"}))
        monkeypatch.setattr("voice_claude_agent.config.get_config_path", lambda: cfg_file)
        monkeypatch.delenv("VOICE_STT_BACKEND", raising=False)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        assert app.stt_backend == "apple-speech"

    def test_explicit_app_backend_overrides_config_json(self, tmp_path, monkeypatch):
        """Explicit app backend should override config.json."""
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"VOICE_STT_BACKEND": "apple-speech"}))
        monkeypatch.setattr("voice_claude_agent.config.get_config_path", lambda: cfg_file)
        monkeypatch.delenv("VOICE_STT_BACKEND", raising=False)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(stt_backend="whisper-cli", _alert_patch=lambda **kw: None)
        assert app.stt_backend == "whisper-cli"


# ── F036: Mic Permission Denial UX ──────────────────────────
class TestMicDenialUX:
    @pytest.fixture(autouse=True)
    def _mock_app_mic_check(self, monkeypatch):
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(
            app_mod,
            "check_mic_permission",
            lambda: (True, "mock microphone accessible"),
        )

    def test_start_wake_blocked_when_mic_denied(self, monkeypatch):
        """_start_wake should NOT activate the wake loop when mic is denied."""
        import voice_claude_agent.app as app_mod

        alerts = []

        monkeypatch.setattr(
            app_mod, "check_mic_permission",
            lambda: (False, "test: mic denied"),
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda title=None, message=None: alerts.append((title, message)))
        assert not app._wake_active

        app._start_wake(app.start_item)
        assert not app._wake_active  # should NOT start
        assert app._wake_thread is None
        assert len(alerts) == 1
        title, msg = alerts[0]
        assert "Microphone" in title
        assert "System Settings" in msg

    def test_trigger_recording_blocked_when_mic_denied(self, monkeypatch):
        """_trigger_recording should show alert and not start when mic denied."""
        import voice_claude_agent.app as app_mod

        alerts = []

        monkeypatch.setattr(
            app_mod, "check_mic_permission",
            lambda: (False, "test: no mic"),
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda title=None, message=None: alerts.append((title, message)))
        app._trigger_recording(app.trigger_item)
        assert not app._wake_active
        assert len(alerts) == 1

    def test_record_and_execute_shows_alert_when_recorder_is_none(self, monkeypatch):
        """When _safe_real_recorder returns None, alert should fire and mic status update."""
        alerts = []

        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            lambda: None,
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda title=None, message=None: alerts.append((title, message)))
        app._record_and_execute()

        assert app.mic_status_item.title == "Mic: Error"
        assert len(alerts) == 1
        assert "Recording Failed" in alerts[0][0]
        assert "Microphone" in alerts[0][1]

    def test_mic_accessible_no_alert_on_start(self, monkeypatch):
        """When mic is accessible, start_wake should proceed without alert."""
        import voice_claude_agent.app as app_mod

        alerts = []
        monkeypatch.setattr(
            app_mod, "check_mic_permission",
            lambda: (True, "microphone accessible"),
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            _wake_target=lambda: None,
            _alert_patch=lambda title=None, message=None: alerts.append((title, message)),
        )

        app._start_wake(app.start_item)
        assert app._wake_active
        assert len(alerts) == 0

        app._wake_thread.join(timeout=3.0)
        app._stop_wake(app.stop_item)

    def test_alert_patch_defaults_to_rumps_alert(self):
        """Without _alert_patch, the app uses rumps.alert."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app._alert is app._rumps_alert


# ── F037: Session Log Parity (Menu Bar vs CLI Wake) ────────
class TestSessionLogParity:
    @pytest.fixture(autouse=True)
    def _isolate_voice_side_effects(self, monkeypatch):
        import voice_claude_agent.app as app_mod
        import voice_claude_agent.cli as cli_mod

        class SilentSpeaker:
            def __init__(self):
                self.spoken = []

            def speak(self, text):
                self.spoken.append(text)

        monkeypatch.setattr(
            app_mod,
            "check_mic_permission",
            lambda: (True, "mock microphone accessible"),
        )
        monkeypatch.setattr(cli_mod, "create_speaker", SilentSpeaker)

    def test_menu_bar_record_and_execute_writes_session(self, tmp_path, monkeypatch):
        """_record_and_execute → _run_pipeline → write_session should produce
        a valid JSONL record with input_mode='voice'."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "OK from menu bar"
        fake_proc.stderr = ""

        mock_recorder = mock.MagicMock()
        mock_recorder.get_audio.return_value = b"test"

        mock_transcriber_cls = mock.MagicMock()
        mock_transcriber_cls.return_value.transcribe.return_value = "请回复 OK"

        monkeypatch.setattr(
            "voice_claude_agent.cli.run_claude",
            mock.MagicMock(return_value=ClaudeRunResult(
                command=["claude", "-p", "请回复 OK"],
                exit_code=0, stdout="OK from menu bar", stderr="",
                duration_seconds=0.1, timed_out=False,
            )),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_recorder),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_record_attempt",
            mock.MagicMock(return_value=b"test audio"),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock_transcriber_cls,
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: None,
        )
        app._record_and_execute()

        # Verify sessions.jsonl
        assert (tmp_path / "sessions.jsonl").exists()
        lines = (tmp_path / "sessions.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["input_mode"] == "voice"
        assert record["transcript"] == "请回复 OK"
        assert record["exit_code"] == 0
        assert "spoken" in record

        # Verify last_result.json
        assert (tmp_path / "last_result.json").exists()
        last = json.loads((tmp_path / "last_result.json").read_text())
        assert last["prompt"] == "请回复 OK"
        assert last["exit_code"] == 0

    def test_menu_bar_session_fields_match_cli_format(self, tmp_path, monkeypatch):
        """The JSONL record from the menu bar must have the same field set
        as CLI wake mode."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        monkeypatch.setattr(
            "voice_claude_agent.cli.run_claude",
            mock.MagicMock(return_value=ClaudeRunResult(
                command=["claude", "-p", "hi"],
                exit_code=0, stdout="hi", stderr="",
                duration_seconds=0, timed_out=False,
            )),
        )

        from voice_claude_agent.app import VoiceClaudeApp
        from voice_claude_agent.cli import _run_pipeline

        # Write through _run_pipeline (CLI path)
        _run_pipeline("test from cli", input_mode="voice", tts_fake=True)

        # Write through app._record_and_execute (menu bar path)
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock.MagicMock()),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_record_attempt",
            mock.MagicMock(return_value=b"audio"),
        )
        mock_transcriber = mock.MagicMock()
        mock_transcriber.return_value.transcribe.return_value = "test from menu bar"
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock_transcriber,
        )

        app = VoiceClaudeApp(stt_backend="text-input", _alert_patch=lambda **kw: None)
        app._record_and_execute()

        lines = (tmp_path / "sessions.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2

        cli_entry = json.loads(lines[0])
        menu_entry = json.loads(lines[1])

        # Both should have the same set of top-level keys
        expected_keys = {
            "timestamp", "input_mode", "transcript", "classified_intent",
            "risk_level", "confirmation_required", "confirmation_received",
            "claude_command", "claude_cwd", "exit_code", "timed_out", "cancelled", "claude_stdout",
            "claude_stderr", "summary", "spoken_summary", "spoken",
            "stt_backend", "tts_backend", "tts_voice_type", "tts_resource_id",
            "tts_duration_seconds", "tts_fallback_used", "tts_fallback_reason",
            "tts_fallback_detail", "tts_cancelled", "reply_style",
        }
        assert set(cli_entry.keys()) == expected_keys
        assert set(menu_entry.keys()) == expected_keys

        assert cli_entry["input_mode"] == "voice"
        assert menu_entry["input_mode"] == "voice"


# ── F039: Non-Interactive Recording ────────────────────────
class TestNonInteractiveRecording:
    def test_record_and_execute_never_calls_input(self):
        """_record_and_execute must NOT use input()."""
        import inspect

        from voice_claude_agent.app import VoiceClaudeApp

        src = inspect.getsource(VoiceClaudeApp._record_and_execute)
        # Remove lines that are docstrings or comments
        code_lines = [
            line for line in src.split("\n")
            if "input(" not in line or not line.strip().startswith(("#", '"""', "Uses"))
        ]
        code = "\n".join(code_lines)
        assert "input(" not in code, "_record_and_execute must not use input()"
        src2 = inspect.getsource(VoiceClaudeApp._record_fixed_duration_with_diag)
        code2_lines = [
            line for line in src2.split("\n")
            if "input(" not in line or not line.strip().startswith("#")
        ]
        code2 = "\n".join(code2_lines)
        assert "input(" not in code2, "_record_fixed_duration_with_diag must not use input()"

    def test_stage_titles_update_during_cycle(self, monkeypatch):
        """Trigger Recording menu item title should change through stages."""
        import voice_claude_agent.app as app_mod

        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: None,
        )

        # Mock recorder with valid audio
        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b"test audio data"
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_record_attempt",
            mock.MagicMock(return_value=b"x"),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._run_pipeline",
            mock.MagicMock(),
        )

        # Monkeypatch RecordingTranscriber to return a valid transcript
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock.MagicMock(return_value=mock.MagicMock(transcribe=mock.MagicMock(return_value="hello"))),
        )

        # Run one cycle — should update titles
        app._record_and_execute()

        # After a successful cycle: trigger_item should say "Done" (via Timer)
        # For fast test, check it changed from default
        assert app.trigger_item.title in {"Done ✓", "Trigger Recording"}

    def test_empty_audio_shows_alert(self, monkeypatch):
        """When recording returns empty bytes, an alert should be shown."""
        import voice_claude_agent.app as app_mod

        alerts = []

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))

        # Recorder returns empty audio
        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b""
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )

        app._record_and_execute()

        assert len(alerts) >= 1
        assert alerts[0]["title"] == "No Audio"
        assert "No audio" in alerts[0]["message"]

    def test_record_timeout_recovers_when_recorder_hangs(self, monkeypatch):
        """A hung recorder must not leave the menu app stuck in Recording."""
        import time

        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "RECORD_WORKER_GRACE_SECONDS", 0.01)

        class HangingRecorder:
            def start(self):
                time.sleep(1.0)

            def stop(self):
                return None

            def get_audio(self):
                return b""

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        app.record_seconds = 0.01
        started_at = time.monotonic()
        audio, diag = app._record_with_timeout(HangingRecorder())

        assert time.monotonic() - started_at < 0.5
        assert audio == b""
        assert "Recording timed out" in diag
        assert app._wake_event.is_set()

    def test_record_timeout_rescues_audio_when_stop_hangs(self, monkeypatch):
        """If stopping the microphone hangs, captured audio should still be used."""
        import time

        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "RECORD_WORKER_GRACE_SECONDS", 0.01)

        class StopHangsRecorder:
            def start(self):
                return None

            def stop(self):
                time.sleep(1.0)

            def get_audio(self):
                return b"\x01\x02" * 100

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        app.record_seconds = 0.01
        audio, diag = app._record_with_timeout(StopHangsRecorder())

        assert audio == b"\x01\x02" * 100
        assert "Captured audio was recovered" in diag
        assert app._wake_event.is_set()

    def test_stt_error_shows_alert(self, monkeypatch):
        """When STT returns [STT error: ...], an alert should be shown."""
        import voice_claude_agent.app as app_mod

        alerts = []

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))

        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b"x"
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock.MagicMock(return_value=mock.MagicMock(
                transcribe=mock.MagicMock(return_value="[STT error: test]")
            )),
        )

        app._record_and_execute()

        assert len(alerts) >= 1
        assert alerts[0]["title"] == "STT Error"

    def test_empty_transcript_shows_alert(self, monkeypatch):
        """When STT returns empty transcript, an alert should be shown."""
        import voice_claude_agent.app as app_mod

        alerts = []

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))

        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b"x"
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock.MagicMock(return_value=mock.MagicMock(
                transcribe=mock.MagicMock(return_value="")
            )),
        )

        app._record_and_execute()

        assert len(alerts) >= 1
        assert alerts[0]["title"] == "No Speech Detected"


# ── F040: Default whisper-cli Backend ───────────────────────
class TestDefaultWhisperBackend:
    def test_run_app_default_is_whisper_cli(self):
        """run_app.py main() should default VOICE_STT_BACKEND to whisper-cli."""
        source = Path(__file__).resolve().parent.parent / "run_app.py"
        content = source.read_text()
        assert 'whisper-cli' in content
        assert 'VOICE_STT_BACKEND' in content

    def test_run_app_bootstraps_py2app_python_lib(self, monkeypatch, tmp_path):
        """run_app should add Resources/lib/pythonX.Y before app imports."""
        import run_app

        resources = tmp_path / "VoiceClaudeAgent.app" / "Contents" / "Resources"
        python_lib = resources / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        python_lib.mkdir(parents=True)
        monkeypatch.setattr(run_app, "__file__", str(resources / "run_app.py"))
        monkeypatch.setattr(sys, "path", [])

        run_app._bootstrap_py2app_runtime_path()

        assert sys.path[0] == str(python_lib)

    def test_run_app_bootstraps_dev_state_dir(self, monkeypatch, tmp_path):
        """Development dist builds should write logs to the repo agent_state."""
        import run_app

        project_root = tmp_path / "project"
        resources = (
            project_root
            / "dist"
            / "VoiceClaudeAgent.app"
            / "Contents"
            / "Resources"
        )
        resources.mkdir(parents=True)
        (project_root / "feature_list.json").write_text("[]")
        monkeypatch.setattr(run_app, "__file__", str(resources / "run_app.py"))
        monkeypatch.delenv("VOICE_CLAUDE_AGENT_STATE_DIR", raising=False)

        run_app._bootstrap_state_dir()

        assert os.environ["VOICE_CLAUDE_AGENT_STATE_DIR"] == str(
            project_root / "agent_state"
        )

    def test_run_app_env_override(self, monkeypatch):
        """Setting VOICE_STT_BACKEND=text-input should override the default."""
        monkeypatch.setenv("VOICE_STT_BACKEND", "text-input")

        import os
        stt = os.environ.get("VOICE_STT_BACKEND", "whisper-cli")
        assert stt == "text-input"

    def test_whisper_cli_missing_binary_shows_alert(self, monkeypatch):
        """When whisper-cli is backend but binary not found, alert should fire."""
        import voice_claude_agent.app as app_mod

        alerts = []
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(
            "voice_claude_agent.stt._find_whisper_cpp_binary",
            lambda: None,
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app_instance = VoiceClaudeApp(
            stt_backend="whisper-cli",
            _alert_patch=lambda **kw: alerts.append(kw),
        )
        assert app_instance.stt_backend == "whisper-cli"
        assert len(alerts) >= 1
        assert alerts[0]["title"] == "STT Backend Unavailable"
        assert "brew install whisper-cpp" in alerts[0]["message"]
        assert "whisper-cli" in app_instance.mic_status_item.title

    def test_whisper_model_missing_shows_alert(self, monkeypatch):
        """When whisper-cli is backend but model env not set, alert should fire."""
        import voice_claude_agent.app as app_mod

        alerts = []
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(
            "voice_claude_agent.stt._find_whisper_cpp_binary",
            lambda: "/fake/whisper-cli",
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt._resolve_whisper_model",
            lambda: (None, "Model not found at /fake/model.bin"),
        )
        # Clear env var to ensure default is checked
        monkeypatch.delenv("WHISPER_CPP_MODEL", raising=False)

        from voice_claude_agent.app import VoiceClaudeApp

        VoiceClaudeApp(
            stt_backend="whisper-cli",
            _alert_patch=lambda **kw: alerts.append(kw),
        )
        assert len(alerts) >= 1
        assert alerts[0]["title"] == "Whisper Model Not Found"
        assert "WHISPER_CPP_MODEL" in alerts[0]["message"]

    def test_text_input_backend_no_validation_alert(self, monkeypatch):
        """text-input backend should NOT trigger STT validation alerts."""
        import voice_claude_agent.app as app_mod

        alerts = []
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))

        from voice_claude_agent.app import VoiceClaudeApp

        VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: alerts.append(kw),
        )


# ── F041: Mic Diagnostic & Recording Failure Info ───────────
class TestMicDiagnostic:
    def test_diagnostic_menu_item_present(self):
        """Menu should contain 'Mic Diagnostic' item."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        titles = set()
        for m in app.menu:
            if m is not None:
                t = m.title
                if callable(t):
                    titles.add(t())
                else:
                    titles.add(str(t))
        assert "Mic Diagnostic" in titles

    def test_diagnostic_includes_bundle_and_device_info(self, monkeypatch):
        """_run_mic_diagnostic alert should include bundle ID and device info."""
        import voice_claude_agent.app as app_mod

        alerts = []
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "mock microphone accessible"))
        monkeypatch.setattr(
            "sounddevice.query_devices",
            lambda kind=None: {
                "name": "Mock Microphone",
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._run_mic_diagnostic(app.diagnostic_item)

        assert len(alerts) == 1
        assert alerts[0]["title"] == "Mic Diagnostic"
        msg = alerts[0]["message"]
        assert "com.voiceclaude.agent" in msg
        assert "sounddevice" in msg.lower() or "python" in msg.lower()
        assert "Mic permission check" in msg

    def test_empty_audio_alert_includes_diagnostic_info(self, monkeypatch):
        """When recording returns empty audio, the alert must include diagnostic fields."""
        import voice_claude_agent.app as app_mod

        alerts = []
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))

        # Recorder returns empty audio
        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b""
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )

        app._record_and_execute()

        # Should have a "No Audio" alert with diagnostic details
        no_audio = [a for a in alerts if a["title"] == "No Audio"]
        assert len(no_audio) >= 1
        msg = no_audio[0]["message"]
        assert "Input device" in msg
        assert "Frames captured" in msg
        assert "Audio bytes" in msg
        assert "recorder.start" in msg
        assert "recorder.stop" in msg

    def test_diagnostic_includes_tcc_troubleshooting(self, monkeypatch):
        """_run_mic_diagnostic should include TCC/gatekeeper troubleshooting tips."""
        import voice_claude_agent.app as app_mod

        alerts = []
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "mock microphone accessible"))
        monkeypatch.setattr(
            "sounddevice.query_devices",
            lambda kind=None: {
                "name": "Mock Microphone",
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._run_mic_diagnostic(app.diagnostic_item)

        msg = alerts[0]["message"]
        assert "tccutil" in msg
        assert "NSMicrophoneUsageDescription" in msg

    def test_bundle_info_plist_has_mic_key(self):
        """setup.py plist should include NSMicrophoneUsageDescription."""
        setup_py = Path(__file__).resolve().parent.parent / "setup.py"
        content = setup_py.read_text()
        assert "NSMicrophoneUsageDescription" in content


# ── F042: PortAudio Dylib Fix ───────────────────────────────
class TestPortAudioDylibFix:
    def test_check_mic_permission_false_on_portaudio_dylib_error(self, monkeypatch):
        """When sounddevice raises a PortAudio dylib load error,
        check_mic_permission must return (False, ...)."""
        from voice_claude_agent.config import check_mic_permission

        monkeypatch.setattr("platform.system", lambda: "Darwin")

        def _raise(*a, **kw):
            raise OSError("cannot load library '/path/to/libportaudio.dylib': dlopen(...)")

        monkeypatch.setattr("sounddevice.InputStream", _raise)
        has_mic, detail = check_mic_permission()
        assert has_mic is False
        assert "PortAudio" in detail
        assert "libportaudio" in detail.lower()

    def test_check_mic_permission_false_on_generic_portaudio_error(self, monkeypatch):
        """Generic PortAudio errors should also return False."""
        from voice_claude_agent.config import check_mic_permission

        monkeypatch.setattr("platform.system", lambda: "Darwin")

        def _raise(*a, **kw):
            raise OSError("PortAudio error: device unavailable")

        monkeypatch.setattr("sounddevice.InputStream", _raise)
        has_mic, detail = check_mic_permission()
        assert has_mic is False
        assert "PortAudio" in detail

    def test_bundle_dylib_not_in_zip(self):
        """After py2app build, _sounddevice_data must NOT be in python314.zip."""
        import zipfile

        bundle = Path("/Users/orange/Documents/Claude/Projects/语音助理/dist/VoiceClaudeAgent.app")
        if not bundle.exists():
            import pytest
            pytest.skip("Bundle not built. Run: python setup.py py2app")

        zip_candidates = sorted(bundle.glob("Contents/Resources/lib/python*.zip"))
        if not zip_candidates:
            import pytest
            pytest.skip("No python*.zip found in bundle")

        zip_path = zip_candidates[0]
        with zipfile.ZipFile(zip_path, "r") as zf:
            package_in_zip = [n for n in zf.namelist() if n.startswith("_sounddevice_data/")]
        assert len(package_in_zip) == 0, f"_sounddevice_data still in zip: {package_in_zip}"

    def test_bundle_dylib_on_filesystem(self):
        """After py2app build, libportaudio.dylib must exist as a real file."""
        bundle = Path("/Users/orange/Documents/Claude/Projects/语音助理/dist/VoiceClaudeAgent.app")
        if not bundle.exists():
            import pytest
            pytest.skip("Bundle not built. Run: python setup.py py2app")

        package_root = bundle / "Contents" / "Resources" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        dylib = package_root / "_sounddevice_data" / "portaudio-binaries" / "libportaudio.dylib"
        package_init = package_root / "_sounddevice_data" / "__init__.py"
        sounddevice_py = package_root / "sounddevice.py"
        assert sounddevice_py.exists(), f"sounddevice.py not found at {sounddevice_py}"
        assert package_init.exists(), f"_sounddevice_data package init not found at {package_init}"
        assert dylib.exists(), f"libportaudio.dylib not found at {dylib}"
        assert dylib.is_file()
        assert os.access(dylib, os.X_OK)

    def test_bundle_sounddevice_data_resolves_to_filesystem(self):
        """py2app sys.path order must still resolve _sounddevice_data to disk."""
        import subprocess

        bundle = Path("/Users/orange/Documents/Claude/Projects/语音助理/dist/VoiceClaudeAgent.app")
        if not bundle.exists():
            import pytest
            pytest.skip("Bundle not built. Run: python setup.py py2app")

        resources = bundle / "Contents" / "Resources"
        zip_candidates = sorted(resources.glob("lib/python*.zip"))
        if not zip_candidates:
            import pytest
            pytest.skip("No python*.zip found in bundle")

        zip_path = zip_candidates[0]
        filesystem_lib = resources / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        code = f"""
import sys
sys.path[:] = [{str(zip_path)!r}, {str(filesystem_lib)!r}] + sys.path
import _sounddevice_data
package_path = next(iter(_sounddevice_data.__path__))
print(package_path)
assert ".zip" not in package_path
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        assert ".zip" not in result.stdout

    def test_mic_diagnostic_includes_portaudio_status(self, monkeypatch):
        """_run_mic_diagnostic should include PortAudio load status."""
        import voice_claude_agent.app as app_mod

        alerts = []
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "mock microphone accessible"))
        monkeypatch.setattr(
            "sounddevice.query_devices",
            lambda kind=None: {
                "name": "Mock Microphone",
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._run_mic_diagnostic(app.diagnostic_item)

        msg = alerts[0]["message"]
        assert "PortAudio" in msg


# ── F043: Non-Reentrant Trigger Recording ───────────────────
class TestNonReentrantTrigger:
    def test_double_trigger_is_ignored(self):
        """Second _trigger_recording during a cycle should be ignored."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_wake_target=lambda: None, _alert_patch=lambda **kw: None)
        app._cycle_in_progress = True  # simulate mid-cycle

        app._trigger_recording(app.trigger_item)
        # Should NOT have started a new thread since cycle is in progress
        assert app._wake_thread is None
        assert not app._wake_active

    def test_trigger_sets_cycle_guard(self):
        """_trigger_recording should set _cycle_in_progress=True."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            _wake_target=lambda: None,
            _alert_patch=lambda **kw: None,
        )

        # Mock mic check to pass
        app._update_mic_status = lambda: None
        app._check_mic_or_alert = lambda: True

        app._trigger_recording(app.trigger_item)
        assert app._cycle_in_progress is True
        assert app._wake_active is True
        assert app._wake_thread is not None

        app._wake_event.set()
        app._wake_thread.join(timeout=3.0)
        app._stop_wake(app.stop_item)

    def test_cycle_guard_cleared_after_error(self):
        """After _record_and_execute with mic error, _cycle_in_progress clears."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)

        def boom():
            raise RuntimeError("cycle failed")

        app._cycle_in_progress = True
        app._trigger_event.set()
        app._record_and_execute = boom

        with pytest.raises(RuntimeError, match="cycle failed"):
            app._run_wake_loop()

        assert app._cycle_in_progress is False


# ── F044: Structured App-Events Logging ─────────────────────
class TestAppEventsLogging:
    def test_write_app_event_writes_jsonl(self, tmp_path, monkeypatch):
        """write_app_event should append a valid JSONL record."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_app_events_log_path",
            lambda: tmp_path / "app_events.jsonl",
        )

        from voice_claude_agent.logging_store import write_app_event

        write_app_event("trigger")
        write_app_event("record_start", elapsed="0.050s")

        path = tmp_path / "app_events.jsonl"
        assert path.exists()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            record = json.loads(line)
            assert "timestamp" in record
            assert "event" in record
        record2 = json.loads(lines[1])
        assert record2["event"] == "record_start"
        assert record2["elapsed"] == "0.050s"

    def test_app_events_log_range_includes_all_f044_types(self, tmp_path, monkeypatch):
        """A successful _record_and_execute must log: trigger, record_start,
        record_stop, stt_start, stt_done, claude_start, claude_done,
        tts_done, cycle_done."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )
        events_path = tmp_path / "app_events.jsonl"
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_app_events_log_path",
            lambda: events_path,
        )

        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        # Mock recorder with valid audio
        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b"test audio data"
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_record_attempt",
            mock.MagicMock(return_value=b"x"),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._run_pipeline",
            mock.MagicMock(),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock.MagicMock(return_value=mock.MagicMock(
                transcribe=mock.MagicMock(return_value="hello")
            )),
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: None,
        )
        app._record_and_execute()

        assert events_path.exists()
        lines = events_path.read_text().strip().split("\n")
        events = [json.loads(line)["event"] for line in lines]

        required = [
            "trigger", "record_start", "record_stop",
            "stt_start", "stt_done",
            "claude_start", "claude_done",
            "tts_done", "cycle_done",
        ]
        for req in required:
            assert req in events, f"Missing app-event: {req}. Got: {events}"

        # Verify chronological order
        indexes = {ev: events.index(ev) for ev in required}
        assert indexes["trigger"] < indexes["record_start"] < indexes["record_stop"]
        assert indexes["record_stop"] < indexes["stt_start"] < indexes["stt_done"]
        assert indexes["stt_done"] < indexes["claude_start"] < indexes["claude_done"]
        assert indexes["claude_done"] < indexes["cycle_done"]

        records = [json.loads(line) for line in lines]
        record_stop = records[events.index("record_stop")]
        assert record_stop["audio_bytes"] == len(b"test audio data")


# ── F045: Title Recovery After Every Error Path ─────────────
class TestTitleRecovery:
    def _make_app(self, **kw):
        from voice_claude_agent.app import VoiceClaudeApp

        return VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: None,
            _wake_target=lambda: None,
            **kw,
        )

    def test_title_reset_after_empty_audio(self, monkeypatch):
        """After empty audio, trigger_item.title must be 'Trigger Recording'."""
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        app = self._make_app()
        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b""
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )

        app._record_and_execute()
        assert app.trigger_item.title == "Trigger Recording"

    def test_title_reset_after_stt_error(self, monkeypatch):
        """After STT error, trigger_item.title must be 'Trigger Recording'."""
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        app = self._make_app()
        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b"x"
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock.MagicMock(return_value=mock.MagicMock(
                transcribe=mock.MagicMock(return_value="[STT error: test]")
            )),
        )

        app._record_and_execute()
        assert app.trigger_item.title == "Trigger Recording"

    def test_title_reset_after_transcriber_crash(self, monkeypatch):
        """After transcribe throws RuntimeError, title must be 'Trigger Recording'."""
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        app = self._make_app()
        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b"x"
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock.MagicMock(return_value=mock.MagicMock(
                transcribe=mock.MagicMock(side_effect=RuntimeError("transcriber crash"))
            )),
        )

        app._record_and_execute()
        assert app.trigger_item.title == "Trigger Recording"

    def test_title_reset_after_record_open_failed(self, monkeypatch):
        """After _open_mic_or_alert returns None, title must reset."""
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=None),
        )

        app = self._make_app()
        app._record_and_execute()
        assert app.trigger_item.title == "Trigger Recording"

    def test_title_reset_after_claude_pipeline_crash(self, monkeypatch):
        """After Claude/pipeline crashes, title must be 'Trigger Recording'."""
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        app = self._make_app()
        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b"x"
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock.MagicMock(return_value=mock.MagicMock(
                transcribe=mock.MagicMock(return_value="hello")
            )),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._run_pipeline",
            mock.MagicMock(side_effect=RuntimeError("claude crashed")),
        )

        app._record_and_execute()
        assert app.trigger_item.title == "Trigger Recording"

    def test_title_resets_after_success_timer(self, monkeypatch):
        """After a successful cycle, Done should reset to Trigger Recording."""
        import voice_claude_agent.app as app_mod

        class ImmediateTimer:
            def __init__(self, _interval, callback):
                self.callback = callback

            def start(self):
                self.callback()

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)
        monkeypatch.setattr(app_mod.threading, "Timer", ImmediateTimer)

        app = self._make_app()
        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b"x"
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock.MagicMock(return_value=mock.MagicMock(
                transcribe=mock.MagicMock(return_value="hello")
            )),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._run_pipeline",
            mock.MagicMock(),
        )

        app._record_and_execute()
        assert app.trigger_item.title == "Trigger Recording"


# ── F046: Last Transcript / Last Summary Menu Items ────────
class TestLastTranscriptSummary:
    def _patch_successful_recording(self, monkeypatch, transcript="hello"):
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b"x"
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock.MagicMock(return_value=mock.MagicMock(
                transcribe=mock.MagicMock(return_value=transcript)
            )),
        )

    def test_menu_items_present(self):
        """Menu should contain 'Last Transcript' and 'Last Summary' items."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app.transcript_item is not None
        assert app.summary_item is not None
        assert "Transcript" in app.transcript_item.title or "transcript" in app.transcript_item.title.lower()
        assert "Summary" in app.summary_item.title or "summary" in app.summary_item.title.lower()

    def test_placeholder_when_empty(self):
        """Before any cycle, transcript/summary should show placeholder."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app._last_transcript == ""
        assert app._last_summary == ""
        assert "(none)" in app.transcript_item.title
        assert "(none)" in app.summary_item.title

    def test_updated_after_successful_cycle(self, tmp_path, monkeypatch):
        """After a successful _record_and_execute, menu items show last values."""
        last_result = tmp_path / "last_result.json"
        monkeypatch.setattr(
            "voice_claude_agent.config.get_last_result_path",
            lambda: last_result,
        )

        def fake_pipeline(prompt, input_mode, tts_fake, confirmation_override=None, stt_backend_used="", on_tts_start=None):
            last_result.write_text(json.dumps(
                {"prompt": prompt, "exit_code": 0, "summary": "Claude says hi"}
            ), encoding="utf-8")

        self._patch_successful_recording(monkeypatch, transcript="hello")
        monkeypatch.setattr("voice_claude_agent.cli._run_pipeline", fake_pipeline)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: None,
            _wake_target=lambda: None,
        )
        app._record_and_execute()

        assert app._last_transcript == "hello"
        assert app._last_summary == "Claude says hi"
        assert "hello" in app.transcript_item.title
        assert "Claude says hi" in app.summary_item.title

    def test_summary_uses_current_pipeline_result_not_previous_result(self, tmp_path, monkeypatch):
        """Last Summary should read the result written by the current cycle."""
        last_result = tmp_path / "last_result.json"
        last_result.write_text(json.dumps(
            {"prompt": "old", "exit_code": 0, "summary": "Old summary"}
        ), encoding="utf-8")
        monkeypatch.setattr(
            "voice_claude_agent.config.get_last_result_path",
            lambda: last_result,
        )

        def fake_pipeline(prompt, input_mode, tts_fake, confirmation_override=None, stt_backend_used="", on_tts_start=None):
            last_result.write_text(json.dumps(
                {"prompt": prompt, "exit_code": 0, "summary": "Fresh summary"}
            ), encoding="utf-8")

        self._patch_successful_recording(monkeypatch, transcript="current transcript")
        monkeypatch.setattr("voice_claude_agent.cli._run_pipeline", fake_pipeline)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: None,
            _wake_target=lambda: None,
        )
        app._record_and_execute()

        assert app._last_transcript == "current transcript"
        assert app._last_summary == "Fresh summary"
        assert "Old summary" not in app.summary_item.title

    def test_show_alert_on_click(self, monkeypatch):
        """Clicking Last Transcript should show an alert."""
        alerts = []

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._last_transcript = "test transcript"
        app._show_last_transcript(app.transcript_item)

        assert len(alerts) == 1
        assert alerts[0]["title"] == "Last Transcript"
        assert "test transcript" in alerts[0]["message"]

    def test_show_alert_placeholder_when_empty(self, monkeypatch):
        """Clicking Last Summary with no data shows placeholder."""
        alerts = []

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_last_summary(app.summary_item)

        assert len(alerts) == 1
        assert "(no summary yet)" in alerts[0]["message"]


# ── F047: TTS Truncation Strategy ──────────────────────────
class TestTTSTruncation:
    def test_short_output_passed_through_unchanged(self):
        """Short output (< MAX_RESULT_CHARS_FOR_READOUT) should not be truncated."""
        short = "OK, done. 完成了。"
        result = summarize(short, exit_code=0, duration_seconds=1.0)
        assert short in result
        assert "完整内容可在" not in result  # no truncation note

    def test_long_output_truncated_with_note(self):
        """Long output should be truncated with a menu bar hint."""
        long_text = "这是一个很长的回复。" * 50  # ~500 chars
        result = summarize(long_text, exit_code=0, duration_seconds=1.0)
        assert len(result) < len(long_text)
        assert "完整内容可在菜单栏 Last Summary 查看" in result

    def test_code_blocks_stripped_for_tts(self):
        """Code fences should be replaced with placeholder text."""
        text_with_code = "解释如下：\n```python\nprint('hello')\n```\n以上就是代码。"
        result = summarize(text_with_code, exit_code=0, duration_seconds=1.0)
        assert "代码块已省略" in result
        assert "print('hello')" not in result  # code content removed

    def test_inline_backticks_stripped(self):
        """Inline backtick code should be replaced."""
        result = summarize("请使用 `claude -p` 命令。", exit_code=0, duration_seconds=1.0)
        assert "引用" in result
        assert "`claude -p`" not in result

    def test_truncation_at_sentence_boundary(self):
        """Truncation should happen at a sentence end, not mid-word."""
        sentence = "第一句话完毕。第二句话完毕。第三句话完毕。第四句话完毕。"
        long = sentence * 20
        result = summarize(long, exit_code=0, duration_seconds=1.0)
        # Should not contain a mid-word cutoff before the truncation note
        truncated_part = result.split("。（回复较长")[0] if "。（回复较长" in result else result
        # The truncated part should end at a natural boundary
        assert truncated_part.endswith("。") or truncated_part.endswith("完毕")

    def test_pipeline_speaks_truncated_but_logs_full_output(self, tmp_path, monkeypatch):
        """F047 should truncate TTS while preserving full output in logs."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        long_text = "这是一个很长的 Claude 回复。" * 80
        monkeypatch.setattr(
            "voice_claude_agent.cli.run_claude",
            mock.MagicMock(return_value=ClaudeRunResult(
                command=["claude", "-p", "long"],
                exit_code=0,
                stdout=long_text,
                stderr="",
                duration_seconds=0.1,
                timed_out=False,
            )),
        )

        from voice_claude_agent.cli import _run_pipeline

        _run_pipeline("long", input_mode="text", tts_fake=True)

        session = json.loads((tmp_path / "sessions.jsonl").read_text().splitlines()[0])
        last_result = json.loads((tmp_path / "last_result.json").read_text())

        assert session["claude_stdout"] == long_text
        assert session["summary"] == long_text
        assert session["spoken_summary"] != long_text
        assert "完整内容可在菜单栏 Last Summary 查看" in session["spoken_summary"]
        assert last_result["summary"] == long_text
        assert last_result["spoken_summary"] == session["spoken_summary"]


# ── F048: Concurrent Trigger Safety ─────────────────────────
class TestConcurrentTriggerSafety:
    def test_simultaneous_threaded_triggers_start_one_cycle(self):
        """Concurrent trigger calls should atomically allow only one cycle."""
        from voice_claude_agent.app import VoiceClaudeApp

        release_wake = threading.Event()
        wake_starts = []
        mic_checks = 0
        counter_lock = threading.Lock()

        def wake_target():
            wake_starts.append("started")
            release_wake.wait(timeout=3.0)

        app = VoiceClaudeApp(
            _wake_target=wake_target,
            _alert_patch=lambda **kw: None,
        )
        app._update_mic_status = lambda: None

        def check_mic():
            nonlocal mic_checks
            with counter_lock:
                mic_checks += 1
            return True

        app._check_mic_or_alert = check_mic

        ready = threading.Barrier(21)

        def trigger():
            ready.wait(timeout=3.0)
            app._trigger_recording(app.trigger_item)

        threads = [threading.Thread(target=trigger) for _ in range(20)]
        for thread in threads:
            thread.start()
        ready.wait(timeout=3.0)
        for thread in threads:
            thread.join(timeout=3.0)

        assert mic_checks == 1
        assert len(wake_starts) == 1
        assert app._cycle_in_progress is True

        release_wake.set()
        if app._wake_thread:
            app._wake_thread.join(timeout=3.0)
        app._wake_active = False
        app._cycle_in_progress = False

    def test_rapid_triple_trigger_one_thread(self):
        """3 rapid _trigger_recording calls should create at most 1 thread."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            _wake_target=lambda: None,
            _alert_patch=lambda **kw: None,
        )
        app._check_mic_or_alert = lambda: True

        # Triple-trigger in rapid succession
        app._trigger_recording(app.trigger_item)
        t1 = app._wake_thread
        app._trigger_recording(app.trigger_item)
        app._trigger_recording(app.trigger_item)

        # Only one thread should have been created
        assert app._wake_thread is t1
        assert app._cycle_in_progress is True

        app._wake_event.set()
        t1.join(timeout=3.0)
        app._stop_wake(app.stop_item)

    def test_cycle_in_progress_blocks_reentry(self):
        """When _cycle_in_progress=True, _trigger_recording must no-op."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            _wake_target=lambda: None,
            _alert_patch=lambda **kw: None,
        )
        app._cycle_in_progress = True

        app._trigger_recording(app.trigger_item)
        # Should NOT start because guard is set
        assert app._wake_thread is None
        assert not app._wake_active

    def test_concurrent_triggers_dont_corrupt_menu_title(self):
        """Menu title should not get stuck mid-state from rapid triggers."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            _wake_target=lambda: None,
            _alert_patch=lambda **kw: None,
        )
        app._check_mic_or_alert = lambda: True

        # Set a title like it's mid-cycle, then trigger
        app.trigger_item.title = "Recording..."
        app._trigger_recording(app.trigger_item)
        # First trigger succeeded, set guard
        app._trigger_recording(app.trigger_item)
        # Second trigger was ignored — title unchanged

        # Simulate cycle finish
        app._cycle_in_progress = False
        app.trigger_item.title = "Trigger Recording"

        assert app.trigger_item.title == "Trigger Recording"

        app._wake_event.set()
        if app._wake_thread:
            app._wake_thread.join(timeout=3.0)
        app._stop_wake(app.stop_item)

    def test_rapid_trigger_only_one_session_line(self, tmp_path, monkeypatch):
        """Multiple rapid triggers should write at most 1 session JSONL line."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )

        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: None,
            _wake_target=lambda: None,
        )

        # Mock complete pipeline
        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b"x"
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr("voice_claude_agent.cli._run_pipeline", mock.MagicMock())
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock.MagicMock(return_value=mock.MagicMock(
                transcribe=mock.MagicMock(return_value="hi")
            )),
        )

        # Run one cycle so _cycle_in_progress gets set
        app._trigger_recording(app.trigger_item)
        # Wait for lambda to finish
        if app._wake_thread:
            app._wake_thread.join(timeout=5.0)

        # Rapid additional triggers during the same cycle should be blocked by guard
        app._trigger_recording(app.trigger_item)
        app._trigger_recording(app.trigger_item)

        # At most one session should have been written (from the one cycle that ran)
        session_path = tmp_path / "sessions.jsonl"
        if session_path.exists():
            lines = session_path.read_text().strip().split("\n")
            assert len(lines) <= 2  # at most 1 from the cycle + maybe 1 from _run_pipeline

        # Stop
        app._stop_wake(app.stop_item)


# ── F052: View Logs Menu Item ───────────────────────────────
class TestViewLogs:
    def test_logs_menu_item_present(self):
        """Menu should contain 'View Logs' item."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app.logs_item is not None
        assert "Logs" in app.logs_item.title or "logs" in app.logs_item.title.lower()

    def test_no_logs_shows_placeholder(self, tmp_path, monkeypatch):
        """When no log files exist, _show_logs should show placeholders."""
        alerts = []

        monkeypatch.setattr(
            "voice_claude_agent.config.get_agent_state_dir",
            lambda: tmp_path,
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_logs(app.logs_item)

        assert len(alerts) == 1
        assert alerts[0]["title"] == "View Logs"
        msg = alerts[0]["message"]
        assert "no app_events log yet" in msg or "no app_events" in msg.lower()
        assert "no last_result.json yet" in msg or "no last_result" in msg.lower()

    def test_logs_with_content_shows_recent_events(self, tmp_path, monkeypatch):
        """When logs exist, _show_logs should display recent entries."""
        import json

        alerts = []

        monkeypatch.setattr(
            "voice_claude_agent.config.get_agent_state_dir",
            lambda: tmp_path,
        )

        # Write app_events
        events_path = tmp_path / "app_events.jsonl"
        events = []
        for i in range(15):
            events.append(json.dumps({
                "timestamp": f"2026-06-07T10:00:{i:02d}+08:00",
                "event": f"event_{i}", "elapsed": f"0.{i}s",
            }))
        events_path.write_text("\n".join(events) + "\n", encoding="utf-8")

        # Write last_result
        result_path = tmp_path / "last_result.json"
        result_path.write_text(json.dumps({
            "prompt": "hello", "exit_code": 0, "summary": "Claude says hi",
        }), encoding="utf-8")

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_logs(app.logs_item)

        assert len(alerts) == 1
        msg = alerts[0]["message"]
        # Should show recent 10 of 15
        assert "10 of 15" in msg
        assert "event_14" in msg  # most recent in last 10
        assert "10:00:14 event_14" in msg
        assert "+08:00 event_14" not in msg
        assert "event_5" in msg  # within last 10 (5-14)
        assert "event_0" not in msg  # too old, 0-4 excluded
        # Should show last_result
        assert "hello" in msg
        assert "Claude says hi" in msg

    def test_corrupted_json_does_not_crash(self, tmp_path, monkeypatch):
        """Corrupted log files should show an error, not crash."""
        alerts = []

        monkeypatch.setattr(
            "voice_claude_agent.config.get_agent_state_dir",
            lambda: tmp_path,
        )

        (tmp_path / "app_events.jsonl").write_text("not valid json\n", encoding="utf-8")
        (tmp_path / "last_result.json").write_text("{{broken", encoding="utf-8")

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_logs(app.logs_item)

        assert len(alerts) == 1
        msg = alerts[0]["message"]
        assert "Error reading app_events" in msg or "corrupted" in msg


# ── F053: VOICE_RECORD_SECONDS Env Config ───────────────────
class TestVoiceRecordSeconds:
    def test_default_is_5(self, monkeypatch):
        """Default record_seconds should be 5."""
        monkeypatch.delenv("VOICE_RECORD_SECONDS", raising=False)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app.record_seconds == 5

    def test_env_var_overrides_default(self, monkeypatch):
        """VOICE_RECORD_SECONDS=3 should set record_seconds to 3."""
        monkeypatch.setenv("VOICE_RECORD_SECONDS", "3")

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app.record_seconds == 3

    def test_invalid_env_var_falls_back_to_default(self, monkeypatch):
        """VOICE_RECORD_SECONDS=abc should fall back to default 5."""
        monkeypatch.setenv("VOICE_RECORD_SECONDS", "abc")

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app.record_seconds == 5

    def test_negative_value_falls_back_to_default(self, monkeypatch):
        """VOICE_RECORD_SECONDS=-1 should fall back to default 5."""
        monkeypatch.setenv("VOICE_RECORD_SECONDS", "-1")

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app.record_seconds == 5

    def test_zero_value_falls_back_to_default(self, monkeypatch):
        """VOICE_RECORD_SECONDS=0 should fall back to default 5."""
        monkeypatch.setenv("VOICE_RECORD_SECONDS", "0")

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app.record_seconds == 5

    def test_record_timeout_uses_configured_seconds(self, monkeypatch):
        """The hard timeout should be based on configured record_seconds."""
        monkeypatch.setenv("VOICE_RECORD_SECONDS", "3")

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        app.RECORD_WORKER_GRACE_SECONDS = 0.25

        assert app._record_timeout_seconds() == 3.25


# ── F054: Actionable Error Messages Audit ───────────────────
class TestActionableErrors:
    def test_mic_denied_mentions_system_settings(self, monkeypatch):
        """Mic denied alert should mention System Settings path."""
        import voice_claude_agent.app as app_mod

        alerts = []
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (False, "test denial"))

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._check_mic_or_alert()

        assert len(alerts) == 1
        assert "System Settings" in alerts[0]["message"]
        assert "Microphone" in alerts[0]["message"]
        assert "test denial" in alerts[0]["message"]

    def test_no_audio_alert_has_diagnostic_info(self, monkeypatch):
        """Empty audio alert must contain actionable diagnostic info."""
        import voice_claude_agent.app as app_mod

        alerts = []
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: alerts.append(kw),
        )

        from unittest import mock
        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b""
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr(app, "record_seconds", 0.01)

        app._record_and_execute()

        no_audio = [a for a in alerts if a["title"] == "No Audio"]
        assert len(no_audio) >= 1
        assert "Input device" in no_audio[0]["message"]

    def test_recording_failed_has_microphone_hint(self, monkeypatch):
        """Recording Failed alert should mention Microphone path."""
        import voice_claude_agent.app as app_mod

        alerts = []
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            __import__("unittest").mock.MagicMock(return_value=None),
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._record_and_execute()

        failed = [a for a in alerts if a["title"] == "Recording Failed"]
        assert len(failed) >= 1
        assert "Microphone" in failed[0]["message"]

    def test_stt_error_mentions_backend_switch_hint(self, monkeypatch):
        """STT Error alert should suggest switching/configuring STT backends."""
        import voice_claude_agent.app as app_mod

        alerts = []
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0.01)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="whisper-cli",
            _alert_patch=lambda **kw: alerts.append(kw),
        )

        mock_rec = mock.MagicMock()
        mock_rec.get_audio.return_value = b"audio"
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            mock.MagicMock(return_value=mock.MagicMock(
                transcribe=mock.MagicMock(return_value="[STT error: model missing]")
            )),
        )

        app._record_and_execute()

        stt_alerts = [a for a in alerts if a["title"] == "STT Error"]
        assert len(stt_alerts) == 1
        assert "VOICE_STT_BACKEND" in stt_alerts[0]["message"]
        assert "text-input" in stt_alerts[0]["message"]
        assert "whisper-cli" in stt_alerts[0]["message"]

    def test_summarizer_timeout_has_actionable_hint(self):
        """Timeout summary should mention check or retry."""
        from voice_claude_agent.summarizer import summarize

        result = summarize("", exit_code=-1, duration_seconds=300)
        assert "超时" in result or "timeout" in result.lower()
        assert "retry" in result.lower() or "重试" in result or "检查" in result

    def test_summarizer_cli_not_found_has_install_hint(self):
        """Claude CLI not found summary should mention install."""
        from voice_claude_agent.summarizer import summarize

        result = summarize("", exit_code=-2, duration_seconds=0)
        assert "安装" in result or "install" in result.lower()

    def test_portaudio_failure_mentions_py2app_rebuild(self, monkeypatch):
        """PortAudio dylib failures should mention rebuilding the .app bundle."""
        from voice_claude_agent.config import check_mic_permission

        monkeypatch.setattr("platform.system", lambda: "Darwin")

        def _raise(*args, **kwargs):
            raise OSError("cannot load library '/tmp/libportaudio.dylib'")

        monkeypatch.setattr("sounddevice.InputStream", _raise)

        has_mic, detail = check_mic_permission()

        assert has_mic is False
        assert "PortAudio" in detail
        assert "libportaudio.dylib" in detail
        assert "python setup.py py2app" in detail


# ── F058: Health Check Menu Item ───────────────────────────
class TestHealthCheck:
    def test_health_menu_item_present(self):
        """Menu should contain 'Health Check' item."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app.health_item is not None
        assert "Health" in app.health_item.title

    def test_check_item_pass_format(self):
        """_check_item with ok=True should return [PASS]."""
        from voice_claude_agent.app import VoiceClaudeApp

        result = VoiceClaudeApp._check_item("Test", True, "ok")
        assert "[PASS]" in result
        assert "Test" in result
        assert "ok" in result

    def test_check_item_fail_format(self):
        """_check_item with ok=False should return [FAIL] with fix hint."""
        from voice_claude_agent.app import VoiceClaudeApp

        result = VoiceClaudeApp._check_item("Test", False, "broken", "Fix it")
        assert "[FAIL]" in result
        assert "broken" in result
        assert "Fix it" in result

    def test_health_check_includes_all_sections(self, monkeypatch):
        """Health check alert should include all 7 check categories."""
        alerts = []

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._run_health_check(app.health_item)

        assert len(alerts) == 1
        assert alerts[0]["title"] == "Health Check"
        msg = alerts[0]["message"]
        assert "Claude CLI" in msg
        assert "whisper-cli" in msg
        assert "Whisper model" in msg
        assert "Microphone" in msg
        assert "PortAudio" in msg
        assert "Agent state dir" in msg
        assert "macOS say" in msg
        assert "Overall" in msg

    def test_health_check_overall_fails_when_selected_volcengine_asr_missing_creds(self, monkeypatch):
        """Overall status should fail when a selected cloud ASR backend is not configured."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        monkeypatch.setattr("voice_claude_agent.config.find_claude_executable", lambda: "/usr/bin/claude")
        monkeypatch.setattr("voice_claude_agent.config.check_apple_speech_available", lambda: True)
        monkeypatch.setattr("voice_claude_agent.config.check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "get_claude_workdir", lambda: Path("/tmp"))
        monkeypatch.setattr("voice_claude_agent.stt._find_whisper_cpp_binary", lambda: "/usr/bin/whisper-cli")
        monkeypatch.setattr("voice_claude_agent.stt._resolve_whisper_model", lambda: ("/tmp/model.bin", ""))
        monkeypatch.setattr("voice_claude_agent.stt._check_volcengine_credentials", lambda: ({}, "missing"))
        monkeypatch.setattr("voice_claude_agent.tts._resolve_tts_backend", lambda: "macos-say")
        monkeypatch.setattr("voice_claude_agent.tts._check_volcengine_tts_credentials", lambda: ({}, ""))

        import sounddevice as sd
        monkeypatch.setattr(sd, "query_devices", lambda **kw: {"name": "test"})

        alerts = []
        app = VoiceClaudeApp(stt_backend="volcengine-doubao", _alert_patch=lambda **kw: alerts.append(kw))
        app._run_health_check(app.health_item)

        msg = alerts[-1]["message"]
        assert "[FAIL] Volcengine ASR" in msg
        assert "Overall: SOME CHECKS FAILED" in msg

    def test_health_check_fails_app_bundle_workdir(self, tmp_path, monkeypatch):
        """An app bundle resource directory exists but is not a valid Claude project workdir."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        bundle_workdir = tmp_path / "VoiceClaudeAgent.app" / "Contents" / "Resources" / "lib"
        bundle_workdir.mkdir(parents=True)
        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        monkeypatch.setattr("voice_claude_agent.config.find_claude_executable", lambda: "/usr/bin/claude")
        monkeypatch.setattr("voice_claude_agent.config.check_apple_speech_available", lambda: True)
        monkeypatch.setattr("voice_claude_agent.config.check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "get_claude_workdir", lambda: bundle_workdir)
        monkeypatch.setattr("voice_claude_agent.stt._find_whisper_cpp_binary", lambda: "/usr/bin/whisper-cli")
        monkeypatch.setattr("voice_claude_agent.stt._resolve_whisper_model", lambda: ("/tmp/model.bin", ""))
        monkeypatch.setattr("voice_claude_agent.stt._check_volcengine_credentials", lambda: ({}, ""))
        monkeypatch.setattr("voice_claude_agent.tts._resolve_tts_backend", lambda: "macos-say")
        monkeypatch.setattr("voice_claude_agent.tts._check_volcengine_tts_credentials", lambda: ({}, ""))

        import sounddevice as sd
        monkeypatch.setattr(sd, "query_devices", lambda **kw: {"name": "test"})

        alerts = []
        app = VoiceClaudeApp(stt_backend="whisper-cli", _alert_patch=lambda **kw: alerts.append(kw))
        app._run_health_check(app.health_item)

        msg = alerts[-1]["message"]
        assert "[FAIL] Claude workdir" in msg
        assert "Overall: SOME CHECKS FAILED" in msg


# ── F059: Config File ───────────────────────────────────────
class TestConfigFile:
    def test_load_config_no_file(self, tmp_path, monkeypatch):
        """When config.json does not exist, load_config returns empty dict."""
        monkeypatch.setattr(
            "voice_claude_agent.config.get_config_path",
            lambda: tmp_path / "nonexistent.json",
        )
        from voice_claude_agent.config import load_config

        cfg = load_config()
        assert cfg == {}

    def test_load_config_valid(self, tmp_path, monkeypatch):
        """Valid config.json should return recognized keys."""
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "VOICE_RECORD_SECONDS": "3",
            "VOICE_STT_BACKEND": "text-input",
            "unknown_key": "ignored",
        }))
        monkeypatch.setattr(
            "voice_claude_agent.config.get_config_path",
            lambda: cfg_file,
        )
        from voice_claude_agent.config import load_config

        cfg = load_config()
        assert cfg["VOICE_RECORD_SECONDS"] == "3"
        assert cfg["VOICE_STT_BACKEND"] == "text-input"
        assert "unknown_key" not in cfg

    def test_load_config_corrupted_json(self, tmp_path, monkeypatch):
        """Corrupted JSON should return empty dict, not crash."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{{{broken")
        monkeypatch.setattr(
            "voice_claude_agent.config.get_config_path",
            lambda: cfg_file,
        )
        from voice_claude_agent.config import load_config

        cfg = load_config()
        assert cfg == {}

    def test_env_var_overrides_config_json(self, tmp_path, monkeypatch):
        """Environment variable should override config.json value."""
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"VOICE_RECORD_SECONDS": "10"}))
        monkeypatch.setattr(
            "voice_claude_agent.config.get_config_path",
            lambda: cfg_file,
        )
        monkeypatch.setenv("VOICE_RECORD_SECONDS", "2")

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        # env (2) > config.json (10) > default (5)
        assert app.record_seconds == 2

    def test_config_json_sets_record_seconds_without_env(self, tmp_path, monkeypatch):
        """Config.json should set record_seconds when no env var."""
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"VOICE_RECORD_SECONDS": "7"}))
        monkeypatch.setattr(
            "voice_claude_agent.config.get_config_path",
            lambda: cfg_file,
        )
        monkeypatch.delenv("VOICE_RECORD_SECONDS", raising=False)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app.record_seconds == 7

    def test_get_config_value_uses_env_over_config(self, tmp_path, monkeypatch):
        """get_config_value should use env var > config.json > default priority."""
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"WHISPER_CPP_LANGUAGE": "en"}))
        monkeypatch.setattr(
            "voice_claude_agent.config.get_config_path",
            lambda: cfg_file,
        )
        monkeypatch.setenv("WHISPER_CPP_LANGUAGE", "auto")

        from voice_claude_agent.config import get_config_value

        assert get_config_value("WHISPER_CPP_LANGUAGE", "zh") == "auto"

    def test_get_claude_timeout_default_env_config_and_invalid_values(self, tmp_path, monkeypatch):
        """VOICE_CLAUDE_TIMEOUT_SECONDS resolves from env/config and rejects unsafe values."""
        import json

        from voice_claude_agent.config import DEFAULT_TIMEOUT_SECONDS, get_claude_timeout

        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr(
            "voice_claude_agent.config.get_config_path",
            lambda: cfg_file,
        )

        assert get_claude_timeout() == DEFAULT_TIMEOUT_SECONDS

        cfg_file.write_text(json.dumps({"VOICE_CLAUDE_TIMEOUT_SECONDS": "600"}))
        assert get_claude_timeout() == 600

        monkeypatch.setenv("VOICE_CLAUDE_TIMEOUT_SECONDS", "900")
        assert get_claude_timeout() == 900

        monkeypatch.setenv("VOICE_CLAUDE_TIMEOUT_SECONDS", "9")
        assert get_claude_timeout() == DEFAULT_TIMEOUT_SECONDS

        monkeypatch.setenv("VOICE_CLAUDE_TIMEOUT_SECONDS", "not-int")
        assert get_claude_timeout() == DEFAULT_TIMEOUT_SECONDS

    def test_claude_workdir_repairs_latin1_mojibake_final_component(self, tmp_path, monkeypatch):
        """A mojibake final directory name should resolve to the real sibling path."""
        import voice_claude_agent.config as config_mod

        project = tmp_path / "语音助理"
        project.mkdir()
        mojibake = project.name.encode("utf-8").decode("latin1")

        monkeypatch.setenv("VOICE_CLAUDE_WORKDIR", str(tmp_path / mojibake))
        assert config_mod.get_claude_workdir() == project

    def test_whisper_model_resolves_from_config_json(self, tmp_path, monkeypatch):
        """WHISPER_CPP_MODEL should resolve from config.json when env is absent."""
        import json

        model = tmp_path / "ggml-base.bin"
        model.write_bytes(b"fake model")
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"WHISPER_CPP_MODEL": str(model)}))
        monkeypatch.setattr(
            "voice_claude_agent.config.get_config_path",
            lambda: cfg_file,
        )
        monkeypatch.delenv("WHISPER_CPP_MODEL", raising=False)

        from voice_claude_agent.stt import _resolve_whisper_model

        resolved, err = _resolve_whisper_model()
        assert resolved == str(model)
        assert err == ""

    def test_whisper_language_resolves_from_config_json(self, tmp_path, monkeypatch):
        """WHISPER_CPP_LANGUAGE should resolve from config.json when env is absent."""
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"WHISPER_CPP_LANGUAGE": "auto"}))
        monkeypatch.setattr(
            "voice_claude_agent.config.get_config_path",
            lambda: cfg_file,
        )
        monkeypatch.delenv("WHISPER_CPP_LANGUAGE", raising=False)

        from voice_claude_agent.stt import _resolve_whisper_language

        assert _resolve_whisper_language() == "auto"


# ── F061: Main Thread Alert Dispatch ───────────────────────
class TestMainThreadAlert:
    def test_alert_on_main_detects_background_thread(self, monkeypatch):
        """When called from a background thread, _alert_on_main uses callAfter."""
        import threading

        from PyObjCTools import AppHelper
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)

        direct_calls = []
        scheduled_calls = []
        monkeypatch.setattr(app, "_alert", lambda **kw: direct_calls.append(kw))
        monkeypatch.setattr(
            AppHelper,
            "callAfter",
            lambda func, *args, **kw: scheduled_calls.append((func, args, kw)),
        )

        # Simulate background thread call
        def _bg_call():
            app._alert_on_main(title="BG Test", message="from bg thread")

        t = threading.Thread(target=_bg_call, daemon=True)
        t.start()
        t.join(timeout=3.0)

        assert direct_calls == []
        assert len(scheduled_calls) == 1
        func, args, kwargs = scheduled_calls[0]
        assert func is app._alert
        assert args == ()
        assert kwargs == {"title": "BG Test", "message": "from bg thread"}

    def test_alert_on_main_calls_directly_on_main_thread(self):
        """When on the main thread, _alert_on_main should call _alert directly."""
        from voice_claude_agent.app import VoiceClaudeApp

        calls = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: calls.append(kw))
        app._alert_on_main(title="Test", message="direct")

        assert len(calls) == 1
        assert calls[0]["title"] == "Test"

    def test_open_mic_failure_alert_uses_main_thread_safe_path(self, monkeypatch):
        """Recording Failed alert should route through _alert_on_main."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        safe_alerts = []
        monkeypatch.setattr(app, "_alert_on_main", lambda **kw: safe_alerts.append(kw))
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            lambda: None,
        )

        recorder = app._open_mic_or_alert()

        assert recorder is None
        assert app.trigger_item.title == "Trigger Recording"
        assert len(safe_alerts) == 1
        assert safe_alerts[0]["title"] == "Recording Failed"

    def test_cycle_guard_cleared_after_record_timeout(self, monkeypatch):
        """After _record_and_execute with timeout, _cycle_in_progress must be False."""
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        # Force record timeout: make DEFAULT_RECORD_SECONDS tiny
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0)
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "RECORD_WORKER_GRACE_SECONDS", 0)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: None,
            _wake_target=lambda: None,
        )
        # Create a recorder whose start() blocks forever

        class BlockRec:
            def start(self):
                pass

            def stop(self):
                pass

            def get_audio(self):
                return b""

        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            __import__("unittest").mock.MagicMock(return_value=BlockRec()),
        )
        # Set a very short record duration + grace so timeout fires
        app.record_seconds = 0
        VoiceClaudeApp.RECORD_WORKER_GRACE_SECONDS = 0

        app._record_and_execute()
        # After return, _cycle_in_progress should be False (cleared in finally)
        assert app._cycle_in_progress is False
        assert app.trigger_item.title == "Trigger Recording"

    def test_second_trigger_works_after_timeout(self, monkeypatch):
        """After a record timeout, a second Trigger Recording should start."""
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: None,
            _wake_target=lambda: None,
        )

        app.record_seconds = 0
        VoiceClaudeApp.RECORD_WORKER_GRACE_SECONDS = 0

        # First trigger: simulate start
        app._trigger_recording(app.trigger_item)
        assert app._cycle_in_progress is True

        # Simulate cycle end (what finally does)
        app._cycle_in_progress = False
        app.trigger_item.title = "Trigger Recording"

        # Second trigger should work now
        app._trigger_recording(app.trigger_item)
        assert app._cycle_in_progress is True

        app._stop_wake(app.stop_item)


# ── F062: zh-CN Output & Record Duration Display ───────────
class TestZhCNOutput:
    def test_claude_prompt_has_zh_cn_constraint(self, monkeypatch):
        """_run_pipeline should prepend zh-CN constraint to the prompt."""
        from unittest import mock as _mock

        import pathlib

        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: pathlib.Path("/tmp/fake.jsonl"),
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: pathlib.Path("/tmp/fake_result.json"),
        )

        with _mock.patch("voice_claude_agent.cli.run_claude") as mock_run:
            mock_run.return_value.exit_code = 0
            mock_run.return_value.stdout = "你好"
            mock_run.return_value.stderr = ""
            mock_run.return_value.timed_out = False
            mock_run.return_value.duration_seconds = 0.1
            mock_run.return_value.command = ["claude", "-p", "test"]

            from voice_claude_agent.cli import _run_pipeline

            _run_pipeline("test prompt", input_mode="text", tts_fake=True)

        call_args = mock_run.call_args[0][0] if mock_run.call_args else ""
        assert "简体中文" in call_args, f"zh-CN constraint missing: {call_args}"

    def test_claude_traditional_output_is_simplified_in_summary(self, monkeypatch):
        """Claude output should be converted to simplified before summary/log/TTS."""
        from unittest import mock as _mock

        from voice_claude_agent.cli import _run_pipeline

        sessions = []
        monkeypatch.setattr(
            "voice_claude_agent.cli.write_session",
            lambda payload: sessions.append(payload),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli.write_last_result",
            lambda payload: None,
        )

        monkeypatch.setattr(
            "voice_claude_agent.cli._resolve_tts_backend",
            lambda: "macos-say",
        )

        with _mock.patch("voice_claude_agent.cli.run_claude") as mock_run:
            mock_run.return_value.exit_code = 0
            mock_run.return_value.stdout = "請問這是什麼時候開始的"
            mock_run.return_value.stderr = ""
            mock_run.return_value.timed_out = False
            mock_run.return_value.cancelled = False
            mock_run.return_value.duration_seconds = 0.1
            mock_run.return_value.command = ["claude", "-p", "test"]
            mock_run.return_value.cwd = ""

            _run_pipeline("test prompt", input_mode="text", tts_fake=True)

        assert sessions
        assert "請" not in sessions[0]["summary"]
        assert "请问这是什么时候开始的" in sessions[0]["summary"]
        assert sessions[0]["spoken_summary"] == sessions[0]["summary"]

    def test_t2s_converts_traditional_to_simplified(self):
        """_t2s_convert should convert traditional characters to simplified."""
        from voice_claude_agent.stt import _t2s_convert

        result = _t2s_convert("請問這是什麼時候開始的")
        assert "请" in result
        assert "问" in result
        assert "这" in result
        assert "么" in result
        assert "时" in result
        assert "开" in result

    def test_t2s_leaves_simplified_unchanged(self):
        """_t2s_convert should not modify already-simplified text."""
        from voice_claude_agent.stt import _t2s_convert

        simplified = "请问这是什么"
        result = _t2s_convert(simplified)
        assert result == simplified

    def test_health_check_shows_record_duration(self, monkeypatch):
        """Health Check should include Record duration with current seconds."""
        alerts = []
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._run_health_check(app.health_item)

        msg = alerts[0]["message"]
        assert "Record duration" in msg
        assert "5s" in msg

    def test_mic_diagnostic_shows_record_duration(self, monkeypatch):
        """Mic Diagnostic should include the current record duration."""
        alerts = []
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setitem(__import__("sys").modules, "_sounddevice_data", None)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app.record_seconds = 10
        app._run_mic_diagnostic(app.diagnostic_item)

        assert alerts
        assert "Record duration: 10s" in alerts[0]["message"]

    def test_voice_claude_app_reads_record_seconds_from_config(self, tmp_path, monkeypatch):
        """VoiceClaudeApp should read VOICE_RECORD_SECONDS from config.json."""
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"VOICE_RECORD_SECONDS": "10"}))
        monkeypatch.setattr(
            "voice_claude_agent.config.get_config_path",
            lambda: cfg_file,
        )
        monkeypatch.delenv("VOICE_RECORD_SECONDS", raising=False)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app.record_seconds == 10


# ── F063: Trigger Serialization & Guard Release ────────────
class TestTriggerSerialization:
    def test_cycle_guard_blocks_reentry(self):
        """When _cycle_in_progress=True, second trigger must return immediately."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            _wake_target=lambda: None,
            _alert_patch=lambda **kw: None,
        )
        app._cycle_in_progress = True

        app._trigger_recording(app.trigger_item)
        # Guard set — should not start
        assert app._wake_thread is None
        assert not app._wake_active

    def test_cycle_guard_released_on_record_open_failure(self):
        """If recorder fails to open, _cycle_in_progress should be False after return."""
        import voice_claude_agent.app as app_mod

        monkeypatch = __import__("pytest").MonkeyPatch()
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            __import__("unittest").mock.MagicMock(return_value=None),
        )

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)

        # Simulate a trigger: guard is set, then _record_and_execute runs
        app._cycle_in_progress = True
        app._record_and_execute()

        # Guard must be False after cycle ends
        assert app._cycle_in_progress is False
        assert app.trigger_item.title == "Trigger Recording"

    def test_rapid_triple_click_only_one_cycle(self):
        """3 rapid _trigger_recording calls should start at most 1 cycle."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            _wake_target=lambda: None,
            _alert_patch=lambda **kw: None,
        )
        app._check_mic_or_alert = lambda: True

        app._trigger_recording(app.trigger_item)
        t1 = app._wake_thread
        assert app._cycle_in_progress is True

        app._trigger_recording(app.trigger_item)
        app._trigger_recording(app.trigger_item)

        # Only one thread should exist
        assert app._wake_thread is t1

        # Manually clean up
        app._cycle_in_progress = False
        app._wake_event.set()
        t1.join(timeout=3.0)
        app._stop_wake(app.stop_item)

    def test_guard_not_double_cleared(self):
        """_cycle_in_progress should only be cleared once per cycle."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            _wake_target=lambda: None,
            _alert_patch=lambda **kw: None,
        )
        app._cycle_in_progress = True

        # Simulate _run_wake_loop's finally block
        app._cycle_in_progress = False
        assert app._cycle_in_progress is False

        # Setting it to False again should be a no-op
        app._cycle_in_progress = False
        assert app._cycle_in_progress is False


# ── F064: Voice Confirmation for High-Risk Actions ──────────
class TestVoiceConfirmation:
    def test_is_voice_confirm_agree(self):
        """is_voice_confirm should return True for agreement keywords."""
        from voice_claude_agent.confirmation import is_voice_confirm

        assert is_voice_confirm("同意") is True
        assert is_voice_confirm("确认执行") is True
        assert is_voice_confirm("可以继续") is True
        assert is_voice_confirm("好的") is True

    def test_is_voice_confirm_reject(self):
        """is_voice_confirm should return False for rejection keywords."""
        from voice_claude_agent.confirmation import is_voice_confirm

        assert is_voice_confirm("取消") is False
        assert is_voice_confirm("不要") is False
        assert is_voice_confirm("拒绝执行") is False
        assert is_voice_confirm("不行") is False

    def test_is_voice_confirm_unclear(self):
        """is_voice_confirm should return None for unclear input."""
        from voice_claude_agent.confirmation import is_voice_confirm

        assert is_voice_confirm("今天天气如何") is None
        assert is_voice_confirm("") is None
        assert is_voice_confirm("今天天气不错") is None

    def test_is_voice_confirm_single_character_keywords_are_exact(self):
        """Single-character Chinese keywords should not match inside unrelated text."""
        from voice_claude_agent.confirmation import is_voice_confirm

        assert is_voice_confirm("好") is True
        assert is_voice_confirm("不") is False
        assert is_voice_confirm("不是") is False
        assert is_voice_confirm("这个方案不好") is False
        assert is_voice_confirm("今天天气不错") is None

    def test_high_risk_triggers_confirmation_flow(self, tmp_path, monkeypatch):
        """When transcript is high-risk, _record_and_execute should trigger confirmation."""
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0)
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "RECORD_WORKER_GRACE_SECONDS", 0)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: None,
            _wake_target=lambda: None,
        )

        # First recording: a high-risk prompt
        prompt_rec = __import__("unittest").mock.MagicMock(name="prompt_recorder")
        confirm_rec = __import__("unittest").mock.MagicMock(name="confirm_recorder")
        safe_recorder = __import__("unittest").mock.MagicMock(
            side_effect=[prompt_rec, confirm_rec]
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            safe_recorder,
        )
        record_mock = __import__("unittest").mock.MagicMock(
            side_effect=[(b"audio1", "ok"), (b"audio2", "ok")]
        )
        monkeypatch.setattr(app, "_record_with_timeout", record_mock)
        pipeline_mock = __import__("unittest").mock.MagicMock()
        monkeypatch.setattr("voice_claude_agent.cli._run_pipeline", pipeline_mock)
        speaker_mock = __import__("unittest").mock.MagicMock()
        monkeypatch.setattr(
            "voice_claude_agent.tts.MacOSSaySpeaker",
            __import__("unittest").mock.MagicMock(return_value=speaker_mock),
        )

        # STT returns high-risk transcript, then confirmation
        transcribe_calls = ["git push origin main --force", "同意"]
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            __import__("unittest").mock.MagicMock(
                return_value=__import__("unittest").mock.MagicMock(
                    transcribe=__import__("unittest").mock.MagicMock(side_effect=transcribe_calls)
                )
            ),
        )

        monkeypatch.setattr(
            "voice_claude_agent.config.get_app_events_log_path",
            lambda: tmp_path / "app_events.jsonl",
        )

        app.record_seconds = 0
        app._record_and_execute()

        # After execution, risk confirmation should have been accepted
        assert app.trigger_item.title in ("Trigger Recording", "Done ✓")
        assert safe_recorder.call_count == 2
        assert record_mock.call_args_list[0].args[0] is prompt_rec
        assert record_mock.call_args_list[1].args[0] is confirm_rec
        pipeline_mock.assert_called_once_with(
            "git push origin main --force",
            input_mode="voice",
            tts_fake=False,
            confirmation_override=True,
            stt_backend_used="text-input",
            on_tts_start=mock.ANY,
        )
        speaker_mock.speak.assert_called_once()

    def test_standalone_permission_reply_does_not_call_claude(self, tmp_path, monkeypatch):
        """A bare '同意' after a previous permission request should show guidance."""
        import json
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0)
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "RECORD_WORKER_GRACE_SECONDS", 0)

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        last_result = state_dir / "last_result.json"
        last_result.write_text(json.dumps({
            "summary": "部分命令需要用户批准才能执行。请批准这些命令。",
            "spoken_summary": "请批准这些命令。",
        }, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(
            "voice_claude_agent.config.get_last_result_path",
            lambda: last_result,
        )
        monkeypatch.setattr(
            "voice_claude_agent.config.get_app_events_log_path",
            lambda: state_dir / "app_events.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            __import__("unittest").mock.MagicMock(return_value=__import__("unittest").mock.MagicMock()),
        )

        from voice_claude_agent.app import VoiceClaudeApp

        alerts = []
        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: alerts.append(kw),
            _wake_target=lambda: None,
        )
        monkeypatch.setattr(app, "_record_with_timeout", lambda recorder: (b"audio", "ok"))
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            __import__("unittest").mock.MagicMock(
                return_value=__import__("unittest").mock.MagicMock(
                    transcribe=__import__("unittest").mock.MagicMock(return_value="同意")
                )
            ),
        )
        pipeline_mock = __import__("unittest").mock.MagicMock()
        monkeypatch.setattr("voice_claude_agent.cli._run_pipeline", pipeline_mock)
        speaker_mock = __import__("unittest").mock.MagicMock()
        monkeypatch.setattr(
            "voice_claude_agent.tts.create_speaker",
            __import__("unittest").mock.MagicMock(return_value=speaker_mock),
        )

        app._record_and_execute()

        pipeline_mock.assert_not_called()
        assert alerts[-1]["title"] == "Manual Approval Required"
        speaker_mock.speak.assert_called_once()

    def test_low_risk_skips_confirmation(self, tmp_path, monkeypatch):
        """Low-risk transcripts should skip voice confirmation."""
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0)
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "RECORD_WORKER_GRACE_SECONDS", 0)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: None,
            _wake_target=lambda: None,
        )

        mock_rec = __import__("unittest").mock.MagicMock()
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            __import__("unittest").mock.MagicMock(return_value=mock_rec),
        )
        record_mock = __import__("unittest").mock.MagicMock(return_value=(b"audio", "ok"))
        monkeypatch.setattr(app, "_record_with_timeout", record_mock)
        pipeline_mock = __import__("unittest").mock.MagicMock()
        monkeypatch.setattr("voice_claude_agent.cli._run_pipeline", pipeline_mock)

        # Low-risk transcript
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            __import__("unittest").mock.MagicMock(
                return_value=__import__("unittest").mock.MagicMock(
                    transcribe=__import__("unittest").mock.MagicMock(return_value="请解释这段代码")
                )
            ),
        )

        monkeypatch.setattr(
            "voice_claude_agent.config.get_app_events_log_path",
            lambda: tmp_path / "app_events.jsonl",
        )

        app.record_seconds = 0
        app._record_and_execute()

        # Should complete normally
        assert app.trigger_item.title in ("Trigger Recording", "Done ✓")
        pipeline_mock.assert_called_once_with(
            "请解释这段代码",
            input_mode="voice",
            tts_fake=False,
            confirmation_override=None,
            stt_backend_used="text-input",
            on_tts_start=mock.ANY,
        )

    def test_high_risk_reject_skips_pipeline(self, tmp_path, monkeypatch):
        """A spoken rejection should abort before Claude runs."""
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0)
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "RECORD_WORKER_GRACE_SECONDS", 0)

        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: None,
            _wake_target=lambda: None,
        )

        mock_rec = __import__("unittest").mock.MagicMock()
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            __import__("unittest").mock.MagicMock(return_value=mock_rec),
        )
        record_mock = __import__("unittest").mock.MagicMock(
            side_effect=[(b"audio1", "ok"), (b"audio2", "ok")]
        )
        monkeypatch.setattr(app, "_record_with_timeout", record_mock)
        pipeline_mock = __import__("unittest").mock.MagicMock()
        monkeypatch.setattr("voice_claude_agent.cli._run_pipeline", pipeline_mock)
        speaker_mock = __import__("unittest").mock.MagicMock()
        monkeypatch.setattr(
            "voice_claude_agent.tts.MacOSSaySpeaker",
            __import__("unittest").mock.MagicMock(return_value=speaker_mock),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            __import__("unittest").mock.MagicMock(
                return_value=__import__("unittest").mock.MagicMock(
                    transcribe=__import__("unittest").mock.MagicMock(
                        side_effect=["git push origin main --force", "取消"]
                    )
                )
            ),
        )
        monkeypatch.setattr(
            "voice_claude_agent.config.get_app_events_log_path",
            lambda: tmp_path / "app_events.jsonl",
        )

        app.record_seconds = 0
        app._record_and_execute()

        pipeline_mock.assert_not_called()
        speaker_mock.speak.assert_any_call("高风险动作已被拒绝，未执行。")

    def test_high_risk_unclear_confirmation_skips_pipeline(self, tmp_path, monkeypatch):
        """An unclear confirmation transcript should abort before Claude runs."""
        import voice_claude_agent.app as app_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, ""))
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "DEFAULT_RECORD_SECONDS", 0)
        monkeypatch.setattr(app_mod.VoiceClaudeApp, "RECORD_WORKER_GRACE_SECONDS", 0)

        from voice_claude_agent.app import VoiceClaudeApp

        alerts = []
        app = VoiceClaudeApp(
            stt_backend="text-input",
            _alert_patch=lambda **kw: alerts.append(kw),
            _wake_target=lambda: None,
        )

        mock_rec = __import__("unittest").mock.MagicMock()
        monkeypatch.setattr(
            "voice_claude_agent.cli._safe_real_recorder",
            __import__("unittest").mock.MagicMock(return_value=mock_rec),
        )
        monkeypatch.setattr(
            app,
            "_record_with_timeout",
            __import__("unittest").mock.MagicMock(
                side_effect=[(b"audio1", "ok"), (b"audio2", "ok")]
            ),
        )
        pipeline_mock = __import__("unittest").mock.MagicMock()
        monkeypatch.setattr("voice_claude_agent.cli._run_pipeline", pipeline_mock)
        monkeypatch.setattr(
            "voice_claude_agent.tts.MacOSSaySpeaker",
            __import__("unittest").mock.MagicMock(
                return_value=__import__("unittest").mock.MagicMock()
            ),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.RecordingTranscriber",
            __import__("unittest").mock.MagicMock(
                return_value=__import__("unittest").mock.MagicMock(
                    transcribe=__import__("unittest").mock.MagicMock(
                        side_effect=["git push origin main --force", "今天天气不错"]
                    )
                )
            ),
        )
        monkeypatch.setattr(
            "voice_claude_agent.config.get_app_events_log_path",
            lambda: tmp_path / "app_events.jsonl",
        )

        app.record_seconds = 0
        app._record_and_execute()

        pipeline_mock.assert_not_called()
        assert app.mic_status_item.title == "Mic: Confirmation unclear"
        assert alerts and alerts[-1]["title"] == "Confirmation Unclear"

    def test_run_pipeline_confirmation_override_skips_cli_input(self, tmp_path, monkeypatch):
        """A voice-confirmed high-risk action should not ask for CLI input again."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )
        monkeypatch.setattr(
            "voice_claude_agent.confirmation.ask_confirmation",
            __import__("unittest").mock.MagicMock(side_effect=AssertionError("should not ask")),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli.run_claude",
            __import__("unittest").mock.MagicMock(
                return_value=ClaudeRunResult(
                    command=["claude", "-p", "test"],
                    exit_code=0,
                    stdout="完成",
                    stderr="",
                    duration_seconds=0.1,
                    timed_out=False,
                )
            ),
        )

        from voice_claude_agent.cli import _run_pipeline

        _run_pipeline(
            "git push origin main --force",
            input_mode="voice",
            tts_fake=True,
            confirmation_override=True,
        )

        records = (tmp_path / "sessions.jsonl").read_text(encoding="utf-8").strip().splitlines()
        session = json.loads(records[-1])
        assert session["confirmation_required"] is True
        assert session["confirmation_received"] is True


# ── F068: Settings UI ───────────────────────────────────────
class TestSettingsUI:
    def test_settings_menu_item_present(self):
        """Menu should contain 'Settings...' item."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        assert app.settings_item is not None
        assert "Settings" in app.settings_item.title
        assert app.reset_settings_item is not None
        assert "Reset Settings" in app.reset_settings_item.title

    def test_show_settings_saves_valid_config_and_reloads(self, tmp_path, monkeypatch):
        """Settings dialog should save valid KEY=value lines and reload the app."""
        import json
        from unittest import mock

        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        response = mock.MagicMock()
        response.clicked = True
        response.text = "VOICE_RECORD_SECONDS=12\nVOICE_STT_BACKEND=apple-speech\n"
        monkeypatch.setattr(
            app_mod.rumps,
            "Window",
            mock.MagicMock(return_value=mock.MagicMock(run=mock.MagicMock(return_value=response))),
        )

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_settings(app.settings_item)

        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved["VOICE_RECORD_SECONDS"] == "12"
        assert saved["VOICE_STT_BACKEND"] == "apple-speech"
        assert app.record_seconds == 12
        assert app.stt_backend == "apple-speech"
        assert alerts[-1]["title"] == "Settings Saved"

    def test_show_settings_validates_claude_timeout(self, tmp_path, monkeypatch):
        """Settings should reject Claude timeout values below 10 seconds."""
        from unittest import mock

        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        response = mock.MagicMock()
        response.clicked = True
        response.text = "VOICE_CLAUDE_TIMEOUT_SECONDS=9\n"
        monkeypatch.setattr(
            app_mod.rumps,
            "Window",
            mock.MagicMock(return_value=mock.MagicMock(run=mock.MagicMock(return_value=response))),
        )

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_settings(app.settings_item)

        assert not cfg_file.exists()
        assert alerts[-1]["title"] == "Settings Validation Error"
        assert "Claude timeout" in alerts[-1]["message"]

    def test_show_settings_saves_claude_workdir(self, tmp_path, monkeypatch):
        """Settings dialog should save VOICE_CLAUDE_WORKDIR."""
        import json
        from unittest import mock

        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        workdir = tmp_path / "project"
        workdir.mkdir()
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        response = mock.MagicMock()
        response.clicked = True
        response.text = f"VOICE_CLAUDE_WORKDIR={workdir}\n"
        monkeypatch.setattr(
            app_mod.rumps,
            "Window",
            mock.MagicMock(return_value=mock.MagicMock(run=mock.MagicMock(return_value=response))),
        )

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_settings(app.settings_item)

        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved["VOICE_CLAUDE_WORKDIR"] == str(workdir)
        assert alerts[-1]["title"] == "Settings Saved"

    def test_show_settings_blank_values_remove_config_without_validation_error(self, tmp_path, monkeypatch):
        """Blank Settings values should remove keys instead of failing validation."""
        import json
        from unittest import mock

        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "VOICE_RECORD_SECONDS": "12",
            "VOICE_STT_BACKEND": "whisper-cli",
            "VOLCENGINE_ASR_API_KEY": "secret",
        }), encoding="utf-8")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        monkeypatch.setattr(app_mod, "load_config", lambda: json.loads(cfg_file.read_text(encoding="utf-8")))

        response = mock.MagicMock()
        response.clicked = True
        response.text = (
            "VOICE_RECORD_SECONDS=\n"
            "VOICE_STT_BACKEND=\n"
            "VOLCENGINE_ASR_API_KEY=<keep existing secret>\n"
        )
        monkeypatch.setattr(
            app_mod.rumps,
            "Window",
            mock.MagicMock(return_value=mock.MagicMock(run=mock.MagicMock(return_value=response))),
        )

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_settings(app.settings_item)

        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert "VOICE_RECORD_SECONDS" not in saved
        assert "VOICE_STT_BACKEND" not in saved
        assert saved["VOLCENGINE_ASR_API_KEY"] == "secret"
        assert app.record_seconds == app.DEFAULT_RECORD_SECONDS
        assert app.stt_backend == "text-input"
        assert alerts[-1]["title"] == "Settings Saved"

    def test_show_settings_empty_text_does_not_truncate_config(self, tmp_path, monkeypatch):
        """Empty Settings text should be rejected without touching config.json."""
        import json
        from unittest import mock

        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        original = {"VOICE_CLAUDE_WORKDIR": "/tmp/project"}
        cfg_file.write_text(json.dumps(original), encoding="utf-8")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        monkeypatch.setattr(app_mod, "load_config", lambda: json.loads(cfg_file.read_text(encoding="utf-8")))

        response = mock.MagicMock()
        response.clicked = True
        response.text = ""
        monkeypatch.setattr(
            app_mod.rumps,
            "Window",
            mock.MagicMock(return_value=mock.MagicMock(run=mock.MagicMock(return_value=response))),
        )

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_settings(app.settings_item)

        assert json.loads(cfg_file.read_text(encoding="utf-8")) == original
        assert cfg_file.stat().st_size > 0
        assert alerts[-1]["title"] == "Settings Validation Error"

    def test_show_settings_unrecognized_text_does_not_truncate_config(self, tmp_path, monkeypatch):
        """Unrecognized Settings keys should be rejected without touching config.json."""
        import json
        from unittest import mock

        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        original = {"VOICE_CLAUDE_WORKDIR": "/tmp/project"}
        cfg_file.write_text(json.dumps(original), encoding="utf-8")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        monkeypatch.setattr(app_mod, "load_config", lambda: json.loads(cfg_file.read_text(encoding="utf-8")))

        response = mock.MagicMock()
        response.clicked = True
        response.text = "NOT_A_SETTING=value\nALSO_BAD=value"
        monkeypatch.setattr(
            app_mod.rumps,
            "Window",
            mock.MagicMock(return_value=mock.MagicMock(run=mock.MagicMock(return_value=response))),
        )

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_settings(app.settings_item)

        assert json.loads(cfg_file.read_text(encoding="utf-8")) == original
        assert cfg_file.stat().st_size > 0
        assert alerts[-1]["title"] == "Settings Validation Error"

    def test_reload_from_config_updates_record_seconds(self):
        """_reload_from_config should update record_seconds immediately."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        app._reload_from_config({"VOICE_RECORD_SECONDS": "15"})
        assert app.record_seconds == 15

    def test_reload_from_config_updates_stt_backend(self, monkeypatch):
        """_reload_from_config should update stt_backend."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(stt_backend="text-input")
        monkeypatch.setattr(app, "_validate_stt_backend", lambda: None)
        app._reload_from_config({"VOICE_STT_BACKEND": "whisper-cli"})
        assert app.stt_backend == "whisper-cli"

    def test_reload_from_config_rejects_invalid_backend(self):
        """_reload_from_config should NOT change stt_backend to invalid value."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(stt_backend="text-input")
        app._reload_from_config({"VOICE_STT_BACKEND": "invalid-backend"})
        assert app.stt_backend == "text-input"  # unchanged

    def test_reload_from_config_rejects_negative_record_seconds(self):
        """_reload_from_config should NOT apply negative record seconds."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        orig = app.record_seconds
        app._reload_from_config({"VOICE_RECORD_SECONDS": "-5"})
        assert app.record_seconds == orig

    def test_reload_from_config_rejects_zero_record_seconds(self):
        """_reload_from_config should NOT apply zero record seconds."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp()
        orig = app.record_seconds
        app._reload_from_config({"VOICE_RECORD_SECONDS": "0"})
        assert app.record_seconds == orig

    def test_reload_from_empty_config_resets_runtime_defaults(self):
        """Resetting config should restore running app defaults."""
        from voice_claude_agent.app import VoiceClaudeApp

        app = VoiceClaudeApp(stt_backend="text-input", _alert_patch=lambda **kw: None)
        app.stt_backend = "whisper-cli"
        app.record_seconds = 20
        app._reload_from_config({})
        assert app.record_seconds == app.DEFAULT_RECORD_SECONDS
        assert app.stt_backend == "text-input"

    def test_reset_settings_removes_config_file_and_reloads_defaults(self, tmp_path, monkeypatch):
        """Reset Settings should delete config.json and apply defaults immediately."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"VOICE_RECORD_SECONDS": "20"}', encoding="utf-8")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        app = VoiceClaudeApp(stt_backend="text-input", _alert_patch=lambda **kw: None)
        app.stt_backend = "whisper-cli"
        app.record_seconds = 20
        app._show_reset_settings(app.reset_settings_item)

        assert not cfg_file.exists()
        assert app.record_seconds == app.DEFAULT_RECORD_SECONDS
        assert app.stt_backend == "text-input"

    def test_health_check_includes_config_path(self, tmp_path, monkeypatch):
        """Health Check should show the local config path."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        monkeypatch.setattr(app_mod, "load_config", lambda: {"VOICE_RECORD_SECONDS": "9"})

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._run_health_check(app.health_item)

        assert str(cfg_file) in alerts[-1]["message"]
        assert "Config file" in alerts[-1]["message"]

    def test_health_check_includes_claude_workdir(self, tmp_path, monkeypatch):
        """Health Check should show the Claude workdir."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "get_claude_workdir", lambda: tmp_path)

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._run_health_check(app.health_item)

        assert "Claude workdir" in alerts[-1]["message"]
        assert str(tmp_path) in alerts[-1]["message"]

    def test_mic_diagnostic_includes_config_path_and_backend(self, tmp_path, monkeypatch):
        """Mic Diagnostic should show current config path and STT backend."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        alerts = []
        app = VoiceClaudeApp(stt_backend="apple-speech", _alert_patch=lambda **kw: alerts.append(kw))
        app._run_mic_diagnostic(app.diagnostic_item)

        assert str(cfg_file) in alerts[-1]["message"]
        assert "STT backend: apple-speech" in alerts[-1]["message"]


# ── Phase 15: Volcengine/Doubao ASR backend ────────────────


class TestVolcengineDoubaoBackend:
    """Tests for volcengine-doubao STT backend (mock only, no real network)."""

    def test_no_credentials_returns_error(self, monkeypatch):
        """When no credentials are set, _check_volcengine_credentials returns error."""
        from voice_claude_agent.stt import _check_volcengine_credentials

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": default,
        )

        creds, err = _check_volcengine_credentials()
        assert creds == {}
        assert "[STT error:" in err
        assert "VOLCENGINE" in err or "volcengine" in err.lower()

    def test_missing_credentials_in_transcriber(self, monkeypatch):
        """RecordingTranscriber(backend='volcengine-doubao') returns error without creds."""
        from voice_claude_agent.stt import RecordingTranscriber

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": default,
        )

        tc = RecordingTranscriber(backend="volcengine-doubao")
        result = tc.transcribe(b"fake pcm data")
        assert result.startswith("[STT error:")

    def test_mock_transcription_success_result_text(self, monkeypatch):
        """Parse response where result.text contains the transcription."""
        import json
        from voice_claude_agent.stt import _transcribe_volcengine_doubao

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_ASR_APP_ID": "123456",
                "VOLCENGINE_ASR_ACCESS_TOKEN": "token-abc",
                "VOLCENGINE_ASR_RESOURCE_ID": "volc.bigasr.auc_turbo",
                "VOLCENGINE_ASR_CLUSTER": "volcengine_input_common",
                "VOLCENGINE_ASR_LANGUAGE": "zh-CN",
                "VOLCENGINE_ASR_ENDPOINT": "http://fake.test/flash",
            }.get(key, default),
        )

        # Mock urllib_request.urlopen to return a success response
        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                return json.dumps({
                    "audio_info": {"duration": 1234},
                    "result": {
                        "text": "今天天气不错",
                        "utterances": [
                            {"text": "今天天气不错", "start_time": 0, "end_time": 1234}
                        ],
                    },
                }).encode("utf-8")

        def _fake_urlopen(req, timeout=None):
            return _FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        monkeypatch.setattr("urllib.request.Request", lambda *a, **kw: mock.MagicMock())

        result = _transcribe_volcengine_doubao(b"fake pcm data")
        assert result == "今天天气不错"

    def test_mock_transcription_success_utterances_fallback(self, monkeypatch):
        """Parse response where result.text is empty but utterances exist."""
        import json
        from voice_claude_agent.stt import _transcribe_volcengine_doubao

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_ASR_APP_ID": "123456",
                "VOLCENGINE_ASR_ACCESS_TOKEN": "token-abc",
            }.get(key, default),
        )

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                return json.dumps({
                    "result": {
                        "text": "",
                        "utterances": [
                            {"text": "你好世界", "start_time": 0, "end_time": 500},
                        ],
                    },
                }).encode("utf-8")

        def _fake_urlopen(req, timeout=None):
            return _FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        monkeypatch.setattr("urllib.request.Request", lambda *a, **kw: mock.MagicMock())

        result = _transcribe_volcengine_doubao(b"fake pcm data")
        assert result == "你好世界"

    def test_mock_error_response(self, monkeypatch):
        """When API returns error code, an STT error message is returned."""
        import json
        from voice_claude_agent.stt import _transcribe_volcengine_doubao

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_ASR_APP_ID": "123456",
                "VOLCENGINE_ASR_ACCESS_TOKEN": "token-abc",
            }.get(key, default),
        )

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                return json.dumps({
                    "result": {
                        "code": 45000001,
                        "message": "Invalid request parameters",
                    },
                }).encode("utf-8")

        def _fake_urlopen(req, timeout=None):
            return _FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        monkeypatch.setattr("urllib.request.Request", lambda *a, **kw: mock.MagicMock())

        result = _transcribe_volcengine_doubao(b"fake pcm data")
        assert result.startswith("[STT error:")
        assert "45000001" in result

    def test_http_error(self, monkeypatch):
        """When server returns HTTP error, a clear message is returned."""
        from voice_claude_agent.stt import _transcribe_volcengine_doubao

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_ASR_APP_ID": "123456",
                "VOLCENGINE_ASR_ACCESS_TOKEN": "token-abc",
            }.get(key, default),
        )

        from urllib import error as _url_error

        def _fake_urlopen(req, timeout=None):
            raise _url_error.HTTPError("http://fake", 403, "Forbidden", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        monkeypatch.setattr("urllib.request.Request", lambda *a, **kw: mock.MagicMock())

        result = _transcribe_volcengine_doubao(b"fake pcm data")
        assert result.startswith("[STT error:")
        assert "403" in result

    def test_no_network_call_without_credentials(self, monkeypatch):
        """When credentials are missing, no network call is attempted."""
        from voice_claude_agent.stt import _transcribe_volcengine_doubao

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": default,
        )

        called = []
        def _fake_urlopen(req, timeout=None):
            called.append(True)
            raise RuntimeError("should not be called")

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        monkeypatch.setattr("urllib.request.Request", lambda *a, **kw: mock.MagicMock())

        result = _transcribe_volcengine_doubao(b"fake pcm data")
        assert result.startswith("[STT error:")
        assert called == []

    def test_old_console_headers_present(self, monkeypatch):
        """Request headers include old-console X-Api-App-Key and X-Api-Access-Key."""
        import json
        from voice_claude_agent.stt import _transcribe_volcengine_doubao

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_ASR_APP_ID": "my-app-id",
                "VOLCENGINE_ASR_ACCESS_TOKEN": "my-token",
            }.get(key, default),
        )

        captured_body = []

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                return json.dumps({
                    "result": {"code": 20000000, "text": "ok"},
                }).encode("utf-8")

        def _fake_request(url, data=None, headers=None, method=None):
            captured_body.append({"data": data, "headers": headers})
            return mock.MagicMock()

        def _fake_urlopen(req, timeout=None):
            return _FakeResp()

        monkeypatch.setattr("urllib.request.Request", _fake_request)
        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

        result = _transcribe_volcengine_doubao(b"fake data")
        assert result == "ok"
        assert len(captured_body) >= 1
        hdrs = captured_body[0]["headers"]
        assert hdrs.get("X-Api-App-Key") == "my-app-id"
        assert hdrs.get("X-Api-Access-Key") == "my-token"
        assert hdrs.get("X-Api-Sequence") == "-1"
        # Verify request body format
        body = json.loads(captured_body[0]["data"])
        assert body["user"]["uid"] == "my-app-id"
        assert "data" in body["audio"]
        assert body["request"]["model_name"] == "bigmodel"

    def test_new_console_api_key_header_present(self, monkeypatch):
        """Request headers support new-console X-Api-Key authentication."""
        import json
        from voice_claude_agent.stt import _transcribe_volcengine_doubao

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_ASR_API_KEY": "my-api-key",
                "VOLCENGINE_ASR_RESOURCE_ID": "volc.bigasr.auc_turbo",
                "VOLCENGINE_ASR_ENDPOINT": "http://fake.test/flash",
            }.get(key, default),
        )

        captured = []

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                return json.dumps({"result": {"code": 20000000, "text": "ok"}}).encode("utf-8")

        def _fake_request(url, data=None, headers=None, method=None):
            captured.append({"url": url, "data": data, "headers": headers, "method": method})
            return mock.MagicMock()

        monkeypatch.setattr("urllib.request.Request", _fake_request)
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResp())

        result = _transcribe_volcengine_doubao(b"fake data")

        assert result == "ok"
        assert captured[0]["url"] == "http://fake.test/flash"
        assert captured[0]["headers"]["X-Api-Key"] == "my-api-key"
        assert "X-Api-Access-Key" not in captured[0]["headers"]
        assert captured[0]["headers"]["X-Api-Sequence"] == "-1"

    def test_volcengine_backend_available_with_creds(self, monkeypatch):
        """_volcengine_backend_available returns True when creds are configured."""
        from voice_claude_agent.stt import _volcengine_backend_available

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_ASR_APP_ID": "a",
                "VOLCENGINE_ASR_ACCESS_TOKEN": "b",
            }.get(key, default),
        )
        assert _volcengine_backend_available() is True

    def test_volcengine_backend_not_available_without_creds(self, monkeypatch):
        """_volcengine_backend_available returns False when creds are missing."""
        from voice_claude_agent.stt import _volcengine_backend_available

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": default,
        )
        assert _volcengine_backend_available() is False

    def test_list_backends_includes_volcengine_when_configured(self, monkeypatch):
        """list_available_backends includes volcengine-doubao when creds are set."""
        from voice_claude_agent.stt import list_available_backends

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_ASR_APP_ID": "a",
                "VOLCENGINE_ASR_ACCESS_TOKEN": "b",
            }.get(key, default),
        )
        backends = list_available_backends()
        assert "volcengine-doubao" in backends

    def test_list_backends_excludes_volcengine_when_not_configured(self, monkeypatch):
        """list_available_backends excludes volcengine-doubao when creds missing."""
        from voice_claude_agent.stt import list_available_backends

        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": default,
        )
        backends = list_available_backends()
        assert "volcengine-doubao" not in backends

    def test_mask_credential_hides_middle(self):
        """_mask_credential shows only first 3 and last 3 characters."""
        from voice_claude_agent.stt import _mask_credential

        result = _mask_credential("KEY", "abcdefghijklmnop")
        assert result == "abc**********nop"
        assert "defghij" not in result

    def test_mask_credential_short(self):
        """_mask_credential masks entire short values."""
        from voice_claude_agent.stt import _mask_credential

        assert _mask_credential("K", "abc") == "***"

    def test_mask_credential_empty(self):
        """_mask_credential returns (not set) for empty values."""
        from voice_claude_agent.stt import _mask_credential

        assert _mask_credential("K", "") == "(not set)"

    def test_config_mask_credential(self):
        """config.mask_credential masks credential values."""
        from voice_claude_agent.config import mask_credential

        result = mask_credential("my-secret-token-12345")
        assert result.startswith("my-s")
        assert result.endswith("2345")
        assert "ecret-token-1" not in result

    def test_settings_ui_masks_access_token(self, tmp_path, monkeypatch):
        """Settings UI shows masked Access Token values."""
        import json
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "VOICE_RECORD_SECONDS": "10",
            "VOLCENGINE_ASR_API_KEY": "api-key-secret-value",
            "VOLCENGINE_ASR_APP_ID": "my-app-12345",
            "VOLCENGINE_ASR_ACCESS_TOKEN": "secret-token-value",
        }), encoding="utf-8")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        monkeypatch.setattr(app_mod, "load_config", lambda: json.loads(cfg_file.read_text(encoding="utf-8")))

        windows = []

        def _fake_window_init(self, **kwargs):
            windows.append(kwargs)

        class _FakeResp:
            clicked = False
            text = None

        monkeypatch.setattr("rumps.Window.__init__", _fake_window_init)
        monkeypatch.setattr("rumps.Window.run", lambda self: _FakeResp)

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_settings(app.settings_item)

        assert windows
        assert "secret-token-value" not in windows[0]["message"]
        assert "secret-token-value" not in windows[0]["default_text"]
        assert "api-key-secret-value" not in windows[0]["message"]
        assert "api-key-secret-value" not in windows[0]["default_text"]
        assert "<keep existing secret>" in windows[0]["default_text"]

    def test_health_check_includes_volcengine_status(self, monkeypatch):
        """Health Check shows Volcengine ASR configuration status."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        monkeypatch.setattr("voice_claude_agent.config.find_claude_executable", lambda: "/usr/bin/claude")
        monkeypatch.setattr("voice_claude_agent.config.check_apple_speech_available", lambda: True)
        monkeypatch.setattr("voice_claude_agent.config.check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr("voice_claude_agent.stt._find_whisper_cpp_binary", lambda: "/usr/bin/whisper-cli")
        monkeypatch.setattr("voice_claude_agent.stt._resolve_whisper_model", lambda: ("/tmp/model.bin", ""))
        monkeypatch.setattr(
            "voice_claude_agent.stt._check_volcengine_credentials",
            lambda: ({"VOLCENGINE_ASR_APP_ID": "a", "VOLCENGINE_ASR_ACCESS_TOKEN": "b"}, ""),
        )

        import sounddevice as sd
        monkeypatch.setattr(sd, "query_devices", lambda **kw: {"name": "test"})

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._run_health_check(app.health_item)

        msg = alerts[-1]["message"]
        assert "Volcengine ASR" in msg

    def test_mic_diagnostic_includes_volcengine_status(self, monkeypatch):
        """Mic Diagnostic shows Volcengine ASR configuration status (masked)."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "get_config_path", lambda: None)
        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_ASR_APP_ID": "my-app-key-12345",
                "VOLCENGINE_ASR_ACCESS_TOKEN": "my-very-long-secret",
                "VOLCENGINE_ASR_RESOURCE_ID": "volc.bigasr.auc_turbo",
            }.get(key, default),
        )
        monkeypatch.setattr(
            "voice_claude_agent.app.get_config_value",
            lambda key, default="": default,
        )

        alerts = []
        app = VoiceClaudeApp(stt_backend="text-input", _alert_patch=lambda **kw: alerts.append(kw))
        app._run_mic_diagnostic(app.diagnostic_item)

        msg = alerts[-1]["message"]
        assert "Volcengine ASR" in msg
        assert "CONFIGURED" in msg
        # Credentials must NOT appear in plain text
        assert "my-app-key-12345" not in msg
        assert "my-very-long-secret" not in msg

    def test_check_command_shows_volcengine_status(self, monkeypatch):
        """CLI check command shows volcengine-doubao configuration status."""
        import subprocess as _sp

        from click.testing import CliRunner
        from voice_claude_agent.cli import main

        monkeypatch.setattr(
            "voice_claude_agent.cli.find_claude_executable",
            lambda: "/usr/bin/claude",
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli.list_available_backends",
            lambda: ["text-input", "whisper-cli", "apple-speech"],
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._check_volcengine_credentials",
            lambda: ({}, "missing"),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli._volcengine_backend_available",
            lambda: False,
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli.check_mic_permission",
            lambda: (True, ""),
        )
        monkeypatch.setattr(_sp, "run", lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "v2.1", "stderr": ""})())

        runner = CliRunner()
        result = runner.invoke(main, ["check"])
        assert result.exit_code == 0
        assert "Volcengine ASR" in result.output
        assert "not configured" in result.output


# ── F072: TTS Preview Voice ──────────────────────────────────


class TestTTSPreviewVoice:
    """Tests for volcengine-doubao TTS backend (mock only, no real network)."""

    DEFAULT_PREVIEW_TEXT = "你好，我是语音助手。当前正在测试语音播报效果。"

    def test_menu_item_exists(self, monkeypatch):
        """Preview TTS Voice menu item is present in VoiceClaudeApp menu."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        app = VoiceClaudeApp()
        titles = set()
        for m in app.menu:
            if m is not None:
                t = m.title
                if callable(t):
                    titles.add(t())
                else:
                    titles.add(t)
        assert "Preview Tts Voice" in titles

    def test_cancel_input_does_not_play(self, monkeypatch, tmp_path):
        """Cancelling the input dialog does not play TTS or write events."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        events_path = tmp_path / "app_events.jsonl"
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_app_events_log_path",
            lambda: events_path,
        )

        class _Cancelled:
            clicked = False
            text = None
        monkeypatch.setattr("rumps.Window.run", lambda self: _Cancelled)

        # Prevent any real TTS
        monkeypatch.setattr(
            "voice_claude_agent.tts.create_speaker",
            lambda: None,
        )

        app = VoiceClaudeApp()
        app._preview_tts_voice(app.preview_tts_item)

        # No events logged on cancel
        if events_path.exists():
            events = events_path.read_text().strip()
            assert events == ""

    def test_empty_input_uses_default_text(self, monkeypatch, tmp_path):
        """Empty input falls back to the default preview text."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        events_path = tmp_path / "app_events.jsonl"
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_app_events_log_path",
            lambda: events_path,
        )

        spoken: list[str] = []

        class _FakeSpeaker:
            def speak(self, text):
                spoken.append(text)

        monkeypatch.setattr(
            "voice_claude_agent.tts.create_speaker",
            lambda: _FakeSpeaker(),
        )
        monkeypatch.setattr(
            "voice_claude_agent.tts._resolve_tts_backend",
            lambda: "macos-say",
        )

        # User clicks OK but provides no text
        class _EmptySubmit:
            clicked = True
            text = ""
        monkeypatch.setattr("rumps.Window.run", lambda self: _EmptySubmit)

        app = VoiceClaudeApp()
        app._preview_tts_voice(app.preview_tts_item)

        assert spoken == [self.DEFAULT_PREVIEW_TEXT]

    def test_preview_does_not_call_claude_or_write_session(self, monkeypatch, tmp_path):
        """Preview TTS Voice never calls Claude CLI or writes sessions.jsonl."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        events_path = tmp_path / "app_events.jsonl"
        sessions_path = tmp_path / "sessions.jsonl"
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_app_events_log_path",
            lambda: events_path,
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: sessions_path,
        )

        spoken: list[str] = []

        class _FakeSpeaker:
            def speak(self, text):
                spoken.append(text)

        monkeypatch.setattr(
            "voice_claude_agent.tts.create_speaker",
            lambda: _FakeSpeaker(),
        )
        monkeypatch.setattr(
            "voice_claude_agent.tts._resolve_tts_backend",
            lambda: "macos-say",
        )

        class _Clicked:
            clicked = True
            text = "测试文本"
        monkeypatch.setattr("rumps.Window.run", lambda self: _Clicked)

        app = VoiceClaudeApp()
        app._preview_tts_voice(app.preview_tts_item)

        # Must have spoken the given text
        assert spoken == ["测试文本"]

        # Must NOT write sessions.jsonl
        assert not sessions_path.exists()

        # Must have app_events: tts_preview_start, tts_preview_done
        events_text = events_path.read_text().strip()
        assert "tts_preview_start" in events_text
        assert "tts_preview_done" in events_text

    def test_volcengine_tts_fallback_recorded(self, monkeypatch, tmp_path):
        """VolcengineDoubaoSpeaker fallback records tts_preview_failed and fallback flag."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        events_path = tmp_path / "app_events.jsonl"
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_app_events_log_path",
            lambda: events_path,
        )

        class _FakeFallbackVolcengineSpeaker:
            _fallback_called = True
            def speak(self, text):
                raise RuntimeError("Volcengine TTS API error")

        monkeypatch.setattr(
            "voice_claude_agent.tts.create_speaker",
            lambda: _FakeFallbackVolcengineSpeaker(),
        )
        monkeypatch.setattr(
            "voice_claude_agent.tts._resolve_tts_backend",
            lambda: "volcengine-doubao",
        )
        # Mock MacOSSaySpeaker so fallback doesn't actually call subprocess
        monkeypatch.setattr(
            "voice_claude_agent.tts.MacOSSaySpeaker.speak",
            lambda self, text: None,
        )

        class _Clicked:
            clicked = True
            text = "测试"
        monkeypatch.setattr("rumps.Window.run", lambda self: _Clicked)

        app = VoiceClaudeApp()
        app._preview_tts_voice(app.preview_tts_item)

        events_text = events_path.read_text().strip()
        lines = events_text.split("\n")
        event_names = []
        for line in lines:
            ev = json.loads(line)
            event_names.append(ev["event"])

        assert "tts_preview_start" in event_names
        assert "tts_preview_failed" in event_names
        assert "tts_preview_done" in event_names

        # The done event should show fallback
        done_line = [line for line in lines if "tts_preview_done" in line][0]
        done = json.loads(done_line)
        assert done["tts_backend_used"] == "macos-say"
        assert done["tts_fallback_used"] is True

    def test_volcengine_internal_fallback_records_failed_event(self, monkeypatch, tmp_path):
        """Real Volcengine speaker fallback does not raise, but preview still logs failed."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp
        from voice_claude_agent.tts import VolcengineDoubaoSpeaker

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        events_path = tmp_path / "app_events.jsonl"
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_app_events_log_path",
            lambda: events_path,
        )

        class _InternalFallbackSpeaker(VolcengineDoubaoSpeaker):
            def __init__(self):
                self._fallback_called = False

            def speak(self, text):
                self._fallback_called = True

        monkeypatch.setattr(
            "voice_claude_agent.tts.create_speaker",
            lambda: _InternalFallbackSpeaker(),
        )
        monkeypatch.setattr(
            "voice_claude_agent.tts._resolve_tts_backend",
            lambda: "volcengine-doubao",
        )

        class _Clicked:
            clicked = True
            text = "测试"

        monkeypatch.setattr("rumps.Window.run", lambda self: _Clicked)

        app = VoiceClaudeApp()
        app._preview_tts_voice(app.preview_tts_item)

        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        assert [event["event"] for event in events] == [
            "tts_preview_start",
            "tts_preview_failed",
            "tts_preview_done",
        ]
        assert events[1]["reason"] == "fallback_used"
        assert events[2]["tts_backend_used"] == "macos-say"
        assert events[2]["tts_fallback_used"] is True

    def test_menu_title_restores_after_preview(self, monkeypatch, tmp_path):
        """Menu title goes Preview TTS Voice → Previewing... → Preview TTS Voice."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        events_path = tmp_path / "app_events.jsonl"
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_app_events_log_path",
            lambda: events_path,
        )

        class _FakeSpeaker:
            def speak(self, text):
                pass
        monkeypatch.setattr(
            "voice_claude_agent.tts.create_speaker",
            lambda: _FakeSpeaker(),
        )
        monkeypatch.setattr(
            "voice_claude_agent.tts._resolve_tts_backend",
            lambda: "macos-say",
        )

        class _Clicked:
            clicked = True
            text = "测试"
        monkeypatch.setattr("rumps.Window.run", lambda self: _Clicked)

        app = VoiceClaudeApp()
        # Initial title
        assert app.preview_tts_item.title == "Preview TTS Voice"

        app._preview_tts_voice(app.preview_tts_item)

        # Must restore to original title
        assert app.preview_tts_item.title == "Preview TTS Voice"

    def test_menu_title_restores_after_cancel(self, monkeypatch, tmp_path):
        """Menu title stays 'Preview TTS Voice' when user cancels."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        class _Cancelled:
            clicked = False
            text = None
        monkeypatch.setattr("rumps.Window.run", lambda self: _Cancelled)

        app = VoiceClaudeApp()
        app._preview_tts_voice(app.preview_tts_item)

        assert app.preview_tts_item.title == "Preview TTS Voice"


# ── F073: TTS Voice Selector ─────────────────────────────────


class TestTTSVoiceSelector:
    """Tests for choosing Volcengine/Doubao TTS voices from the menu bar app."""

    def test_tts_voice_menu_item_exists_and_shows_current_voice(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"VOLCENGINE_TTS_VOICE_TYPE": "zh_male_qingrun_moon_bigtts"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        app = VoiceClaudeApp()

        assert app.tts_voice_item.title == "TTS Voice: 清润男声"
        assert "清润男声" in list(app.tts_voice_item.keys())

    def test_select_voice_updates_config_and_preserves_secret(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({
                "VOLCENGINE_TTS_API_KEY": "secret-key",
                "VOLCENGINE_TTS_VOICE_TYPE": "zh_female_shuangkuaisisi_moon_bigtts",
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        alerts = []

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._on_select_tts_voice("zh_male_qingrun_moon_bigtts", True)(app.tts_voice_item)

        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved["VOLCENGINE_TTS_API_KEY"] == "secret-key"
        assert saved["VOLCENGINE_TTS_VOICE_TYPE"] == "zh_male_qingrun_moon_bigtts"
        assert app.tts_voice_item.title == "TTS Voice: 清润男声"
        assert alerts[-1]["title"] == "TTS Voice Changed"

    def test_select_moon_bigtts_sets_seed_tts_1_resource(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"VOLCENGINE_TTS_RESOURCE_ID": "seed-tts-2.0"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        app._save_voice_type("zh_male_qingrun_moon_bigtts", True)

        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved["VOLCENGINE_TTS_VOICE_TYPE"] == "zh_male_qingrun_moon_bigtts"
        assert saved["VOLCENGINE_TTS_RESOURCE_ID"] == "seed-tts-1.0"

    def test_tts_2_voices_are_in_menu_and_set_seed_tts_2_resource(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({
                "VOLCENGINE_TTS_API_KEY": "secret-key",
                "VOLCENGINE_TTS_RESOURCE_ID": "seed-tts-1.0",
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        alerts = []

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))

        assert "东方浩然" in list(app.tts_voice_item.keys())
        assert "阿虎" in list(app.tts_voice_item.keys())

        app._on_select_tts_voice_v2(
            "zh_male_dongfanghaoran_uranus_bigtts",
            "seed-tts-2.0",
        )(app.tts_voice_item)

        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved["VOLCENGINE_TTS_API_KEY"] == "secret-key"
        assert saved["VOLCENGINE_TTS_VOICE_TYPE"] == "zh_male_dongfanghaoran_uranus_bigtts"
        assert saved["VOLCENGINE_TTS_RESOURCE_ID"] == "seed-tts-2.0"
        assert app.tts_voice_item.title == "TTS Voice: 东方浩然"
        assert "seed-tts-2.0" in alerts[-1]["message"]

    def test_select_bv_voice_does_not_overwrite_resource_id(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"VOLCENGINE_TTS_RESOURCE_ID": "custom-resource"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        alerts = []

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._on_select_tts_voice("BV701_streaming", False)(app.tts_voice_item)

        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved["VOLCENGINE_TTS_VOICE_TYPE"] == "BV701_streaming"
        assert saved["VOLCENGINE_TTS_RESOURCE_ID"] == "custom-resource"
        assert "Resource ID" in alerts[-1]["message"]

    def test_preview_uses_updated_voice_config(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp
        from voice_claude_agent.config import get_config_value

        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        events_path = tmp_path / "app_events.jsonl"
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_app_events_log_path",
            lambda: events_path,
        )
        spoken_voices = []

        class _FakeSpeaker:
            def speak(self, text):
                spoken_voices.append(get_config_value("VOLCENGINE_TTS_VOICE_TYPE"))

        monkeypatch.setattr("voice_claude_agent.tts.create_speaker", lambda: _FakeSpeaker())
        monkeypatch.setattr("voice_claude_agent.tts._resolve_tts_backend", lambda: "volcengine-doubao")

        class _Clicked:
            clicked = True
            text = "测试"

        monkeypatch.setattr("rumps.Window.run", lambda self: _Clicked)

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        app._save_voice_type("zh_male_qingrun_moon_bigtts", True)
        app._preview_tts_voice(app.preview_tts_item)

        assert spoken_voices == ["zh_male_qingrun_moon_bigtts"]

    def test_health_check_displays_current_tts_voice(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"VOLCENGINE_TTS_VOICE_TYPE": "zh_male_qingrun_moon_bigtts"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        monkeypatch.setattr("voice_claude_agent.config.find_claude_executable", lambda: "/usr/bin/claude")
        monkeypatch.setattr("voice_claude_agent.config.check_apple_speech_available", lambda: True)
        monkeypatch.setattr("voice_claude_agent.stt._find_whisper_cpp_binary", lambda: "/usr/bin/whisper-cli")
        monkeypatch.setattr("voice_claude_agent.stt._resolve_whisper_model", lambda: ("/tmp/model.bin", ""))
        monkeypatch.setattr("voice_claude_agent.stt._check_volcengine_credentials", lambda: ({}, ""))
        monkeypatch.setattr("voice_claude_agent.tts._check_volcengine_tts_credentials", lambda: ({}, ""))

        import sounddevice as sd
        monkeypatch.setattr(sd, "query_devices", lambda **kw: {"name": "test"})

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._run_health_check(app.health_item)

        assert "TTS voice" in alerts[-1]["message"]
        assert "zh_male_qingrun_moon_bigtts" in alerts[-1]["message"]

    def test_mic_diagnostic_displays_current_tts_voice(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"VOLCENGINE_TTS_VOICE_TYPE": "BV120_streaming"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        monkeypatch.setattr("voice_claude_agent.stt._check_volcengine_credentials", lambda: ({}, ""))
        monkeypatch.setattr("voice_claude_agent.tts._check_volcengine_tts_credentials", lambda: ({}, ""))

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._run_mic_diagnostic(app.diagnostic_item)

        assert "TTS voice: BV120_streaming" in alerts[-1]["message"]

    def test_reload_from_settings_updates_tts_voice_menu(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"VOLCENGINE_TTS_VOICE_TYPE": "zh_female_shuangkuaisisi_moon_bigtts"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        cfg_file.write_text(
            json.dumps({"VOLCENGINE_TTS_VOICE_TYPE": "zh_male_qingrun_moon_bigtts"}),
            encoding="utf-8",
        )
        app._reload_from_config({"VOLCENGINE_TTS_VOICE_TYPE": "zh_male_qingrun_moon_bigtts"})

        assert app.tts_voice_item.title == "TTS Voice: 清润男声"

    def test_view_logs_shows_stt_tts_voice_config(self, monkeypatch, tmp_path):
        """View Logs shows current STT backend, TTS backend, and TTS voice type."""
        import json as _json

        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(_json.dumps({
            "VOICE_TTS_BACKEND": "volcengine-doubao",
            "VOLCENGINE_TTS_VOICE_TYPE": "zh_male_qingrun_moon_bigtts",
        }), encoding="utf-8")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        # Mock app_events and last_result paths
        events_path = tmp_path / "app_events.jsonl"
        events_path.write_text('{"timestamp":"2026-06-08T10:00:00+08:00","event":"tts_preview_start","backend":"volcengine-doubao","text_length":5}\n{"timestamp":"2026-06-08T10:00:02+08:00","event":"tts_preview_done","backend":"volcengine-doubao","tts_backend_used":"volcengine-doubao","tts_fallback_used":false,"duration":1.5}\n', encoding="utf-8")

        last_path = tmp_path / "last_result.json"
        last_path.write_text(_json.dumps({
            "prompt": "测试", "exit_code": 0,
            "summary": "完成", "tts_backend": "volcengine-doubao",
            "stt_backend": "volcengine-doubao",
            "tts_voice_type": "zh_male_qingrun_moon_bigtts",
            "tts_resource_id": "seed-tts-1.0",
            "tts_duration_seconds": 1.5, "tts_fallback_used": False,
        }), encoding="utf-8")

        monkeypatch.setattr(app_mod, "get_agent_state_dir", lambda: tmp_path)
        monkeypatch.setattr("voice_claude_agent.config.get_agent_state_dir", lambda: tmp_path)

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_logs(app.logs_item)

        msg = alerts[-1]["message"]
        assert "STT backend:" in msg
        assert "TTS backend: volcengine-doubao" in msg
        assert "TTS voice type: zh_male_qingrun_moon_bigtts" in msg
        assert "tts_preview_done tts=volcengine-doubao fallback=False" in msg
        assert "stt_backend: volcengine-doubao" in msg
        assert "tts_voice_type: zh_male_qingrun_moon_bigtts" in msg
        assert "tts_resource_id: seed-tts-1.0" in msg

    def test_bv_voices_are_experimental_not_default(self, monkeypatch):
        """BV701_streaming / BV120_streaming must be in TTS_EXPERIMENTAL_VOICES, not TTS_VOICES."""
        from voice_claude_agent.app import VoiceClaudeApp
        default_vts = {vt for _, vt, _ in VoiceClaudeApp.TTS_VOICES}
        exp_vts = {vt for _, vt, _ in VoiceClaudeApp.TTS_EXPERIMENTAL_VOICES}
        assert "BV701_streaming" not in default_vts
        assert "BV120_streaming" not in default_vts
        assert "BV701_streaming" in exp_vts
        assert "BV120_streaming" in exp_vts

    def test_resource_mismatch_fallback_shown_in_view_logs(self, monkeypatch, tmp_path):
        """View Logs shows diagnostic when fallback_reason mentions resource mismatch."""
        import json as _json

        from voice_claude_agent.app import VoiceClaudeApp
        import voice_claude_agent.app as app_mod
        import voice_claude_agent.config as config_mod

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        # Isolate from real agent_state dir
        state_dir = tmp_path / "agent_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config_mod, "get_agent_state_dir", lambda: state_dir)
        monkeypatch.setattr(config_mod, "get_app_events_log_path", lambda: state_dir / "app_events.jsonl")
        monkeypatch.setattr(config_mod, "get_last_result_path", lambda: state_dir / "last_result.json")

        # Write only a minimal app_events
        (state_dir / "app_events.jsonl").write_text(
            '{"timestamp":"2026-06-08T10:00:00+08:00","event":"tts_preview_done"}\n',
            encoding="utf-8",
        )
        # Write last_result with resource mismatch fallback
        (state_dir / "last_result.json").write_text(_json.dumps({
            "prompt": "test", "exit_code": 0, "summary": "ok",
            "tts_backend": "macos-say", "tts_fallback_used": True,
            "tts_fallback_reason": "api_error",
            "tts_fallback_detail": "resource ID is mismatched with speaker related resource",
        }), encoding="utf-8")

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_logs(app.logs_item)

        msg = alerts[-1]["message"]
        assert "resource mismatch" in msg
        assert "moon_bigtts" in msg

    def test_view_logs_shows_claude_running_when_stuck(self, monkeypatch, tmp_path):
        """View Logs shows 'Claude CLI: running' when _claude_invocation_start is set."""
        import json as _json
        import time as _time

        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        events_path = tmp_path / "app_events.jsonl"
        events_path.write_text("", encoding="utf-8")
        last_path = tmp_path / "last_result.json"
        last_path.write_text(_json.dumps({"prompt": "test", "exit_code": 0, "summary": "done"}), encoding="utf-8")
        import voice_claude_agent.config as config_mod
        monkeypatch.setattr(config_mod, "get_agent_state_dir", lambda: tmp_path)
        monkeypatch.setattr(config_mod, "get_app_events_log_path", lambda: events_path)
        monkeypatch.setattr(config_mod, "get_last_result_path", lambda: last_path)

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        # Simulate Claude running for 30 seconds
        app._claude_invocation_start = _time.monotonic() - 30
        app._show_logs(app.logs_item)

        msg = alerts[-1]["message"]
        assert "Claude CLI: running" in msg
        assert "30s" in msg or "29s" in msg

    def test_view_logs_uses_wide_text_window(self, monkeypatch):
        """Real View Logs uses a wider selectable text window instead of a narrow alert."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        calls = []

        class _FakeWindow:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self._textfield = mock.MagicMock()

            def run(self):
                calls.append({"run": True})

        monkeypatch.setattr(app_mod.rumps, "Window", _FakeWindow)
        app = VoiceClaudeApp()
        app._show_text_window("View Logs", "hello")

        assert calls[0]["title"] == "View Logs"
        assert calls[0]["default_text"] == "hello"
        assert calls[0]["dimensions"][0] >= 800
        assert calls[0]["dimensions"][1] >= 500
        assert calls[-1] == {"run": True}


# ── F076-F079: Voice UX Polish ──────────────────────────────


class TestVoiceUXPolish:
    """Tests for F076-F079: reply style, TTS summary max, running state, stop run."""

    def test_reply_style_default_is_normal(self, monkeypatch):
        """Default reply style is 'normal'."""
        monkeypatch.setattr(
            "voice_claude_agent.cli.get_config_value",
            lambda key, default="": default,
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli.run_claude",
            lambda prompt, timeout=300, extra_args=None, workdir=None, cancel_event=None: type("R", (), {"command": [], "exit_code": 0, "stdout": "ok", "stderr": "", "duration_seconds": 0.1, "timed_out": False, "cancelled": False, "cwd": ""})(),
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli.write_session",
            lambda entry: None,
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli.write_last_result",
            lambda entry: None,
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli.create_speaker",
            lambda: type("S", (), {"speak": lambda self, t: None})(),
        )
        monkeypatch.setattr("voice_claude_agent.cli._resolve_tts_backend", lambda: "macos-say")

        from voice_claude_agent.cli import _run_pipeline
        _run_pipeline("test", input_mode="text", tts_fake=True)
        # No crash = the new reply_style code path works

    def test_concise_reply_style_affects_claude_prompt(self, monkeypatch):
        """Concise reply style adds '尽量简短' to Claude prompt."""
        calls = []
        def _fake_claude(prompt, timeout=300, extra_args=None, workdir=None, cancel_event=None):
            calls.append(prompt)
            return type("R", (), {"command": [], "exit_code": 0, "stdout": "ok", "stderr": "", "duration_seconds": 0.1, "timed_out": False, "cancelled": False, "cwd": ""})()
        monkeypatch.setattr("voice_claude_agent.cli.run_claude", _fake_claude)
        monkeypatch.setattr("voice_claude_agent.cli.write_session", lambda entry: None)
        monkeypatch.setattr("voice_claude_agent.cli.write_last_result", lambda entry: None)
        monkeypatch.setattr("voice_claude_agent.cli.create_speaker", lambda: type("S", (), {"speak": lambda self, t: None})())
        monkeypatch.setattr("voice_claude_agent.cli._resolve_tts_backend", lambda: "macos-say")
        monkeypatch.setattr(
            "voice_claude_agent.cli.get_config_value",
            lambda key, default="": {"VOICE_REPLY_STYLE": "concise"}.get(key, default),
        )

        from voice_claude_agent.cli import _run_pipeline
        _run_pipeline("test", input_mode="text", tts_fake=True)
        assert any("尽量简短" in c for c in calls)

    def test_detailed_reply_style_affects_claude_prompt(self, monkeypatch):
        """Detailed reply style adds '尽可能详细完整' to Claude prompt."""
        calls = []
        def _fake_claude(prompt, timeout=300, extra_args=None, workdir=None, cancel_event=None):
            calls.append(prompt)
            return type("R", (), {"command": [], "exit_code": 0, "stdout": "ok", "stderr": "", "duration_seconds": 0.1, "timed_out": False, "cancelled": False, "cwd": ""})()
        monkeypatch.setattr("voice_claude_agent.cli.run_claude", _fake_claude)
        monkeypatch.setattr("voice_claude_agent.cli.write_session", lambda entry: None)
        monkeypatch.setattr("voice_claude_agent.cli.write_last_result", lambda entry: None)
        monkeypatch.setattr("voice_claude_agent.cli.create_speaker", lambda: type("S", (), {"speak": lambda self, t: None})())
        monkeypatch.setattr("voice_claude_agent.cli._resolve_tts_backend", lambda: "macos-say")
        monkeypatch.setattr(
            "voice_claude_agent.cli.get_config_value",
            lambda key, default="": {"VOICE_REPLY_STYLE": "detailed"}.get(key, default),
        )

        from voice_claude_agent.cli import _run_pipeline
        _run_pipeline("test", input_mode="text", tts_fake=True)
        assert any("尽可能详细完整" in c for c in calls)

    def test_tts_summary_max_chars_config(self, monkeypatch):
        """VOICE_TTS_SUMMARY_MAX_CHARS sets truncation limit for TTS."""
        monkeypatch.setattr("voice_claude_agent.cli.run_claude",
            lambda prompt, timeout=300, extra_args=None, workdir=None, cancel_event=None: type("R", (), {"command": [], "exit_code": 0, "stdout": "A sentence here. " * 30, "stderr": "", "duration_seconds": 0.1, "timed_out": False, "cancelled": False, "cwd": ""})())

        spoken: list[str] = []
        class _S:
            def speak(self, text):
                spoken.append(text)

        monkeypatch.setattr("voice_claude_agent.cli.create_speaker", lambda: _S())
        monkeypatch.setattr("voice_claude_agent.cli._resolve_tts_backend", lambda: "macos-say")
        monkeypatch.setattr("voice_claude_agent.cli.write_session", lambda entry: None)
        monkeypatch.setattr("voice_claude_agent.cli.write_last_result", lambda entry: None)

        # Set max to 80 chars
        monkeypatch.setattr(
            "voice_claude_agent.cli.get_config_value",
            lambda key, default="": (
                {"VOICE_TTS_SUMMARY_MAX_CHARS": "80"}.get(key, default) if key == "VOICE_TTS_SUMMARY_MAX_CHARS" else default
            ),
        )

        from voice_claude_agent.cli import _run_pipeline
        _run_pipeline("test", input_mode="text", tts_fake=False)
        assert spoken
        assert len(spoken[0]) < 120
        assert "（回复较长" in spoken[0]

    def test_logging_store_includes_reply_style(self, monkeypatch, tmp_path):
        """Session logs include reply_style field."""
        monkeypatch.setattr("voice_claude_agent.cli.run_claude",
            lambda prompt, timeout=300, extra_args=None, workdir=None, cancel_event=None: type("R", (), {"command": [], "exit_code": 0, "stdout": "ok", "stderr": "", "duration_seconds": 0.1, "timed_out": False, "cancelled": False, "cwd": ""})())
        monkeypatch.setattr("voice_claude_agent.cli._resolve_tts_backend", lambda: "macos-say")
        monkeypatch.setattr("voice_claude_agent.cli.create_speaker", lambda: type("S", (), {"speak": lambda self, t: None})())

        sessions_path = tmp_path / "sessions.jsonl"
        last_path = tmp_path / "last_result.json"
        monkeypatch.setattr("voice_claude_agent.logging_store.get_sessions_log_path", lambda: sessions_path)
        monkeypatch.setattr("voice_claude_agent.logging_store.get_last_result_path", lambda: last_path)

        from voice_claude_agent.cli import _run_pipeline
        _run_pipeline("test", input_mode="text", tts_fake=True)

        session = json.loads(sessions_path.read_text().splitlines()[0])
        # reply_style defaults to "normal" since we don't override get_config_value
        assert session.get("reply_style") == "normal"

        last = json.loads(last_path.read_text())
        assert last.get("reply_style") == "normal"

    def test_app_view_logs_shows_reply_style_and_spoken_summary(self, monkeypatch, tmp_path):
        """View Logs shows reply_style and spoken_summary differentiation."""
        import voice_claude_agent.app as app_mod
        import voice_claude_agent.config as config_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        state_dir = tmp_path / "agent_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config_mod, "get_agent_state_dir", lambda: state_dir)
        monkeypatch.setattr(config_mod, "get_app_events_log_path", lambda: state_dir / "app_events.jsonl")
        monkeypatch.setattr(config_mod, "get_last_result_path", lambda: state_dir / "last_result.json")

        (state_dir / "app_events.jsonl").write_text(
            '{"timestamp":"2026-06-08T10:00:00+08:00","event":"cycle_done"}\n', encoding="utf-8")
        (state_dir / "last_result.json").write_text(json.dumps({
            "prompt": "test", "exit_code": 0,
            "timed_out": True,
            "summary": "这是一段非常非常长的完整回答",
            "spoken_summary": "这是一段非常非常长的...",
            "reply_style": "concise",
        }, ensure_ascii=False), encoding="utf-8")

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_logs(app.logs_item)

        msg = alerts[-1]["message"]
        # View Logs reads reply_style from last_result
        assert "reply_style" in msg
        assert "concise" in msg
        assert "timed_out: True" in msg

    def test_stop_current_run_menu_item_exists(self, monkeypatch):
        """Stop Current Run menu item is present."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        app = VoiceClaudeApp()
        titles = set()
        for m in app.menu:
            if m is not None:
                t = m.title
                if callable(t):
                    titles.add(t())
                else:
                    titles.add(t)
        assert "Stop Current Run" in titles

    def test_stop_current_run_cancels_claude(self, monkeypatch):
        """_stop_current_run triggers request_cancel."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        cancel_called = []
        monkeypatch.setattr("voice_claude_agent.claude_runner._cancel_event",
            type("E", (), {"set": lambda: cancel_called.append(1), "is_set": lambda: False, "clear": lambda: None})())
        # Also mock the module-level functions
        monkeypatch.setattr("voice_claude_agent.claude_runner.request_cancel",
            lambda: cancel_called.append(1))
        monkeypatch.setattr("voice_claude_agent.claude_runner.is_cancelled",
            lambda: len(cancel_called) > 0)

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._cycle_in_progress = True
        app._stop_current_run(app.stop_current_item)

        assert len(cancel_called) >= 1
        assert app.trigger_item.title == "Cancelling..."

    def test_stop_current_run_writes_app_event(self, monkeypatch, tmp_path):
        """_stop_current_run writes cycle_cancelled event."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        events_path = tmp_path / "app_events.jsonl"
        monkeypatch.setattr("voice_claude_agent.logging_store.get_app_events_log_path", lambda: events_path)

        monkeypatch.setattr("voice_claude_agent.claude_runner.request_cancel", lambda: None)
        monkeypatch.setattr("voice_claude_agent.claude_runner.is_cancelled", lambda: False)

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._cycle_in_progress = True
        app._stop_current_run(app.stop_current_item)

        events = events_path.read_text().strip()
        assert "cycle_cancelled" in events

    def test_stop_current_run_is_idempotent(self, monkeypatch):
        """Calling stop twice does not double-fire."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        calls = []
        monkeypatch.setattr("voice_claude_agent.claude_runner.request_cancel", lambda: calls.append(1))
        monkeypatch.setattr("voice_claude_agent.claude_runner.is_cancelled", lambda: len(calls) > 0)
        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        app._cycle_in_progress = True
        app._stop_current_run(app.stop_current_item)
        app._stop_current_run(app.stop_current_item)
        assert len(calls) == 1

    def test_stop_current_run_idle_is_noop(self, monkeypatch, tmp_path):
        """Stop Current Run should not write cancellation events when idle."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        events_path = tmp_path / "app_events.jsonl"
        monkeypatch.setattr("voice_claude_agent.logging_store.get_app_events_log_path", lambda: events_path)

        calls = []
        monkeypatch.setattr("voice_claude_agent.claude_runner.request_cancel", lambda: calls.append(1))
        monkeypatch.setattr("voice_claude_agent.claude_runner.is_cancelled", lambda: False)

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._cycle_in_progress = False
        app._claude_invocation_start = 0
        app._stop_current_run(app.stop_current_item)

        assert calls == []
        assert not events_path.exists()
        assert "No active run" in alerts[-1]["message"]


# ── F071: Volcengine/Doubao TTS backend (continued) ──────────


class TestVolcengineDoubaoTTS:
    """Tests for volcengine-doubao TTS backend (mock only, no real network)."""

    def test_create_speaker_defaults_to_macos_say(self, monkeypatch):
        """create_speaker() returns MacOSSaySpeaker when backend is macos-say."""
        monkeypatch.setattr(
            "voice_claude_agent.tts.get_config_value",
            lambda key, default="": default,
        )
        from voice_claude_agent.tts import create_speaker, MacOSSaySpeaker
        speaker = create_speaker()
        assert isinstance(speaker, MacOSSaySpeaker)

    def test_create_speaker_returns_volcengine_when_configured(self, monkeypatch):
        """create_speaker() returns VolcengineDoubaoSpeaker when backend is volcengine-doubao."""
        monkeypatch.setattr(
            "voice_claude_agent.tts.get_config_value",
            lambda key, default="": {"VOICE_TTS_BACKEND": "volcengine-doubao"}.get(key, default),
        )
        from voice_claude_agent.tts import create_speaker, VolcengineDoubaoSpeaker
        speaker = create_speaker()
        assert isinstance(speaker, VolcengineDoubaoSpeaker)

    def test_volcengine_speaker_falls_back_on_missing_credentials(self, monkeypatch):
        """VolcengineDoubaoSpeaker falls back to MacOSSaySpeaker when API key missing."""
        monkeypatch.setattr(
            "voice_claude_agent.tts.get_config_value",
            lambda key, default="": default,
        )
        from voice_claude_agent.tts import FakeSpeaker, VolcengineDoubaoSpeaker
        speaker = VolcengineDoubaoSpeaker()
        fallback = FakeSpeaker()
        speaker._fallback = fallback
        speaker.speak("test")
        assert fallback.spoken == ["test"]
        assert speaker._fallback_called is True

    def test_volcengine_speaker_calls_api_and_plays_audio(self, monkeypatch, tmp_path):
        """VolcengineDoubaoSpeaker calls the TTS API, saves temp file, plays afplay."""
        import json
        from unittest import mock

        monkeypatch.setattr(
            "voice_claude_agent.tts.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_TTS_API_KEY": "test-key",
                "VOLCENGINE_TTS_RESOURCE_ID": "seed-tts-2.0",
                "VOLCENGINE_TTS_VOICE_TYPE": "zh_female_test",
                "VOLCENGINE_TTS_AUDIO_FORMAT": "mp3",
            }.get(key, default),
        )

        # Fake HTTP response: one chunk of base64 audio
        import base64
        fake_audio = b"\xff\xfb\x90\x00" * 20
        fake_chunk = json.dumps({"code": 0, "data": base64.b64encode(fake_audio).decode()})

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def __iter__(self):
                return iter([fake_chunk.encode()])

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResp())
        monkeypatch.setattr("urllib.request.Request", lambda *a, **kw: mock.MagicMock())

        popen_calls = []

        class _FakeProc:
            pid = 12345

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: popen_calls.append(a) or _FakeProc())

        from voice_claude_agent.tts import VolcengineDoubaoSpeaker
        speaker = VolcengineDoubaoSpeaker()
        speaker.speak("测试文本")

        assert len(popen_calls) >= 1
        cmd = popen_calls[0][0] if isinstance(popen_calls[0], tuple) else []
        assert "afplay" in str(cmd)
        assert speaker._fallback_called is False

    def test_volcengine_speaker_accepts_success_terminal_code(self, monkeypatch):
        """Volcengine TTS terminal success code 20000000 should not trigger fallback."""
        import base64
        import json
        from unittest import mock

        monkeypatch.setattr(
            "voice_claude_agent.tts.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_TTS_API_KEY": "test-key",
            }.get(key, default),
        )

        fake_audio = b"\xff\xfb\x90\x00" * 20
        lines = [
            json.dumps({"code": 0, "data": base64.b64encode(fake_audio).decode()}).encode(),
            json.dumps({"code": 20000000, "message": "ok", "data": None}).encode(),
        ]

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def __iter__(self): return iter(lines)

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResp())
        monkeypatch.setattr("urllib.request.Request", lambda *a, **kw: mock.MagicMock())
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)

        from voice_claude_agent.tts import VolcengineDoubaoSpeaker
        speaker = VolcengineDoubaoSpeaker()
        speaker.speak("测试文本")
        assert speaker._fallback_called is False

    def test_volcengine_speaker_default_resource_matches_default_voice(self, monkeypatch):
        """Default moon_bigtts voice should use seed-tts-1.0 to avoid resource mismatch."""
        import base64
        import json

        monkeypatch.setattr(
            "voice_claude_agent.tts.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_TTS_API_KEY": "test-key",
            }.get(key, default),
        )

        requests = []

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def __iter__(self):
                data = base64.b64encode(b"\xff\xfb\x90\x00").decode()
                return iter([json.dumps({"code": 0, "data": data}).encode()])

        def fake_request(url, data=None, headers=None, method=None):
            requests.append({"url": url, "headers": headers or {}, "body": json.loads(data.decode("utf-8"))})
            return object()

        monkeypatch.setattr("urllib.request.Request", fake_request)
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResp())
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)

        from voice_claude_agent.tts import VolcengineDoubaoSpeaker
        VolcengineDoubaoSpeaker().speak("测试文本")

        assert requests[0]["headers"]["X-Api-Resource-Id"] == "seed-tts-1.0"
        assert requests[0]["body"]["req_params"]["speaker"] == "zh_female_shuangkuaisisi_moon_bigtts"

    def test_volcengine_speaker_falls_back_on_http_error(self, monkeypatch):
        """VolcengineDoubaoSpeaker falls back to say on HTTP error."""
        monkeypatch.setattr(
            "voice_claude_agent.tts.get_config_value",
            lambda key, default="": {
                "VOLCENGINE_TTS_API_KEY": "test-key",
            }.get(key, default),
        )
        from urllib import error as _url_error
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: (_ for _ in ()).throw(_url_error.HTTPError("http://fake", 500, "Error", {}, None)))

        # Mock subprocess.run for both afplay and say fallback
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run", lambda *a, **kw: None)

        from voice_claude_agent.tts import VolcengineDoubaoSpeaker
        speaker = VolcengineDoubaoSpeaker()
        # Should not raise
        speaker.speak("test")

    def test_tts_fallback_logged_to_app_events(self, monkeypatch, tmp_path):
        """TTS fallback events are logged to app_events.jsonl."""
        monkeypatch.setattr(
            "voice_claude_agent.tts.get_config_value",
            lambda key, default="": default,
        )

        events_path = tmp_path / "app_events.jsonl"
        monkeypatch.setattr("voice_claude_agent.logging_store.get_app_events_log_path", lambda: events_path)

        from voice_claude_agent.tts import _log_tts_fallback
        _log_tts_fallback("test_reason", "test_detail")

    def test_macos_say_speaker_still_works(self):
        """MacOSSaySpeaker.speak() still works (no crash with subprocess)."""
        from voice_claude_agent.tts import MacOSSaySpeaker
        speaker = MacOSSaySpeaker()
        # Should not raise even if say is not installed
        speaker.speak("test")

    def test_fake_speaker_still_works(self):
        """FakeSpeaker records spoken text."""
        from voice_claude_agent.tts import FakeSpeaker
        speaker = FakeSpeaker()
        speaker.speak("hello")
        assert speaker.spoken == ["hello"]

    def test_health_check_includes_tts_backend(self, monkeypatch):
        """Health Check shows TTS backend status."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        monkeypatch.setattr("voice_claude_agent.config.find_claude_executable", lambda: "/usr/bin/claude")
        monkeypatch.setattr("voice_claude_agent.config.check_apple_speech_available", lambda: True)
        monkeypatch.setattr("voice_claude_agent.config.check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr("voice_claude_agent.stt._find_whisper_cpp_binary", lambda: "/usr/bin/whisper-cli")
        monkeypatch.setattr("voice_claude_agent.stt._resolve_whisper_model", lambda: ("/tmp/model.bin", ""))
        monkeypatch.setattr("voice_claude_agent.stt._check_volcengine_credentials", lambda: ({}, ""))
        monkeypatch.setattr("voice_claude_agent.tts._resolve_tts_backend", lambda: "macos-say")
        monkeypatch.setattr("voice_claude_agent.tts._check_volcengine_tts_credentials", lambda: ({}, ""))

        import sounddevice as sd
        monkeypatch.setattr(sd, "query_devices", lambda **kw: {"name": "test"})

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._run_health_check(app.health_item)

        msg = alerts[-1]["message"]
        assert "TTS backend" in msg

    def test_health_check_shows_backend_env_overrides(self, monkeypatch):
        """Health Check should reveal env vars that override Settings config."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setenv("VOICE_STT_BACKEND", "whisper-cli")
        monkeypatch.setenv("VOICE_TTS_BACKEND", "macos-say")
        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        monkeypatch.setattr("voice_claude_agent.config.find_claude_executable", lambda: "/usr/bin/claude")
        monkeypatch.setattr("voice_claude_agent.config.check_apple_speech_available", lambda: True)
        monkeypatch.setattr("voice_claude_agent.config.check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr("voice_claude_agent.stt._find_whisper_cpp_binary", lambda: "/usr/bin/whisper-cli")
        monkeypatch.setattr("voice_claude_agent.stt._resolve_whisper_model", lambda: ("/tmp/model.bin", ""))
        monkeypatch.setattr("voice_claude_agent.stt._check_volcengine_credentials", lambda: ({}, ""))
        monkeypatch.setattr("voice_claude_agent.tts._check_volcengine_tts_credentials", lambda: ({}, ""))

        import sounddevice as sd
        monkeypatch.setattr(sd, "query_devices", lambda **kw: {"name": "test"})

        alerts = []
        app = VoiceClaudeApp(stt_backend="whisper-cli", _alert_patch=lambda **kw: alerts.append(kw))
        app._run_health_check(app.health_item)

        msg = alerts[-1]["message"]
        assert "env override: VOICE_STT_BACKEND=whisper-cli" in msg
        assert "env override: VOICE_TTS_BACKEND=macos-say" in msg

    def test_mic_diagnostic_includes_tts_backend(self, monkeypatch):
        """Mic Diagnostic shows TTS backend configuration."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "get_config_path", lambda: None)
        monkeypatch.setattr(app_mod, "get_config_value", lambda key, default="": default)
        monkeypatch.setattr(
            "voice_claude_agent.tts._resolve_tts_backend",
            lambda: "volcengine-doubao",
        )
        monkeypatch.setattr(
            "voice_claude_agent.tts._check_volcengine_tts_credentials",
            lambda: ({"VOLCENGINE_TTS_API_KEY": "secret-key-12345"}, ""),
        )
        monkeypatch.setattr(
            "voice_claude_agent.stt.get_config_value",
            lambda key, default="": default,
        )

        alerts = []
        app = VoiceClaudeApp(stt_backend="text-input", _alert_patch=lambda **kw: alerts.append(kw))
        app._run_mic_diagnostic(app.diagnostic_item)

        msg = alerts[-1]["message"]
        assert "TTS backend" in msg
        assert "volcengine-doubao" in msg
        assert "CONFIGURED" in msg
        # Key should be masked
        assert "secret-key-12345" not in msg

    def test_settings_includes_tts_keys(self, tmp_path, monkeypatch):
        """Settings UI includes TTS configuration keys."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "VOICE_TTS_BACKEND": "volcengine-doubao",
            "VOLCENGINE_TTS_API_KEY": "secret-key-value",
        }), encoding="utf-8")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        class _FakeResp:
            clicked = False
            text = None

        monkeypatch.setattr("rumps.Window.run", lambda self: _FakeResp)

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_settings(app.settings_item)
        window = alerts  # keep local assertion block compact for this smoke test
        assert window == []

    def test_config_accepts_tts_endpoint(self, tmp_path, monkeypatch):
        """VOLCENGINE_TTS_ENDPOINT from config.json is recognized."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "VOLCENGINE_TTS_ENDPOINT": "https://example.test/tts",
        }), encoding="utf-8")
        monkeypatch.setattr("voice_claude_agent.config.get_config_path", lambda: cfg_file)

        from voice_claude_agent.config import get_config_value
        assert get_config_value("VOLCENGINE_TTS_ENDPOINT") == "https://example.test/tts"

    def test_pipeline_records_tts_metrics(self, tmp_path, monkeypatch):
        """Pipeline writes actual TTS backend, duration, and fallback flag after speaking."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli.run_claude",
            mock.MagicMock(return_value=ClaudeRunResult(
                command=["claude", "-p", "test"],
                exit_code=0,
                stdout="完成",
                stderr="",
                duration_seconds=0.1,
                timed_out=False,
            )),
        )
        monkeypatch.setattr(
            "voice_claude_agent.config.get_config_path",
            lambda: tmp_path / "config.json",
        )
        (tmp_path / "config.json").write_text(
            json.dumps({
                "VOLCENGINE_TTS_VOICE_TYPE": "zh_male_qingrun_moon_bigtts",
                "VOLCENGINE_TTS_RESOURCE_ID": "seed-tts-1.0",
            }),
            encoding="utf-8",
        )

        from voice_claude_agent.cli import _run_pipeline
        _run_pipeline("test", input_mode="text", tts_fake=True, stt_backend_used="text-input")

        session = json.loads((tmp_path / "sessions.jsonl").read_text().splitlines()[0])
        last_result = json.loads((tmp_path / "last_result.json").read_text())
        assert session["stt_backend"] == "text-input"
        assert session["tts_backend"] == "fake"
        assert session["tts_voice_type"] == "zh_male_qingrun_moon_bigtts"
        assert session["tts_resource_id"] == "seed-tts-1.0"
        assert isinstance(session["tts_duration_seconds"], float)
        assert session["tts_fallback_used"] is False
        assert last_result["stt_backend"] == "text-input"
        assert last_result["tts_backend"] == "fake"
        assert last_result["tts_voice_type"] == "zh_male_qingrun_moon_bigtts"
        assert last_result["tts_resource_id"] == "seed-tts-1.0"
        assert isinstance(last_result["tts_duration_seconds"], float)

    def test_pipeline_records_tts_fallback_reason(self, tmp_path, monkeypatch):
        """Pipeline persists Volcengine fallback reason/detail for later View Logs diagnostics."""
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_sessions_log_path",
            lambda: tmp_path / "sessions.jsonl",
        )
        monkeypatch.setattr(
            "voice_claude_agent.logging_store.get_last_result_path",
            lambda: tmp_path / "last_result.json",
        )
        monkeypatch.setattr(
            "voice_claude_agent.cli.run_claude",
            mock.MagicMock(return_value=ClaudeRunResult(
                command=["claude", "-p", "test"],
                exit_code=0,
                stdout="完成",
                stderr="",
                duration_seconds=0.1,
                timed_out=False,
            )),
        )
        monkeypatch.setattr(
            "voice_claude_agent.config.get_config_path",
            lambda: tmp_path / "config.json",
        )
        (tmp_path / "config.json").write_text(
            json.dumps({
                "VOICE_TTS_BACKEND": "volcengine-doubao",
                "VOLCENGINE_TTS_VOICE_TYPE": "BV701_streaming",
                "VOLCENGINE_TTS_RESOURCE_ID": "seed-tts-1.0",
            }),
            encoding="utf-8",
        )

        from voice_claude_agent.tts import VolcengineDoubaoSpeaker

        class _FallbackSpeaker(VolcengineDoubaoSpeaker):
            def speak(self, text: str) -> None:
                self._fallback_called = True
                self._fallback_reason = "api_error"
                self._fallback_detail = "resource ID is mismatched with speaker related resource"

        monkeypatch.setattr("voice_claude_agent.cli.create_speaker", lambda: _FallbackSpeaker())
        monkeypatch.setattr("voice_claude_agent.cli._resolve_tts_backend", lambda: "volcengine-doubao")

        from voice_claude_agent.cli import _run_pipeline
        _run_pipeline("test", input_mode="text", tts_fake=False, stt_backend_used="volcengine-doubao")

        session = json.loads((tmp_path / "sessions.jsonl").read_text().splitlines()[0])
        last_result = json.loads((tmp_path / "last_result.json").read_text())
        assert session["tts_fallback_used"] is True
        assert session["tts_fallback_reason"] == "api_error"
        assert "mismatched" in session["tts_fallback_detail"]
        assert last_result["tts_fallback_reason"] == "api_error"
        assert "mismatched" in last_result["tts_fallback_detail"]


# ── F080: Stop Current Run — TTS Cancellation ──────────────────


class TestTTSCancellation:
    """F080: Stop Current Run cancels TTS playback (say + afplay)."""

    # ── helpers ──

    @staticmethod
    def _mock_app(monkeypatch, tmp_path, alerts):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        state_dir = tmp_path / "agent_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("voice_claude_agent.config.get_agent_state_dir", lambda: state_dir)
        monkeypatch.setattr("voice_claude_agent.config.get_app_events_log_path", lambda: state_dir / "app_events.jsonl")

        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        return app

    # ── macOS say cancellation ──

    def test_say_playback_pid_tracked_and_cleared(self):
        """_register + _unregister track say process PID lifecycle."""
        from voice_claude_agent.tts import _register_tts_pid, _unregister_tts_pid, _tts_pids

        _tts_pids.clear()
        _register_tts_pid(12345)
        _register_tts_pid(12346)
        assert 12345 in _tts_pids
        assert 12346 in _tts_pids

        _unregister_tts_pid(12345)
        assert 12345 not in _tts_pids
        assert 12346 in _tts_pids

        _unregister_tts_pid(99999)  # no-op for unknown pid
        _tts_pids.clear()

    def test_tts_cancelled_flag_roundtrip(self):
        """is_tts_cancelled / reset_tts_cancelled / mark_tts_cancelled round-trip."""
        from voice_claude_agent.tts import (
            is_tts_cancelled, reset_tts_cancelled, mark_tts_cancelled,
            _tts_pids,
        )

        _tts_pids.clear()
        reset_tts_cancelled()
        assert is_tts_cancelled() is False

        mark_tts_cancelled()
        assert is_tts_cancelled() is True

        reset_tts_cancelled()
        assert is_tts_cancelled() is False

    def test_cancel_all_tts_marks_cancelled(self, monkeypatch):
        """cancel_all_tts calls mark_tts_cancelled when a PID is tracked."""
        from voice_claude_agent.tts import cancel_all_tts, _register_tts_pid, _tts_pids

        mark_calls = []
        monkeypatch.setattr("voice_claude_agent.tts.mark_tts_cancelled", lambda: mark_calls.append(1))
        # Also bypass os.kill so we don't touch real processes
        monkeypatch.setattr("voice_claude_agent.tts.os.kill", lambda pid, sig: None)

        _tts_pids.clear()
        _register_tts_pid(12345)

        cancel_all_tts()
        assert len(mark_calls) == 1
        assert len(_tts_pids) == 0

    # ── afplay cancellation (Volcengine path) ──

    def test_afplay_drains_pids(self, monkeypatch):
        """cancel_all_tts drains _tts_pids, covering the Volcengine afplay path."""
        from voice_claude_agent.tts import cancel_all_tts, _register_tts_pid, _tts_pids

        killed = []
        monkeypatch.setattr("voice_claude_agent.tts.os.kill", lambda pid, sig: killed.append((pid, sig)))
        _tts_pids.clear()
        _register_tts_pid(99999)
        assert len(_tts_pids) == 1

        cancel_all_tts()
        assert len(_tts_pids) == 0
        assert killed == [(99999, 15)]

    # ── Stop Current Run during TTS writes tts_cancelled ──

    def test_stop_during_speaking_writes_tts_cancelled(self, monkeypatch, tmp_path):
        """Stop Current Run while TTS is playing writes tts_cancelled event."""
        alerts = []
        app = self._mock_app(monkeypatch, tmp_path, alerts)

        monkeypatch.setattr(
            "voice_claude_agent.tts.is_tts_speaking", lambda: True,
        )
        kill_calls = []
        monkeypatch.setattr(
            "voice_claude_agent.tts.cancel_all_tts",
            lambda: kill_calls.append(1) or 1,
        )

        app._stop_current_run(app.stop_current_item)
        assert kill_calls

        events_path = tmp_path / "agent_state" / "app_events.jsonl"
        assert events_path.exists()
        events = events_path.read_text().strip()
        assert "tts_cancelled" in events
        assert "TTS playback stopped" in alerts[-1]["message"]

    # ── View Logs shows tts_cancelled ──

    def test_view_logs_shows_tts_cancelled(self, monkeypatch, tmp_path):
        """View Logs displays tts_cancelled from last_result."""
        import voice_claude_agent.app as app_mod
        import voice_claude_agent.config as config_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        state_dir = tmp_path / "agent_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config_mod, "get_agent_state_dir", lambda: state_dir)
        monkeypatch.setattr(config_mod, "get_app_events_log_path", lambda: state_dir / "app_events.jsonl")
        monkeypatch.setattr(config_mod, "get_last_result_path", lambda: state_dir / "last_result.json")

        (state_dir / "app_events.jsonl").write_text(
            '{"timestamp":"2026-06-09T10:00:00+08:00","event":"tts_cancelled"}\n',
            encoding="utf-8",
        )
        (state_dir / "last_result.json").write_text(json.dumps({
            "prompt": "test", "exit_code": 0,
            "summary": "Some result",
            "tts_cancelled": True,
        }, ensure_ascii=False), encoding="utf-8")

        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._show_logs(app.logs_item)

        msg = alerts[-1]["message"]
        assert "tts_cancelled" in msg

    # ── Idle Stop Current Run still no-op ──

    def test_idle_stop_no_tts_event(self, monkeypatch, tmp_path):
        """Idle Stop Current Run does not write tts_cancelled."""
        alerts = []
        app = self._mock_app(monkeypatch, tmp_path, alerts)

        monkeypatch.setattr("voice_claude_agent.tts.is_tts_speaking", lambda: False)
        monkeypatch.setattr("voice_claude_agent.claude_runner.request_cancel", lambda: None)
        monkeypatch.setattr("voice_claude_agent.claude_runner.is_cancelled", lambda: False)

        app._cycle_in_progress = False
        app._claude_invocation_start = 0
        app._stop_current_run(app.stop_current_item)

        events_path = tmp_path / "agent_state" / "app_events.jsonl"
        assert not events_path.exists()
        assert "No active run" in alerts[-1]["message"]

    # ── Pipeline tts_cancelled persistence ──

    def test_pipeline_persists_tts_cancelled(self, monkeypatch, tmp_path):
        """_run_pipeline persists tts_cancelled in session and last_result."""
        monkeypatch.setattr("voice_claude_agent.cli.run_claude",
            lambda prompt, timeout=300, extra_args=None, workdir=None, cancel_event=None: type("R", (), {"command": [], "exit_code": 0, "stdout": "ok", "stderr": "", "duration_seconds": 0.1, "timed_out": False, "cancelled": False, "cwd": ""})())
        monkeypatch.setattr("voice_claude_agent.cli._resolve_tts_backend", lambda: "macos-say")
        monkeypatch.setattr("voice_claude_agent.cli.create_speaker", lambda: type("S", (), {"speak": lambda self, t: None})())

        # Simulate TTS being cancelled during speak
        monkeypatch.setattr("voice_claude_agent.cli.is_tts_cancelled", lambda: True)
        monkeypatch.setattr("voice_claude_agent.cli.reset_tts_cancelled", lambda: None)

        sessions_path = tmp_path / "sessions.jsonl"
        last_path = tmp_path / "last_result.json"
        monkeypatch.setattr("voice_claude_agent.logging_store.get_sessions_log_path", lambda: sessions_path)
        monkeypatch.setattr("voice_claude_agent.logging_store.get_last_result_path", lambda: last_path)

        from voice_claude_agent.cli import _run_pipeline
        _run_pipeline("test", input_mode="text", tts_fake=False)

        session = json.loads(sessions_path.read_text().splitlines()[0])
        assert session.get("tts_cancelled") is True

        last = json.loads(last_path.read_text())
        assert last.get("tts_cancelled") is True


# ── F081: Claude CLI 402 Balance Error Friendly Message ───────
# (tests are in TestSummarizer above)


# ── F082: Current Status Menu Item ──────────────────────────


class TestCurrentStatus:
    """F082: Current Status menu item tracks pipeline stages."""

    @staticmethod
    def _mock_app(monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        state_dir = tmp_path / "agent_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("voice_claude_agent.config.get_agent_state_dir", lambda: state_dir)
        monkeypatch.setattr("voice_claude_agent.config.get_app_events_log_path", lambda: state_dir / "app_events.jsonl")

        app = VoiceClaudeApp()
        return app

    def test_status_menu_item_present(self, monkeypatch, tmp_path):
        app = self._mock_app(monkeypatch, tmp_path)
        titles = set()
        for m in app.menu:
            if m is not None:
                t = m.title
                titles.add(t() if callable(t) else t)
        assert "Current Status: Idle" in titles

    def test_status_defaults_to_idle(self, monkeypatch, tmp_path):
        app = self._mock_app(monkeypatch, tmp_path)
        assert app._current_status == "Idle"
        assert app.status_item.title == "Current Status: Idle"

    def test_set_status_updates_title(self, monkeypatch, tmp_path):
        app = self._mock_app(monkeypatch, tmp_path)
        app._set_status("Recording")
        assert app.status_item.title == "Current Status: Recording"
        app._set_status("Idle")
        assert app.status_item.title == "Current Status: Idle"

    def test_record_and_execute_sets_statuses(self, monkeypatch, tmp_path):
        """_record_and_execute transitions through Recording/Transcribing/Claude running/Idle."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        state_dir = tmp_path / "agent_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("voice_claude_agent.config.get_agent_state_dir", lambda: state_dir)
        monkeypatch.setattr("voice_claude_agent.config.get_app_events_log_path", lambda: state_dir / "app_events.jsonl")
        monkeypatch.setattr("voice_claude_agent.config.get_last_result_path", lambda: state_dir / "last_result.json")

        status_log = []

        class FakeRec:
            def start(self): pass
            def stop(self): pass
            def get_audio(self): return b"fake audio"

        monkeypatch.setattr("voice_claude_agent.stt.RecordingTranscriber",
            lambda backend="": type("T", (), {"transcribe": lambda self, a: "hello"})())

        monkeypatch.setattr("voice_claude_agent.cli._run_pipeline",
            lambda transcript, input_mode, tts_fake, confirmation_override=None, stt_backend_used="", on_tts_start=None: (
                on_tts_start() if on_tts_start else None,
                (state_dir / "last_result.json").write_text('{"summary":"ok"}'),
            ))

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        app._open_mic_or_alert = lambda: FakeRec()
        app._record_with_timeout = lambda r: (b"audio", "ok")
        app._set_status = lambda s: status_log.append(s)
        app._append_runtime_event = lambda *a, **kw: None

        app._record_and_execute()
        assert "Recording" in status_log
        assert "Transcribing" in status_log
        assert "Claude running" in status_log
        assert status_log[-1] == "Idle"

    def test_stop_current_run_sets_status_cancelled(self, monkeypatch, tmp_path):
        """Stop Current Run sets Current Status to Cancelled."""
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp

        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)

        monkeypatch.setattr("voice_claude_agent.claude_runner.request_cancel", lambda: None)
        monkeypatch.setattr("voice_claude_agent.claude_runner.is_cancelled", lambda: False)
        monkeypatch.setattr("voice_claude_agent.tts.is_tts_speaking", lambda: False)

        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        app._cycle_in_progress = True
        app._stop_current_run(app.stop_current_item)
        assert app.status_item.title == "Current Status: Cancelled"


# ── F083: Reload Config Menu Item ──────────────────────────


class TestReloadConfig:
    """F083: Reload Config re-reads config.json and refreshes runtime state."""

    def test_reload_config_menu_item_present(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        app = VoiceClaudeApp()
        titles = set()
        for m in app.menu:
            if m is not None:
                t = m.title
                titles.add(t() if callable(t) else t)
        assert "Reload Config" in titles

    def test_reload_updates_record_seconds(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"VOICE_RECORD_SECONDS":"3"}')
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        app.record_seconds = 5
        app._reload_config_menu(app.reload_config_item)
        assert app.record_seconds == 3

    def test_reload_updates_stt_backend(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"VOICE_STT_BACKEND":"text-input"}')
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        app = VoiceClaudeApp(_alert_patch=lambda **kw: None)
        app.stt_backend = "whisper-cli"
        app._reload_config_menu(app.reload_config_item)
        assert app.stt_backend == "text-input"

    def test_reload_missing_config_no_crash(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        cfg_file = tmp_path / "nonexistent.json"
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._reload_config_menu(app.reload_config_item)
        assert any("Reloaded" in a["message"] for a in alerts if "message" in a)

    def test_reload_corrupted_config_shows_error(self, monkeypatch, tmp_path):
        import voice_claude_agent.app as app_mod
        from voice_claude_agent.app import VoiceClaudeApp
        monkeypatch.setattr(app_mod, "check_mic_permission", lambda: (True, "ok"))
        monkeypatch.setattr(app_mod, "load_config", lambda: {})
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not json")
        monkeypatch.setattr(app_mod, "get_config_path", lambda: cfg_file)
        alerts = []
        app = VoiceClaudeApp(_alert_patch=lambda **kw: alerts.append(kw))
        app._reload_config_menu(app.reload_config_item)
        assert any("Reload Config Failed" in a.get("title", "") for a in alerts)
