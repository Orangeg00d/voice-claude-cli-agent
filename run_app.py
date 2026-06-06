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


def main():
    _bootstrap_py2app_runtime_path()
    from voice_claude_agent.app import launch_app

    stt_backend = os.environ.get("VOICE_STT_BACKEND", "whisper-cli")
    launch_app(stt_backend=stt_backend)


if __name__ == "__main__":
    main()
