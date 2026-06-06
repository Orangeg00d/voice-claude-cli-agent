"""Entry-point script for the py2app-bundled Voice Claude Agent.

This script is the APP target for py2app. When the .app bundle is launched,
it starts the rumps menu bar application without requiring a terminal.

Default STT backend is whisper-cli (local whisper.cpp). Override with
VOICE_STT_BACKEND env var: text-input, whisper-cli, or apple-speech.
"""

import os

from voice_claude_agent.app import launch_app


def main():
    stt_backend = os.environ.get("VOICE_STT_BACKEND", "whisper-cli")
    launch_app(stt_backend=stt_backend)


if __name__ == "__main__":
    main()
