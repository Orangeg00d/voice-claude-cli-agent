# Developer Program Application — Voice Claude Agent

## Project Summary

Voice Claude Agent is a **local-first macOS voice assistant** that enables
hands-free interaction with Claude CLI. Users speak commands through a menu-bar
app; the speech is transcribed locally via whisper.cpp, executed by Claude CLI,
and the results are read aloud via macOS Text-to-Speech.

## Key Differentiators

1. **Local-first audio pipeline** — Recording (sounddevice), STT (whisper.cpp),
   and TTS (macOS say) can all run on-device, with optional Volcengine/Doubao
   cloud ASR/TTS when the user configures API keys.
2. **Menu-bar native macOS experience** — Built with rumps/PyObjC, ships as
   a standalone .app bundle via py2app. No Docker, no Electron, no web UI.
3. **Safety-first design** — Three-tier risk classification (read-only /
   recoverable / destructive). High-risk actions trigger a voice confirmation
   loop: TTS asks the user to confirm, records a response, and only executes
   if the user says "agree" (同意/确认).
4. **Observable by default** — Every recording cycle logs structured events
   (trigger → record → STT → Claude → TTS) to app_events.jsonl. Full-stack
   Health Check and Mic Diagnostic available from the menu bar.
5. **Configurable** — `~/.voice-claude-agent/config.json` supports persistent
   settings without touching code.

## Technology Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | Python 3.11+ | Broad ecosystem, macOS compatible |
| CLI framework | Click | Standard Python CLI |
| Audio recording | sounddevice + numpy | Cross-platform PortAudio bindings |
| STT | whisper.cpp (CLI) | Offline, fast, Apple Silicon optimized |
| TTS | macOS `say` + optional Volcengine/Doubao TTS | Native fallback plus higher-quality cloud voice |
| Menu bar UI | rumps + PyObjC | Lightweight macOS system tray |
| Packaging | py2app | Native .app bundle |
| Testing | pytest (316 tests) | Full pipeline + UI + concurrency + cloud STT/TTS + workdir coverage |

## Project Status

- **Version**: v0.1.0
- **Features**: 79 acceptance items (F001-F079), all passing on current `main`
- **Tests**: 316 (pytest), passing with `./init.sh test`
- **Lint**: ruff clean
- **Code**: ~6000 lines (src + tests), 20+ source modules

## Privacy Design

See [PRIVACY.md](PRIVACY.md) for full details. Summary:
- Audio recording, default STT/TTS, and logging are local; optional Volcengine/Doubao ASR/TTS sends audio or text to the configured cloud API
- Transcripts are passed to Claude CLI, which may send them to Anthropic per
  the user's Claude CLI configuration
- No telemetry, no analytics, no network calls from the agent itself
- `NSMicrophoneUsageDescription` included in .app Info.plist

## Known Limitations

- **No wake-word detection** — interaction is via menu-bar button clicks
  (Trigger Recording / Start Wake), not voice activation
- **macOS only** — relies on rumps/PyObjC/macOS say/sounddevice
- **py2app packaging** requires a monkeypatch for setuptools ≥ 82 and
  manual extraction of libportaudio.dylib from python314.zip
- **TCC registration** — the .app may need Finder-based launch
  (`open dist/VoiceClaudeAgent.app`) for macOS to register the
  `com.voiceclaude.agent` bundle ID in Privacy & Security

## Roadmap (Tentative)

| Priority | Item |
|----------|------|
| P0 | Real wake-word detection (porcupine / openWakeWord) |
| P1 | Apple Silicon CoreML whisper backend |
| P2 | Notarized .app for distribution |
| P3 | Multi-language UI (English / 中文) |
| P4 | Homebrew cask distribution |

## Contact & Links

- Source: https://github.com/Orangeg00d/voice-claude-cli-agent
- License: MIT License — see [LICENSE](LICENSE)
- Documentation: README.md, RELEASE_NOTES.md, PRIVACY.md, SECURITY.md, CONTRIBUTING.md, TOOLS_AND_DEPS.md, GITHUB_RELEASE_DRAFT.md, REPOSITORY_METADATA.md, MANUAL_TEST_RELEASE.md, MANUAL_TEST_PHASE11.md, MANUAL_TEST_PHASE14.md
