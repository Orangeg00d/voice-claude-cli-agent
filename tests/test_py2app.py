"""Tests for py2app bundling (F035).

These tests verify the bundle structure and configuration, not runtime behavior.
"""

import os
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


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

    def test_bundle_exists(self):
        """dist/VoiceClaudeAgent.app should exist after build."""
        bundle = _project_root() / "dist" / "VoiceClaudeAgent.app"
        assert bundle.exists(), (
            f"Bundle not found at {bundle}. Run 'python setup.py py2app -A' first."
        )

    def test_bundle_has_info_plist(self):
        """Bundle should contain Info.plist with LSUIElement=True."""
        info_plist = (
            _project_root()
            / "dist"
            / "VoiceClaudeAgent.app"
            / "Contents"
            / "Info.plist"
        )
        assert info_plist.exists()
        content = info_plist.read_text()
        assert "LSUIElement" in content
        assert "NSMicrophoneUsageDescription" in content
        assert "com.voiceclaude.agent" in content

    def test_bundle_has_executable(self):
        """Bundle should contain the main executable."""
        exe = (
            _project_root()
            / "dist"
            / "VoiceClaudeAgent.app"
            / "Contents"
            / "MacOS"
            / "VoiceClaudeAgent"
        )
        assert exe.exists()
        assert os.access(exe, os.X_OK)

    def test_bundle_has_python_symlink(self):
        """Alias mode bundle should have a python symlink."""
        python_link = (
            _project_root()
            / "dist"
            / "VoiceClaudeAgent.app"
            / "Contents"
            / "MacOS"
            / "python"
        )
        assert python_link.is_symlink()
