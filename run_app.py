"""Entry-point script for the py2app-bundled Voice Claude Agent.

This script is the APP target for py2app. When the .app bundle is launched,
it starts the rumps menu bar application without requiring a terminal.

Default STT backend is whisper-cli (local whisper.cpp). Override with
VOICE_STT_BACKEND env var: text-input, whisper-cli, or apple-speech.
"""

import os
import sys
from pathlib import Path


def _bootstrap_py2app_runtime_path() -> None:
    """Make py2app's filesystem packages importable before app modules load."""
    resources = Path(__file__).resolve().parent
    python_lib = resources / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    if python_lib.exists():
        python_lib_str = str(python_lib)
        if python_lib_str not in sys.path:
            sys.path.insert(0, python_lib_str)


def _bootstrap_state_dir() -> None:
    """Keep app runtime logs out of the .app bundle."""
    if os.environ.get("VOICE_CLAUDE_AGENT_STATE_DIR"):
        return

    resources = Path(__file__).resolve().parent
    project_root = resources.parents[3] if len(resources.parents) > 3 else None
    if project_root and (project_root / "feature_list.json").exists():
        state_dir = project_root / "agent_state"
    else:
        state_dir = (
            Path.home()
            / "Library"
            / "Application Support"
            / "VoiceClaudeAgent"
            / "agent_state"
        )
    os.environ["VOICE_CLAUDE_AGENT_STATE_DIR"] = str(state_dir)


def main():
    _bootstrap_py2app_runtime_path()
    _bootstrap_state_dir()
    from voice_claude_agent.app import launch_app
    from voice_claude_agent.config import get_config_value

    stt_backend = get_config_value("VOICE_STT_BACKEND", "whisper-cli")
    launch_app(stt_backend=stt_backend)


if __name__ == "__main__":
    main()
