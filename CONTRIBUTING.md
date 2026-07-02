# Contributing

## Setup

Requires Python 3.10+ and CMake 3.28+. Full environment guide: `docs/DEVELOPMENT.md`.

## Workflow

1. Pick a ticket in Jira, project RTOP (internal tracker).
2. Branch from `development`, named `RTOP-<n>-<kebab-slug>`. Maintenance without a ticket uses `chore/`, `fix/`, `docs/`.
3. Commit style: Conventional Commits (full spec in `docs/DEVELOPMENT.md`), concise, focused on why.
4. Open a PR targeting `development` and fill in the PR template.

## Before pushing

```bash
bash scripts/run-pytest.sh   # sets up the test venv and runs the suite
pre-commit run               # lint/format (install once with: pre-commit install)
```

## Docs

If your change alters documented behavior (commands, endpoints, env vars, architecture, setup steps), update the affected docs (README, CLAUDE.md, docs/) in the same PR.

## Review

PRs are reviewed against `docs/pr-reviews/PR_REVIEW_CHECKLIST.md` (summary in `.claude/review-guidelines.md`).
