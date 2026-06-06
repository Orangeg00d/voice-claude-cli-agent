#!/usr/bin/env bash
# Voice Claude Agent — project init script
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

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
  pyver=$(python3 -c "import sys; print(sys.version.split()[0])" 2>/dev/null || echo "")
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
  if python3 -c "import voice_claude_agent" 2>/dev/null; then
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
  python3 -m pytest tests/ -v "$@"
}

# ── demo-text ────────────────────────────────────────────
do_demo_text() {
  local prompt="${1:-让 Claude 回复 OK}"
  echo "=== Voice Claude Agent — Demo Text ==="
  echo "Prompt: $prompt"
  echo ""
  python3 -m voice_claude_agent.cli demo-text "$prompt"
}

# ── lint ─────────────────────────────────────────────────
do_lint() {
  if command -v ruff &>/dev/null; then
    ruff check src/ tests/ "$@"
  else
    say_warn "ruff not installed — install dev dependencies first"
  fi
}

# ── format ───────────────────────────────────────────────
do_format() {
  if command -v ruff &>/dev/null; then
    ruff format src/ tests/ "$@"
  else
    say_warn "ruff not installed — install dev dependencies first"
  fi
}

# ── dispatch ─────────────────────────────────────────────
case "${1:-}" in
  install)   do_install ;;
  check)     do_check ;;
  test)      shift; do_test "$@" ;;
  demo-text) do_demo_text "${2:-}" ;;
  lint)      shift; do_lint "$@" ;;
  format)    shift; do_format "$@" ;;
  *)
    echo "Usage: $0 {install|check|test|demo-text|lint|format}"
    echo ""
    echo "  install    Install package in editable mode"
    echo "  check      Check all dependencies"
    echo "  test       Run pytest"
    echo "  demo-text  Run the full demo-text pipeline"
    echo "  lint       Run ruff linter"
    echo "  format     Run ruff formatter"
    exit 1
    ;;
esac
