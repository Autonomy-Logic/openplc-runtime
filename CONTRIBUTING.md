# Contributing

## Licensing and the Contributor License Agreement

OpenPLC Runtime is dual-licensed under the GPLv3 and a commercial licence (see `LICENSING.md`).

- **Contributor License Agreement.** By ticking the Contributor License Agreement checkbox in the pull request template, you agree that your contribution is licensed to Autonomy Logic Inc. under the terms of the [Contributor License Agreement](CLA.md). The CLA check fails until the box is ticked. You keep the copyright on your contribution.
- **Always available under the GPLv3.** Every contribution accepted here stays available under the GPLv3, in addition to the commercial licence.
- **SPDX header.** Every new source file starts with `SPDX-License-Identifier: GPL-3.0-only` and the Autonomy copyright line, as in the existing files.
- **Third-party code.** Do not add third-party code unless its licence is compatible with both the GPLv3 and the commercial licence. Ask in the pull request when in doubt.

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

PRs are reviewed against the shared review process maintained outside the repo. The in-tree
summary lives at `.claude/review-guidelines.md`.
