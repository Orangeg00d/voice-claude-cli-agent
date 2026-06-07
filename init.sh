#!/usr/bin/env bash
# Voice Claude Agent — project init script
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
  PYTHON="$VIRTUAL_ENV/bin/python"
elif [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
  PYTHON="$PROJECT_ROOT/.venv/bin/python"
else
  PYTHON="python3"
fi

if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/ruff" ]; then
  RUFF="$VIRTUAL_ENV/bin/ruff"
elif [ -x "$PROJECT_ROOT/.venv/bin/ruff" ]; then
  RUFF="$PROJECT_ROOT/.venv/bin/ruff"
else
  RUFF="ruff"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

say_ok()  { echo -e "${GREEN}[OK]${NC} $*"; }
say_err() { echo -e "${RED}[ERR]${NC} $*"; }
say_warn(){ echo -e "${YELLOW}[WARN]${NC} $*"; }

# ── install ──────────────────────────────────────────────
do_install() {
  if command -v uv &>/dev/null; then
    uv pip install -e ".[dev]"
  else
    pip install -e ".[dev]"
  fi
  say_ok "Package installed (editable mode)"
}

# ── check ────────────────────────────────────────────────
do_check() {
  local ok=true

  echo "=== Voice Claude Agent — Dependency Check ==="
  echo ""

  # Python
  local pyver
  pyver=$("$PYTHON" -c "import sys; print(sys.version.split()[0])" 2>/dev/null || echo "")
  if [ -z "$pyver" ]; then
    say_err "Python 3 not found"
    ok=false
  else
    say_ok "Python $pyver"
  fi

  # Claude CLI
  if command -v claude &>/dev/null; then
    say_ok "Claude CLI: $(command -v claude)"
    claude --version 2>/dev/null || true
  else
    say_warn "Claude CLI not found in PATH — install it to use Claude features"
  fi

  # macOS say
  if command -v say &>/dev/null; then
    say_ok "macOS say: available"
  else
    say_warn "macOS 'say' not found — TTS will not work"
  fi

  # Project package
  if "$PYTHON" -c "import voice_claude_agent" 2>/dev/null; then
    say_ok "voice_claude_agent package importable"
  else
    say_warn "voice_claude_agent not importable — run './init.sh install'"
    ok=false
  fi

  # agent_state dir
  if [ -d "agent_state" ]; then
    say_ok "agent_state/ exists"
  else
    say_warn "agent_state/ missing"
    ok=false
  fi

  echo ""
  if $ok; then
    say_ok "All critical checks passed."
  else
    say_err "Some checks failed — review warnings above."
    exit 1
  fi
}

# ── test ─────────────────────────────────────────────────
do_test() {
  if [ ! -f "pyproject.toml" ]; then
    say_err "pyproject.toml not found — run './init.sh install' first"
    exit 1
  fi
  "$PYTHON" -m pytest tests/ -v "$@"
}

# ── demo-text ────────────────────────────────────────────
do_demo_text() {
  local prompt="${1:-让 Claude 回复 OK}"
  echo "=== Voice Claude Agent — Demo Text ==="
  echo "Prompt: $prompt"
  echo ""
  "$PYTHON" -m voice_claude_agent.cli demo-text "$prompt"
}

# ── lint ─────────────────────────────────────────────────
do_lint() {
  if command -v "$RUFF" &>/dev/null; then
    "$RUFF" check src/ tests/ "$@"
  else
    say_warn "ruff not installed — install dev dependencies first"
  fi
}

# ── format ───────────────────────────────────────────────
do_format() {
  if command -v "$RUFF" &>/dev/null; then
    "$RUFF" format src/ tests/ "$@"
  else
    say_warn "ruff not installed — install dev dependencies first"
  fi
}

# ── build-app ────────────────────────────────────────────
do_build_app() {
  if [ ! -f "setup.py" ]; then
    say_err "setup.py not found — cannot build .app"
    exit 1
  fi
  say_ok "Building VoiceClaudeAgent.app ..."
  "$PYTHON" setup.py py2app
  if [ -d "dist/VoiceClaudeAgent.app" ]; then
    say_ok "Build complete: dist/VoiceClaudeAgent.app"
  else
    say_err "Build failed — dist/VoiceClaudeAgent.app not found"
    exit 1
  fi
}

# ── install-app ───────────────────────────────────────────
do_install_app() {
  local target="$HOME/Applications/VoiceClaudeAgent.app"
  if [ ! -d "dist/VoiceClaudeAgent.app" ]; then
    say_err "dist/VoiceClaudeAgent.app not found — run './init.sh build-app' first"
    exit 1
  fi
  mkdir -p "$HOME/Applications"
  if [ -d "$target" ]; then
    rm -rf "$target"
  fi
  cp -R dist/VoiceClaudeAgent.app "$target"
  say_ok "Installed to $target"
}

# ── dispatch ─────────────────────────────────────────────
case "${1:-}" in
  install)     do_install ;;
  check)       do_check ;;
  test)        shift; do_test "$@" ;;
  demo-text)   do_demo_text "${2:-}" ;;
  lint)        shift; do_lint "$@" ;;
  format)      shift; do_format "$@" ;;
  build-app)   do_build_app ;;
  install-app) do_install_app ;;
  *)
    echo "Usage: $0 {install|check|test|demo-text|lint|format|build-app|install-app}"
    echo ""
    echo "  install     Install package in editable mode"
    echo "  check       Check all dependencies"
    echo "  test        Run pytest"
    echo "  demo-text   Run the full demo-text pipeline"
    echo "  lint        Run ruff linter"
    echo "  format      Run ruff formatter"
    echo "  build-app   Build .app bundle via py2app"
    echo "  install-app Copy .app to ~/Applications/"
    exit 1
    ;;
esac
