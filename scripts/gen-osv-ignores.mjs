#!/usr/bin/env node
// Generate osv-scanner.toml [[IgnoredVulns]] entries from a triaged
// vulnerabilities.csv. Rows classified "not affected" / "mitigated" (our VEX
// baseline) become suppressions so the monthly scan only surfaces NEW findings.
// Rows that require action (APLICA / affected) are intentionally NOT suppressed
// — they keep alerting until the dependency is bumped.
//
// Usage:
//   node scripts/gen-osv-ignores.mjs sbom/vulnerabilities.csv 2026-10-01 >> osv-scanner.toml
//     arg1 = CSV path
//     arg2 = review-by date (YYYY-MM-DD) written as ignoreUntil
//
// The CSV must contain an advisory-id column (named cve / advisory / id) whose
// cells hold OSV ids (GHSA-*, CVE-*, PYSEC-*, possibly pipe-separated) and a
// verdict column (verdict / verdict_vex / reach). Only rows the team classified
// "not_affected" or "mitigated" are suppressed; a not_affected row is suppressed
// ONLY if its verdict/basis names one of the five CISA justification enums
// (component_not_present, vulnerable_code_not_present,
// vulnerable_code_not_in_execute_path,
// vulnerable_code_cannot_be_controlled_by_adversary,
// inline_mitigations_already_exist). Everything else stays visible.
//
// Emitted reasons are machine-readable and parsed back by build-report.mjs:
//   VEX not_affected/<cisa_justification> [pkg / sev]: <basis>
//   VEX affected/mitigated [pkg / sev]: <control>          (a DISTINCT VEX status)

import { readFileSync } from 'node:fs';

const [csvPath, reviewDate = '1970-01-01'] = process.argv.slice(2);
if (!csvPath) { console.error('usage: gen-osv-ignores.mjs <vulnerabilities.csv> <YYYY-MM-DD>'); process.exit(1); }

const rows = readFileSync(csvPath, 'utf8').trim().split('\n').map(parseCsvLine);
const header = rows.shift().map((h) => h.toLowerCase());
const idCol = header.findIndex((h) => ['cve', 'advisory', 'id', 'advisory_id'].includes(h));
const verdictCol = header.findIndex((h) => ['verdict', 'verdict_vex', 'reach'].includes(h));
// The reason must be a real VEX justification — NEVER the advisory title/summary
// (that would write the vulnerability's description as if it were our rationale).
const reasonCol = header.findIndex((h) => ['basis', 'verdict_vex', 'reason', 'justification'].includes(h));
if (idCol < 0) { console.error('No advisory-id column (cve/advisory/id) found'); process.exit(1); }
// A verdict column is MANDATORY. Without it we cannot tell "not affected" from
// "requires action", and suppressing every row would blind the scanner. Refuse.
if (verdictCol < 0) {
  console.error('Refusing to run: the CSV has no verdict column (verdict/verdict_vex/reach).\n'
    + 'Without a per-row verdict, this would suppress the ENTIRE scan. Add a verdict column\n'
    + 'carrying the CISA VEX status (e.g. "not_affected", "mitigated", or "affected").');
  process.exit(1);
}

// Optional package / severity columns — included in the reason for readability.
const pkgCol = header.findIndex((h) => ['package', 'pkg', 'component'].includes(h));
const sevCol = header.findIndex((h) => ['severity', 'sev'].includes(h));

// A suppressible row is one the team classified "not affected" or "mitigated".
// "affected"/"requires action" (and anything ambiguous) stay VISIBLE so they keep
// alerting. `\bmitigat` is anchored to a status word so it does not match
// "mitigation pending" / "needs mitigation" (which mean action is still required).
const isMitigated = (v) => /\bmitigat(?:ed|ing)\b|inline[_ ]?mitigation/i.test(v || '');
const isNotAffected = (v) => /\bnot[_ ]?affected\b|\bnot[_ ]?applicable\b/i.test(v || '');

// The five CISA VEX "not_affected" justification enums. We only suppress with one
// of these (or "mitigated"); a row that names none is left visible for triage.
const CISA = ['component_not_present', 'vulnerable_code_not_present', 'vulnerable_code_not_in_execute_path', 'vulnerable_code_cannot_be_controlled_by_adversary', 'inline_mitigations_already_exist'];
const cisaOf = (text) => CISA.find((e) => new RegExp(e, 'i').test(text || ''));

const seen = new Set();
let emitted = 0, skippedNoJust = 0;

for (const r of rows) {
  const verdict = r[verdictCol] || '';
  const reasonText = (reasonCol >= 0 ? r[reasonCol] : '') || '';
  const mitigated = isMitigated(verdict) || isMitigated(reasonText);
  if (!mitigated && !isNotAffected(verdict)) continue; // keep actionable / ambiguous ones visible

  // "affected/mitigated" is a distinct VEX status from "not_affected" — a
  // compensating control is NOT a statement of non-affectedness (CISA VEX).
  let tag, just;
  if (mitigated) { tag = 'affected/mitigated'; just = null; }
  else {
    just = cisaOf(verdict) || cisaOf(reasonText);
    if (!just) { skippedNoJust++; continue; } // refuse to suppress without a CISA justification
    tag = `not_affected/${just}`;
  }

  const ids = String(r[idCol] || '').split('|').map((s) => s.trim()).filter((s) => /^(GHSA|CVE|PYSEC)-/i.test(s));
  const pkg = pkgCol >= 0 ? String(r[pkgCol] || '').trim() : '';
  const sev = sevCol >= 0 ? String(r[sevCol] || '').trim() : '';
  const loc = pkg ? ` [${pkg}${sev ? ` / ${sev}` : ''}]` : '';
  const detail = reasonText.replace(/\s+/g, ' ').slice(0, 160) || (mitigated ? 'compensating control in place' : just);
  for (const id of ids) {
    if (seen.has(id)) continue;
    seen.add(id);
    console.log(`\n[[IgnoredVulns]]`);
    console.log(`id = "${id}"`);
    console.log(`ignoreUntil = "${reviewDate}T00:00:00Z"`);
    console.log(`reason = ${JSON.stringify(`VEX ${tag}${loc}: ${detail}`)}`);
    emitted++;
  }
}
if (skippedNoJust) console.error(`Left ${skippedNoJust} not-affected row(s) visible: no CISA justification enum in the verdict/basis column.`);
console.error(`Emitted ${emitted} ignore entries from ${rows.length} rows.`);

function parseCsvLine(line) {
  const out = []; let cur = '', q = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (q) { if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c; }
    else { if (c === '"') q = true; else if (c === ',') { out.push(cur); cur = ''; } else cur += c; }
  }
  out.push(cur); return out;
}
