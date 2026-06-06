# Agent Progress Log

## 2026-06-06 09:15 — Initializer: Project Skeleton & Demo-Text MVP

### Completed
- Created pyproject.toml with Python 3.11+ project config, click, pytest, ruff
- Created src/voice_claude_agent/ package with full module tree:
  - cli.py — CLI entry point (check, demo-text, run-text)
  - config.py — configuration and dependency detection
  - claude_runner.py — subprocess Claude CLI executor with ClaudeRunResult
  - risk.py — three-level risk classifier (read_only / recoverable / external_or_destructive)
  - confirmation.py — CLI yes/no confirmation for high-risk actions
  - tts.py — TTS abstraction (MacOSSaySpeaker, FakeSpeaker for tests)
  - summarizer.py — Claude CLI output → human-readable summary
  - logging_store.py — JSONL session logger + last_result.json writer
- Created init.sh with install, check, test, demo-text, lint, format commands
- Created feature_list.json with F001–F012 features (all passes=false)
- Created agent_state/ directory for runtime data
- Verified README.md, CLAUDE.md, CODEX_REVIEW_GUIDE.md exist

### Verification
- Pending: ./init.sh check
- Pending: ./init.sh test
- Pending: voice-claude-agent demo-text

### Files Changed
- pyproject.toml (new)
- src/voice_claude_agent/__init__.py (new)
- src/voice_claude_agent/cli.py (new)
- src/voice_claude_agent/config.py (new)
- src/voice_claude_agent/claude_runner.py (new)
- src/voice_claude_agent/risk.py (new)
- src/voice_claude_agent/confirmation.py (new)
- src/voice_claude_agent/tts.py (new)
- src/voice_claude_agent/summarizer.py (new)
- src/voice_claude_agent/logging_store.py (new)
- init.sh (new)
- feature_list.json (new)
- agent-progress.md (new)
- agent_state/ (new directory)

### Next Recommended Task
- Write pytest tests for all core modules
- Run ./init.sh install && ./init.sh check && ./init.sh test
- Update feature_list.json passes/evidence

### Risks / Notes
- Claude CLI may not be present on this system — tests must mock the subprocess
- macOS say may not be available — demo-text uses FakeSpeaker by default
- F010/F011 (STT, wake trigger) only need interface stubs in Phase 1; full implementation in Phase 2/3

## 2026-06-06 09:40 — Codex Review: Phase 1 Verification

### Completed
- Reviewed Claude's Phase 1 implementation and feature evidence.
- Found a harness bug: `./init.sh install` installed dependencies into `.venv`, but `check`, `test`, `demo-text`, `lint`, and `format` still used global `python3` / `ruff`.
- Fixed `init.sh` to prefer the active virtualenv or project `.venv`.
- Cleaned lint-only issues: unused imports and ambiguous local variable names.
- Verified real `demo-text` path invokes local `claude -p` and records a successful result.

### Verification
- `./init.sh install` passed
- `./init.sh check` passed
- `./init.sh test` passed: 28 passed
- `./init.sh lint` passed
- `./init.sh demo-text "请只回复 OK"` passed: Claude CLI returned `OK`

### Files Changed
- init.sh
- src/voice_claude_agent/claude_runner.py
- src/voice_claude_agent/cli.py
- src/voice_claude_agent/command_router.py
- src/voice_claude_agent/config.py
- src/voice_claude_agent/confirmation.py
- src/voice_claude_agent/summarizer.py
- tests/test_core.py
- agent-progress.md

### Next Recommended Task
- Ask Claude to begin Phase 2 by improving the STT abstraction and adding a manual record/transcribe flow behind mocks first.
- Before Phase 2 implementation, Claude should update the progress entry format so verification is not left as Pending after feature evidence is marked passed.

### Risks / Notes
- `agent-progress.md` from the initializer still contains Pending verification lines, even though `feature_list.json` marks F001-F012 passed. This was corrected by Codex verification above, but future Claude rounds must keep progress and feature evidence consistent.
- `MacOSSaySpeaker` currently escapes quotes before passing an argv list to `subprocess.run`; this is not dangerous, but it can alter spoken text. Fix in a later polish task.
