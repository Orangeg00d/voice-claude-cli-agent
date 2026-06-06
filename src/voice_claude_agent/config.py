"""Configuration management."""

import shutil
from pathlib import Path


def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def get_agent_state_dir() -> Path:
    return get_project_root() / "agent_state"


def get_sessions_log_path() -> Path:
    return get_agent_state_dir() / "sessions.jsonl"


def get_last_result_path() -> Path:
    return get_agent_state_dir() / "last_result.json"


def find_claude_executable() -> str | None:
    return shutil.which("claude")


DEFAULT_TIMEOUT_SECONDS = 300
