# Security — SBOM and vulnerability flows

Two separate flows, run by GitHub Actions:

| Flow | What it is | When it runs | Output |
|---|---|---|---|
| **SBOM** (`security-sbom.yml`) | The bill of materials: every dependency with its version and licence (CycloneDX 1.6, SPDX 2.3, components CSV) | Once per release: a commit landing on `main`. A released version's SBOM never changes, so it is not regenerated on a schedule | Branch `security-archive`, folder `pr-<n>-<date>/` |
| **Vulnerabilities, PR check** (`security-pr-gate.yml`) | osv-scanner on the PR head and base | Every PR into `development` or `main` | Blocks only a **critical advisory the PR introduces**; pre-existing advisories never block. Sticky PR comment, no SBOM, no report |
| **Vulnerabilities, release report** (`security-release-vulns.yml`) | Scan plus the vulnerability report (md, html, pdf) | Once per release, next to the SBOM | Same `security-archive` folder as the SBOM |
| **Vulnerabilities, monthly** (`security-monthly.yml`) | Scan, Claude triage of new advisories, vulnerability report | 1st of every month, 06:00 UTC, and on demand | A PR into `main` with `security/<YYYY-MM>/` |

The vulnerability flows do not need the SBOM: they scan the pinned Python
closure and the Go modules directly (see *Coverage* below) and honour the VEX
baseline in `../osv-scanner.toml`. The report's *raw* scan is run with
`--config=security/osv-raw.toml`, an empty config: without `--config`,
osv-scanner applies the `osv-scanner.toml` of the scanned directory, and the raw
and suppressed counts would be identical. A commit on `main` that only
touches `security/` and `osv-scanner.toml` (such as the merged monthly PR) is not
a release, so the release flows skip it.

```
security-archive (orphan branch)
└── pr-<n>-<date>/
    ├── <name>.cdx.json · <name>.spdx.json · <name>.components.csv · PROVENANCE-sbom.txt   ← SBOM flow
    └── vulnerabilities.csv · <Title>-Vulnerability-Report.md/.html/.pdf · PROVENANCE-vulns.txt ← vulnerability flow

security/ (this folder, on the code branches)
├── <YYYY-MM>/                       ← monthly vulnerability snapshot
├── LATEST                           ← text file naming the current month
└── report-config.json               ← report DATA (VEX triage + narrative)
```

(`LATEST` is a plain text file rather than a symlink: a committed symlink
materializes as a text file on Windows checkouts anyway, so it may as well be one
everywhere.)

> `on: schedule` only fires from the repository's **default branch**. To test the
> release flows without touching the real evidence, run them with **Run workflow**
> on any branch other than `main`; they write to `archive_branch` (default
> `security-archive-test`). On `main` the input is ignored and the target is always
> `security-archive`, and `scripts/security-archive.sh` refuses to write
> `security-archive` from any other ref.

## Coverage

This repository has no single lockfile, so each ecosystem is handled on its own.

| Part | SBOM (`scripts/generate-sbom.sh`) | Vulnerability scan (`scripts/osv-scan.sh`) |
|---|---|---|
| Python: `requirements.txt` (runtime venv) and every `core/src/drivers/plugins/python/*/requirements.txt` (one venv per plugin) | Yes. The files are mostly unpinned, so `scripts/resolve-python-deps.sh` asks pip for the closure it would install today (`pip install --dry-run --report`, Python 3.11 like the `debian:bookworm` image), one manifest at a time; cdxgen reads the pinned result | Yes, the same pinned closure (`--no-resolve`) |
| Go: `bootloader/` module | Yes, cdxgen `-t go` (with a Go toolchain, so indirect modules are included) | Yes, `go.mod` found by the recursive scan (also `tests/integration/stubruntime`, a test module) |
| Vendored C/C++: cJSON (`core/src/drivers/plugins/native/cjson`), Snap7 (`core/src/drivers/plugins/native/s7comm/snap7`) | Yes, from `security/components.json`, licences from the vendored file headers | **No.** No package-manager identity osv-scanner can match; review upstream advisories by hand |
| Fetched by `install.sh`: EtherDOG (`ETHERDOG_REF`), its SOEM submodule, the CMake release | Yes, from `security/components.json` | **No**, as above |
| Debian packages of the base image, the system CPython, gcc and the other apt toolchain | No | No |
| STruC++ runtime headers (arrive with each uploaded PLC program) | No (not in this repository) | No |

`security/components.json` is checked on every SBOM run: an entry with
`versionFrom` must match the version in the named file (`cJSON.h`, the Snap7
header, `install.sh`), so upgrading vendored code without updating the list fails
the release SBOM. SOEM has no such check: its commit is pinned by the EtherDOG
release, so update it together with `ETHERDOG_REF`.

Because the Python closure is re-resolved on every run, a new upstream release can
change the scanned versions without any commit here. The PR gate resolves base
and head in the same job, so an unchanged manifest resolves the same way on both
sides and only a real dependency change counts as introduced.

When a scan finds nothing to scan or no advisory, the flows still succeed with an
empty register; only a scanner or resolver error fails them.

## What stops a bad report from shipping

The chain has to fail loudly rather than produce a clean-looking document from
nothing. Three guards do that:

