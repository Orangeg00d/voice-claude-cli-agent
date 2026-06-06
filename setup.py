"""py2app setup script for Voice Claude Agent.

Build:
  python setup.py py2app        # standalone .app
  python setup.py py2app -A     # alias mode (dev, uses symlinks)

Note: setuptools 82+ auto-populates install_requires from pyproject.toml
into the Distribution object, which py2app 0.28 rejects. We monkeypatch
py2app's init to tolerate this.

"""

from setuptools import setup

# ---- monkeypatch: py2app rejects install_requires in setuptools >= 80 ----
import py2app.build_app

_original_init = py2app.build_app.py2app.__init__


def _patched_init(self, dist, **kwargs):
    # Clear install_requires so py2app doesn't raise
    if hasattr(dist, "install_requires"):
        dist.install_requires = None
    _original_init(self, dist, **kwargs)


py2app.build_app.py2app.__init__ = _patched_init
# ---------------------------------------------------------------------------

APP = ["run_app.py"]
DATA_FILES: list[str] = []
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "LSUIElement": True,
        "CFBundleName": "VoiceClaudeAgent",
        "CFBundleDisplayName": "Voice Claude Agent",
        "CFBundleIdentifier": "com.voiceclaude.agent",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "NSMicrophoneUsageDescription": "Voice Claude Agent needs microphone access to record your voice commands.",
    },
    "packages": [
        "rumps",
        "click",
        "sounddevice",
        "numpy",
        "voice_claude_agent",
    ],
}

setup(
    app=APP,
    name="VoiceClaudeAgent",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)
