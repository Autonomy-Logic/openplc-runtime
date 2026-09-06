// Unit tests for the merge-deciding / compliance scripts. Zero-dependency
// (node:test), run with: node --test scripts/__tests__/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { writeFileSync, mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPTS = join(dirname(fileURLToPath(import.meta.url)), '..');
const dir = mkdtempSync(join(tmpdir(), 'sec-test-'));
const w = (name, obj) => { const p = join(dir, name); writeFileSync(p, JSON.stringify(obj)); return p; };
const adv = (id, sev, extra = {}) => ({ id, ...(sev ? { database_specific: { severity: sev } } : {}), ...extra });
const scan = (...pkgs) => ({ results: [{ packages: pkgs.map(([name, vulns]) => ({ package: { name, version: '1' }, vulnerabilities: vulns })) }] });

function gate(base, head, threshold = 'HIGH') {
  const env = { ...process.env, GATE_COMMENT_FILE: join(dir, 'c.md'), GATE_STATUS_FILE: join(dir, 's') };
  return spawnSync('node', [join(SCRIPTS, 'pr-gate-diff.mjs'), base, head, threshold], { env, encoding: 'utf8' });
}

test('gate: MEDIUM does not block (normalized to MODERATE)', () => {
  const r = gate(w('b.json', { results: [] }), w('h.json', scan(['m', [adv('CVE-MED', 'MEDIUM')]])));
  assert.equal(r.status, 0, r.stderr);
});
test('gate: HIGH blocks', () => {
  const r = gate(w('b.json', { results: [] }), w('h.json', scan(['h', [adv('CVE-HI', 'HIGH')]])));
  assert.equal(r.status, 1);
});
test('gate: same CVE on a NEW package is introduced (blocks); on the same package it is not', () => {
  const base = w('b.json', scan(['shared', [adv('CVE-X', 'HIGH')]]));
  const headNew = w('hn.json', scan(['shared', [adv('CVE-X', 'HIGH')]], ['newpkg', [adv('CVE-X', 'HIGH')]]));
  assert.equal(gate(base, headNew).status, 1, 'new package must block');
  const headSame = w('hs.json', scan(['shared', [adv('CVE-X', 'HIGH')]]));
  assert.equal(gate(base, headSame).status, 0, 'pre-existing must not block');
});
test('gate: fails CLOSED (exit 2) on missing or shapeless scan input', () => {
  assert.equal(gate(join(dir, 'nope.json'), w('h.json', { results: [] })).status, 2, 'missing base');
  assert.equal(gate(w('bad.json', { garbage: true }), w('h.json', { results: [] })).status, 2, 'no results array');
});
test('gate: CVSS 3.1 vector (9.8) scored as blocking', () => {
  const head = w('h.json', scan(['c', [adv('CVE-CVSS', null, { severity: [{ type: 'CVSS_V3', score: 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H' }] })]]));
  assert.equal(gate(w('b.json', { results: [] }), head).status, 1);
});
test('gate: PYSEC+GHSA alias group counts once at its real (max) severity, not a double HIGH', () => {
  // osv-scanner emits the PYSEC and GHSA records for one issue separately; the
  // PYSEC one carries no severity (would fall back to approx HIGH and block).
  const head = w('h.json', { results: [{ packages: [{
    package: { name: 'filelock', version: '3.19.1' },
    vulnerabilities: [
      { id: 'PYSEC-2026-1374' },
      { id: 'GHSA-qmgc-5h2g-mvrw', database_specific: { severity: 'MODERATE' } },
    ],
    groups: [{ ids: ['PYSEC-2026-1374', 'GHSA-qmgc-5h2g-mvrw'], max_severity: '5.3' }],
  }] }] });
  const r = gate(w('b.json', { results: [] }), head); // threshold HIGH
  assert.equal(r.status, 0, 'a MODERATE group must not block at HIGH, and PYSEC must not double-count as HIGH');
  assert.match(r.stdout, /introduced by this PR \(1\)/, 'the aliased pair counts once');
  assert.match(r.stdout, /\[MODERATE\] filelock/);
  assert.doesNotMatch(r.stdout, /PYSEC-2026-1374/, 'group represented by its GHSA id, not a duplicate PYSEC row');
});
test('gate: PYSEC-only group with a real max_severity of HIGH still blocks', () => {
  const head = w('h.json', { results: [{ packages: [{
    package: { name: 'x', version: '1' },
    vulnerabilities: [{ id: 'PYSEC-2026-9999' }],
    groups: [{ ids: ['PYSEC-2026-9999'], max_severity: '8.1' }],
  }] }] });
  assert.equal(gate(w('b.json', { results: [] }), head).status, 1, 'a genuine HIGH must still block');
});

function spdx(cdx) {
  const out = join(dir, 'o.spdx.json');
  const r = spawnSync('node', [join(SCRIPTS, 'cdx-to-spdx.mjs'), w('in.cdx.json', cdx), out], { encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
  return JSON.parse(readFileSync(out, 'utf8'));
}
test('cdx-to-spdx: no duplicate SPDXID when a component appears twice', () => {
  const doc = spdx({
    metadata: { component: { 'bom-ref': 'root@1', name: 'root', components: [{ 'bom-ref': 'dup@1', name: 'dup', version: '1' }] } },
    components: [{ 'bom-ref': 'dup@1', name: 'dup', version: '1' }, { 'bom-ref': 'solo@2', name: 'solo', version: '2' }],
  });
  const ids = doc.packages.map((p) => p.SPDXID);
  assert.equal(new Set(ids).size, ids.length, 'SPDXIDs must be unique');
});
test('cdx-to-spdx: multiple licenses join with OR (dual-licensed), not AND', () => {
  const doc = spdx({ components: [{ 'bom-ref': 'x@1', name: 'x', version: '1', licenses: [{ license: { id: 'MIT' } }, { license: { id: 'GPL-3.0-only' } }] }] });
  const x = doc.packages.find((p) => p.name === 'x');
  assert.equal(x.licenseDeclared, 'MIT OR GPL-3.0-only');
});

// ---------- build-report: B1 (VEX numbers derive + reconcile) ----------
const MIN_CFG = {
  product: 'p', title: 'P', subtitle: 's', version: '1', sbomBasename: 'p', securityContact: 'x@y',
  advisories: { total: 0, critical: 0, high: 0, moderate: 0, low: 0 },
  counts: { affected: { n: 0, sev: '' }, mitigated: { n: 0, sev: '' }, notAffected: { n: 0, pct: '0%' } },
  headline: 'h', scope: 's', scopeNote: 's', methodologyNote: 'm', licenseNote: 'l',
  affected: [], mitigated: [],
  notAffected: [{ justification: 'component_not_present / x', count: '?', components: 'c', basis: 'b' }],
  remediation: ['r'], practices: [{ practice: 'p', status: 'In place' }],
};
function report({ baselineToml, raw, delta, cfg = MIN_CFG }) {
  const cdxP = w('r.cdx.json', { components: [] });
  const cfgP = w('cfg.json', cfg);
  const a = ['--config', cfgP, '--cdx', cdxP, '--out', dir, '--date', '2026-08'];
  if (raw) a.push('--osv-raw', w('raw.json', raw));
  if (delta) a.push('--osv-delta', w('delta.json', delta));
  if (baselineToml != null) { const p = join(dir, 'osv.toml'); writeFileSync(p, baselineToml); a.push('--baseline', p); }
  const r = spawnSync('node', [join(SCRIPTS, 'build-report.mjs'), ...a], { encoding: 'utf8' });
  return { ...r, md: r.status === 0 ? readFileSync(join(dir, 'P-Security-Report.md'), 'utf8') : '' };
}
const entry = (id, reason) => `\n[[IgnoredVulns]]\nid = "${id}"\nignoreUntil = "2026-10-01T00:00:00Z"\nreason = "${reason}"\n`;

test('build-report: reconciles raw = surfacing + suppressed, derives §5 count', () => {
  const raw = scan(['x', [adv('GHSA-A', 'CRITICAL')]], ['y', [adv('GHSA-B', 'HIGH')]], ['z', [adv('GHSA-C', 'HIGH')]]);
  const delta = scan(['z', [adv('GHSA-C', 'HIGH')]]); // A,B suppressed; C surfaces
  const baseline = entry('GHSA-A', 'VEX not_affected/component_not_present [x / critical]: dev-only')
    + entry('GHSA-B', 'VEX affected/mitigated [y / high]: control');
  const r = report({ baselineToml: baseline, raw, delta });
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.md, /Not affected — suppressed by the VEX baseline \| 1/);
  assert.match(r.md, /Affected, mitigated[^|]*\| 1/);
  assert.match(r.md, /requires triage \| 1/);
  assert.match(r.md, /component_not_present \/ x` \| 1 /); // §5 count derived, not "?"
});
test('build-report: fails CLOSED when a suppressed advisory has no VEX entry', () => {
  const raw = scan(['x', [adv('GHSA-A', 'HIGH')]], ['z', [adv('GHSA-C', 'HIGH')]]);
  const delta = scan(['z', [adv('GHSA-C', 'HIGH')]]); // A suppressed but not in baseline
  const r = report({ baselineToml: entry('GHSA-OTHER', 'VEX not_affected/component_not_present [q / low]: x'), raw, delta });
  assert.equal(r.status, 1, 'must refuse to render when numbers do not reconcile');
});
test('build-report: matches a GHSA advisory to its CVE-keyed baseline entry (alias)', () => {
  const raw = scan(['x', [adv('GHSA-A', 'HIGH', { aliases: ['CVE-2026-1'] })]], ['z', [adv('GHSA-C', 'HIGH')]]);
  const delta = scan(['z', [adv('GHSA-C', 'HIGH')]]);
  const baseline = entry('CVE-2026-1', 'VEX not_affected/component_not_present [x / high]: dev-only');
  assert.equal(report({ baselineToml: baseline, raw, delta }).status, 0);
});
test('build-report: surfacing>0 with affected:[] never says "None" — DRAFT banner + surfacing table', () => {
  const one = scan(['p', [adv('GHSA-S', 'HIGH', { summary: 'boom' })]]);
  const r = report({ raw: one, delta: one }); // 1 surfacing, nothing suppressed, cfg.affected == []
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.md, /DRAFT — 1 advisory surfacing/);
  assert.match(r.md, /GHSA-S/, 'the surfacing advisory must be listed in §4');
  assert.doesNotMatch(r.md, /\*\*None\.\*\*/, 'must not claim None while an advisory surfaces');
});
test('build-report: surfacing==0 renders §4 None and no DRAFT banner', () => {
  const raw = scan(['p', [adv('GHSA-S', 'HIGH')]]);
  const r = report({ raw, delta: { results: [] } }); // all suppressed, 0 surfacing
  assert.equal(r.status, 0, r.stderr);
  assert.doesNotMatch(r.md, /DRAFT/);
  assert.match(r.md, /\*\*None\.\*\*/);
});

// ---------- gen-osv-ignores: B2 (distinct mitigated status, CISA enum required) ----------
function genOsv(csv) {
  const p = join(dir, 'v.csv'); writeFileSync(p, csv);
  return spawnSync('node', [join(SCRIPTS, 'gen-osv-ignores.mjs'), p, '2026-10-01'], { encoding: 'utf8' });
}
test('gen-osv-ignores: mitigated → affected/mitigated (distinct from not_affected)', () => {
  const out = genOsv('advisory,package,severity,verdict,basis\nCVE-1,dompurify,high,mitigated,allow-list').stdout;
  assert.match(out, /VEX affected\/mitigated \[dompurify \/ high\]/);
  assert.doesNotMatch(out, /not_affected/);
});
test('gen-osv-ignores: not_affected without a CISA enum stays visible (not suppressed)', () => {
  const r = genOsv('advisory,package,verdict,basis\nCVE-2,foo,not_affected,we think it is fine\nCVE-3,bar,not_affected,vulnerable_code_not_in_execute_path');
  assert.doesNotMatch(r.stdout, /CVE-2/);
  assert.match(r.stdout, /CVE-3.*[\s\S]*vulnerable_code_not_in_execute_path/);
});
test('gen-osv-ignores: affected/requires-action row is never suppressed', () => {
  assert.doesNotMatch(genOsv('advisory,package,verdict,basis\nCVE-9,node-forge,affected,upgrade').stdout, /CVE-9/);
});