- **`scripts/assert-sbom.mjs`** — a *content* floor on the generated SBOM. Every
  other stage validates only shape, so an empty or partial CycloneDX file used to
  travel the whole chain and come out as a Confidential report attesting a clean
  posture over zero components. Below the floor, the run stops.
- **Untriaged detection** — each entry in `affected[]` / `mitigated[]` /
  `notAffected[]` **must** list the advisory `ids` it accounts for (any alias
  works; the monthly validation step fails on an entry without `ids`).
  `build-report.mjs` compares those against the live post-suppression scan;
  anything unclaimed gets its own **section 4a — Untriaged in This Scan**, the
  executive summary is marked *Assessment incomplete*, and the stored posture
  statement is labelled as the last full assessment that does not cover them,
  instead of as the headline posture. The document is allowed to be incomplete; it
  is not allowed to be quietly self-contradictory.
- **`scripts/lib/severity.mjs` and `scripts/lib/cdx.mjs`** — one severity verdict
  and one component-collection walk, shared by the gate, the report, the
  vulnerabilities CSV, the SPDX converter and the CSV converter. An advisory is
  one OSV alias group per package (a GHSA and its PYSEC/CVE twins count once);
  its severity is the highest parsable severity of any member or of the group's
  `max_severity`, and **UNKNOWN** when there is none. A PYSEC record with no
  severity of its own therefore takes the CVSS of its GHSA twin or of the group.
  UNKNOWN is counted and listed, and never blocks the gate. `vulnerabilities.csv`
  is written from the same index, so its row count equals the register count in
  the report. The release SBOM flow asserts the three component counts agree
  before archiving. `fixed_in` lists only the fix versions OSV gives for that
  package (and ecosystem); one advisory can cover several packages. Values that
  hold `|` (an advisory installed at several versions) are escaped in every
  Markdown table.
- **SPDX supplier** — taken from the CycloneDX `supplier`, `manufacturer`,
  `publisher` or `authors` of each component (the entries of
  `security/components.json` carry `supplier`), and `NOASSERTION` otherwise.
  Registry author strings are cleaned to `Person: <name> (<email>)` (stray quotes
  and nested `<...>` dropped). The SBOM does not claim NTIA minimum-elements
  conformance: most registry packages carry no supplier. `created` is UTC with
  second precision.

## The suppression baseline (`../osv-scanner.toml`)

Expiry dates are **staggered on purpose** — see the header comment in that file.
The offset from the review date comes from the VEX justification and is then
shifted by severity (CRITICAL is pulled to the first review, LOW/MODERATE pushed
back), so no single month inherits the whole baseline for re-review.

Suppressions written by the automated triage step carry
`AI-TRIAGED [unreviewed]` in their `reason`. **The AI step has authority to write
security suppressions and the only control is human review of the PR diff** —
clearing that marker is the reviewer's acknowledgement that a person checked it.
A step after the triage re-parses both files, refuses a net *loss* of
suppressions, refuses more than **15 new suppressions in one run**, and refuses a
triage entry without `ids`, before anything is rendered. The prompt tells the
model that advisory text is untrusted data, never instructions. When the triage
changed `osv-scanner.toml`, the job rescans with it (same wrapper and resolved
Python closure), so the report and `vulnerabilities.csv` reflect the new
suppressions.

To re-baseline by hand from a triaged register:

```sh
node scripts/gen-osv-ignores.mjs sbom/vulnerabilities.csv 2027-01-01 >> osv-scanner.toml
```

It refuses to run without a per-row verdict column, and it will not suppress an
advisory that any row triages as requiring action. Only the exact verdicts
`not_affected`, `mitigated` and `not_applicable` suppress (case, spaces and
hyphens folded); "not mitigated" stays visible. An expiry that would fall past the
end of a month is clamped to its last day.

## One-time setup — run Claude on your subscription (no API key)

Claude runs inside the workflow on **your Claude subscription** (Pro/Max), not a
pay-per-token API key:

1. On your machine: `claude setup-token` → complete the browser login → copy the
   token it prints (`CLAUDE_CODE_OAUTH_TOKEN`).
2. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret** → name `CLAUDE_CODE_OAUTH_TOKEN`, value = the token.
3. Done. The monthly workflow will authenticate as you. (If the secret is absent,
   the workflow still runs and builds the report deterministically — it just
   skips the AI triage step.)

This repository is public. The triage step only runs from the schedule or a
manual dispatch, never from a pull request, so the secret is not exposed to
forks.

> To move billing off a personal account later (e.g. an org API key), add an
> `ANTHROPIC_API_KEY` secret and, in `security-monthly.yml`, map it in the job `env`,
> change the Claude step's `if:` to test `env.ANTHROPIC_API_KEY`, and replace the
> `claude_code_oauth_token` input with `anthropic_api_key`.

## Reviewing the monthly PR

- **No new advisory:** confirm the summary, approve, merge (the snapshot is archived).
- **New advisory, not exploitable:** check Claude's justification in the
  `osv-scanner.toml` diff, drop the `AI-TRIAGED [unreviewed]` marker from the
  entries you have verified, then merge.
- **New advisory, exploitable:** bump the dependency (separate PR) and merge the
  report that documents it.
- **Report says *Assessment incomplete*:** section 4a lists advisories nothing
  accounted for. Each one needs either a dependency bump or a documented
  `not_affected` determination with its id recorded in `report-config.json`.
