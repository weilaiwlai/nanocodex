# CLAUDE.md

## Environment (MANDATORY)

Before any Python code, activate the conda env:

```bash
. $PROFILE
conda activate xcode
```

## Tech Stack

- **Python >= 3.12** — package manager: `uv`, config: `pyproject.toml`
- **TUI** — TypeScript, React + Ink, separate npm project in `tui/`

## Running

```bash
# CLI (Python)
uv run python scripts/cli.py

# TUI (TypeScript)
npm --prefix tui run dev
```

## Rules

1. **Never update/add dependencies without asking first.** This covers `uv add/sync --upgrade`, `npm install/add/update`, and any edits to `pyproject.toml` or `tui/package.json`.

2. **Never commit unless explicitly asked.**

3. **Git commits** follow Conventional Commits: `<type>(<scope>): <subject>`. Types: feat/fix/docs/style/refactor/test/chore/perf/ci/revert.

4. **Code style**: mimic existing patterns, no unnecessary comments, don't introduce new dependencies without asking.

5. **Config**: copy `.env.example` to `.env` before first run. Artifacts go to `artifacts/`.