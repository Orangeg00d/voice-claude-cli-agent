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
from voice_claude_agent.risk import (
    RiskLevel,
    classify_risk,
    requires_confirmation,
    DESTRUCTIVE_KEYWORDS,
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
