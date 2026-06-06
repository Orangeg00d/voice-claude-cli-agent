"""Entry-point script for the py2app-bundled Voice Claude Agent.

This script is the APP target for py2app. When the .app bundle is launched,
it starts the rumps menu bar application without requiring a terminal.
"""

from voice_claude_agent.app import launch_app
import os


def main():
    stt_backend = os.environ.get("VOICE_STT_BACKEND", "text-input")
    launch_app(stt_backend=stt_backend)


if __name__ == "__main__":
    main()
