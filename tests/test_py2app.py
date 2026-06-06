"""Tests for py2app bundling (F035).

These tests verify the bundle structure and configuration, not runtime behavior.
"""

import os
import plistlib
from pathlib import Path

import pytest


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def app_bundle() -> Path:
    bundle = _project_root() / "dist" / "VoiceClaudeAgent.app"
    if not bundle.exists():
        pytest.skip("Run 'python setup.py py2app' to produce dist/VoiceClaudeAgent.app")
    return bundle


class TestPy2appBundle:
    def test_setup_py_exists(self):
        """setup.py should exist at project root."""
        setup_py = _project_root() / "setup.py"
        assert setup_py.exists(), f"setup.py not found at {setup_py}"
        content = setup_py.read_text()
        assert "py2app" in content
        assert "VoiceClaudeAgent" in content
        assert "LSUIElement" in content

    def test_run_app_py_exists(self):
        """run_app.py should exist at project root."""
        run_app = _project_root() / "run_app.py"
        assert run_app.exists(), f"run_app.py not found at {run_app}"
        content = run_app.read_text()
        assert "launch_app" in content

    def test_bundle_exists(self, app_bundle):
        """dist/VoiceClaudeAgent.app should exist after build."""
        assert app_bundle.exists()

    def test_bundle_has_info_plist(self, app_bundle):
        """Bundle should contain Info.plist with LSUIElement=True."""
        info_plist = app_bundle / "Contents" / "Info.plist"
        assert info_plist.exists()
        with info_plist.open("rb") as f:
            plist = plistlib.load(f)

        assert plist["LSUIElement"] is True
        assert plist["NSMicrophoneUsageDescription"]
        assert plist["CFBundleIdentifier"] == "com.voiceclaude.agent"

    def test_bundle_has_executable(self, app_bundle):
        """Bundle should contain the main executable."""
        exe = app_bundle / "Contents" / "MacOS" / "VoiceClaudeAgent"
        assert exe.exists()
        assert os.access(exe, os.X_OK)

    def test_bundle_is_standalone_not_alias_mode(self, app_bundle):
        """Standalone bundle should embed Python instead of symlinking to the venv."""
        info_plist = app_bundle / "Contents" / "Info.plist"
        with info_plist.open("rb") as f:
            plist = plistlib.load(f)

        python_exe = app_bundle / "Contents" / "MacOS" / "python"
        framework = app_bundle / "Contents" / "Frameworks" / "Python.framework"

        assert plist["PyOptions"]["alias"] is False
        assert python_exe.exists()
        assert not python_exe.is_symlink()
        assert framework.exists()
