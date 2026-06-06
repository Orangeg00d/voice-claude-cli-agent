"""py2app setup script for Voice Claude Agent.

Build:
  python setup.py py2app        # standalone .app
  python setup.py py2app -A     # alias mode (dev, uses symlinks)

Note: setuptools 82+ auto-populates install_requires from pyproject.toml
into the Distribution object, which py2app 0.28 rejects. We monkeypatch
py2app's init to tolerate this.

F042: _sounddevice_data/portaudio-binaries/libportaudio.dylib must exist
as a real file on the filesystem (NOT inside python314.zip), because
dlopen cannot load dylibs from inside zip archives.
"""

import os
import pathlib
import shutil
import sys
import zipfile

from setuptools import setup

# ---- monkeypatch: py2app rejects install_requires in setuptools >= 80 ----
import py2app.build_app

_original_init = py2app.build_app.py2app.__init__


def _patched_init(self, dist, **kwargs):
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


# ── F042: Post-build fixup — extract _sounddevice_data dylib from zip ──

def _fixup_portaudio_dylib(dist_dir: str) -> None:
    """Extract libportaudio.dylib from python314.zip to the real filesystem.

    py2app puts _sounddevice_data inside python314.zip, but dlopen()
    cannot load .dylib files from inside a zip archive.  We extract
    the _sounddevice_data subtree to Resources/lib/ so it exists as
    real files.
    """
    resources = pathlib.Path(dist_dir) / "Contents" / "Resources"
    zip_path = resources / "lib" / "python314.zip"
    if not zip_path.exists():
        # Try older Python naming
        for candidate in sorted(resources.glob("lib/python*.zip")):
            zip_path = candidate
            break
    if not zip_path.exists():
        print("[F042] WARNING: python*.zip not found — skipping PortAudio fixup")
        return

    dest_root = resources / "lib"
    dest_root.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if "_sounddevice_data/" in name and "libportaudio" in name:
                # Extract the dylib
                member = zf.getinfo(name)
                dest = dest_root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(dest, 0o755)
                print(f"[F042] Extracted {name} -> {dest}")

                # Remove from zip so there's no stale copy
                # (can't modify in-place; we'll use a new zip)

    # Rebuild the zip without the dylib entries
    tmp_zip = zip_path.with_suffix(".tmp.zip")
    with zipfile.ZipFile(zip_path, "r") as zin:
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if "_sounddevice_data/" in item.filename and "libportaudio" in item.filename:
                    continue  # skip — now on filesystem
                data = zin.read(item.filename)
                zout.writestr(item, data)

    tmp_zip.replace(zip_path)
    print(f"[F042] Removed libportaudio.dylib from {zip_path.name}")


setup(
    app=APP,
    name="VoiceClaudeAgent",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)

# Run post-build fixup
if "py2app" in sys.argv:
    dist_dir = os.path.join(os.path.dirname(__file__) or ".", "dist", "VoiceClaudeAgent.app")
    if os.path.isdir(dist_dir):
        _fixup_portaudio_dylib(dist_dir)
