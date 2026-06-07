# GitHub Release v0.1.0 — Draft

## v0.1.0 — Initial Release

Voice Claude Agent v0.1.0 is a local-first macOS voice assistant that enables
hands-free interaction with Claude CLI through a menu-bar app.

### Core Capabilities

- **Menu-bar app** (rumps): click Trigger Recording, speak, hear Claude's response
- **Local offline STT** via whisper.cpp (with text-input fallback for dev)
- **TTS** via macOS `say`
- **Three-tier risk classification**: read-only / recoverable / destructive
- **Voice confirmation for high-risk actions**: say "同意" to proceed, "取消" to abort
- **223 pytest tests**, ruff-clean, full pipeline coverage
- **Standalone .app bundle** via py2app
- **Structured logging**: app_events.jsonl, sessions.jsonl, last_result.json
- **Health Check + Mic Diagnostic** from the menu bar
- **Configurable**: `~/.voice-claude-agent/config.json`

### Quick Start

```bash
git clone https://github.com/Orangeg00d/voice-claude-cli-agent.git
cd voice-claude-cli-agent
./init.sh install
./init.sh check          # verify environment
./init.sh test           # 223 tests
voice-claude-agent app   # launch menu bar app
```

### Requirements

- macOS 14+ (Apple Silicon or Intel)
- Python 3.11+
- `brew install whisper-cpp` (for offline STT)
- `brew install claude` (Claude CLI, the execution backend)
- A GGML whisper model downloaded and `WHISPER_CPP_MODEL` set

### Known Limitations

- No real wake-word detection — interaction is via menu-bar button clicks
- py2app packaging requires setuptools ≥ 82 monkeypatch + manual dylib extraction
- macOS TCC mic permission may require Finder-based launch first

### Privacy

All audio recording, STT, and TTS run locally. Transcripts are passed to
Claude CLI per its own configuration. No telemetry. See [PRIVACY.md](PRIVACY.md).

### License

MIT — see [LICENSE](LICENSE).

### Full Release Notes

See [RELEASE_NOTES.md](RELEASE_NOTES.md) for detailed feature list (F001-F067).
