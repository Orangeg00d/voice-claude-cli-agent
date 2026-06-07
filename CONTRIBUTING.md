# Contributing Guidelines

Voice Claude Agent uses a **dual-agent collaborative workflow**:

- **Claude** (Claude Desktop / Claude CLI) is the primary developer.
- **Codex** is the reviewer, verifier, and GitHub publisher.

## Development Workflow

1. **Feature selection**: Pick the highest-priority `passes=false` feature
   from `feature_list.json`.

2. **Single feature per round**: Implement exactly one feature. Do not expand
   scope to other pending features.

3. **Self-test**: Run the project's test suite and verification commands:

   ```bash
   ./init.sh check
   ./init.sh lint
   ./init.sh test -q
   ```

4. **Update records**: After the feature passes verification:
   - Set `feature_list.json` → `passes=true` with evidence
   - Append a section to `agent-progress.md` documenting what was done,
     files changed, verification results, and next recommended task

5. **Commit**: One clean commit per feature with a descriptive message.

6. **Codex review**: Codex reviews the commit, checks diff/test/lint, and
   only then pushes to GitHub.

## Code Conventions

- Python 3.11+, type-hinted where practical
- Lint with `ruff check src/ tests/` (zero errors required)
- Format with `ruff format src/ tests/`
- Tests in `tests/`, organized by feature phase
- Stable references for rumps MenuItems (never use dynamic title lookups)

## Pull Requests

- PRs should correspond to either a single feature or a Codex review round
- PR description should link to the feature ID and list verification steps
- All CI checks (test + lint) must pass before merge

## Questions?

Open a GitHub Issue for questions about development setup or the workflow.
