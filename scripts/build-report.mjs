#!/usr/bin/env node
// build-report.mjs — render the Security Report (Markdown + HTML) deterministically
// from structured data, so the monthly output is IDENTICAL in shape every run and
// never drifts from the SBOM.
//
//   node scripts/build-report.mjs \
//     --config security/report-config.json \
//     --cdx sbom/<name>.cdx.json \
//     --out security/<YYYY-MM> \
//     --date 2026-08
//
// Component count and license distribution are computed live from the CycloneDX
// SBOM; everything else (VEX triage, narrative) comes from the config, which is
// what Claude updates when a new advisory appears.

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';

const args = Object.fromEntries(process.argv.slice(2).reduce((a, v, i, arr) => {
  if (v.startsWith('--')) a.push([v.slice(2), arr[i + 1]]);
  return a;
}, []));
const cfg = JSON.parse(readFileSync(args.config, 'utf8'));
const cdx = JSON.parse(readFileSync(args.cdx, 'utf8'));
const date = args.date || 'unknown';
const outDir = args.out || '.';
mkdirSync(outDir, { recursive: true });

// --- live metrics from the SBOM ---
const comps = cdx.components || [];
const componentCount = comps.length;
const licAgg = {};
for (const c of comps) {
  for (const l of (c.licenses || [])) {
    const id = l.license?.id || l.expression || l.license?.name || 'Unlicensed';
    licAgg[id] = (licAgg[id] || 0) + 1;
  }
}
const topLicenses = Object.entries(licAgg).sort((a, b) => b[1] - a[1]).slice(0, 8);

// --- live advisory metrics from the actual scan(s), when provided -----------
// Keeps the headline honest: the numbers reflect THIS run's scan, not a static
// value in the config. --osv-raw = unfiltered scan; --osv-delta = scan WITH the
// osv-scanner.toml VEX baseline applied. Falls back to the config numbers if no
// scan is passed (e.g. an ad-hoc local render).
const loadScan = (p) => { try { return JSON.parse(readFileSync(p, 'utf8')); } catch { return null; } };
function idSet(scan) {
  const bySev = { CRITICAL: 0, HIGH: 0, MODERATE: 0, LOW: 0 };
  const ids = new Set();
  const aliases = new Map(); // id -> [aliases]; OSV reports GHSA ids, the baseline keys on CVEs
  for (const r of (scan?.results || [])) for (const p of (r.packages || [])) for (const v of (p.vulnerabilities || [])) {
    if (!v.id || ids.has(v.id)) continue; ids.add(v.id);
    aliases.set(v.id, v.aliases || []);
    let s = (v.database_specific?.severity || '').toUpperCase();
    if (s === 'MEDIUM') s = 'MODERATE';
    if (!(s in bySev)) s = 'MODERATE';
    bySev[s]++;
  }
  return { ids, total: ids.size, bySev, aliases };
}
const sevStr = (b) => `${b.CRITICAL} critical · ${b.HIGH} high · ${b.MODERATE} moderate · ${b.LOW} low`;

// Parse the VEX baseline (osv-scanner.toml) into id -> { status, justification }.
// Reasons are written in a machine-readable form by gen-osv-ignores.mjs / reclassify:
//   VEX not_affected/<cisa_justification> [pkg / sev]: ...
//   VEX affected/mitigated [pkg / sev]: ...
function parseBaseline(p) {
  const map = new Map();
  const src = readFileSync(p, 'utf8');
  for (const block of src.split(/\n(?=\[\[IgnoredVulns\]\])/)) {
    if (!block.includes('[[IgnoredVulns]]')) continue;
    const id = (block.match(/id\s*=\s*"([^"]+)"/) || [])[1];
    const reason = (block.match(/reason\s*=\s*"([^"]*)"/) || [])[1] || '';
    const m = reason.match(/VEX\s+(not_affected|affected)\/(\S+)/);
    if (id && m) map.set(id, { status: m[1], justification: m[2] });
  }
  return map;
}

const rawScan = args['osv-raw'] ? loadScan(args['osv-raw']) : null;
const deltaScan = args['osv-delta'] ? loadScan(args['osv-delta']) : null;
const baseline = args.baseline ? parseBaseline(args.baseline) : null;

// derived = the single source of truth for the report's numbers when a live scan
// is available. Everything reconciles by construction; a mismatch fails the build.
let derived = null;
let advisoryRows;
if (rawScan && deltaScan) {
  const raw = idSet(rawScan), delta = idSet(deltaScan);
  const suppressedIds = [...raw.ids].filter((id) => !delta.ids.has(id));
  const suppressed = suppressedIds.length;

  if (baseline) {
    // Split the suppressed set into CISA buckets straight from the baseline.
    const byJust = {};
    let mitigated = 0;
    const unexplained = [];
    for (const id of suppressedIds) {
      const candidates = [id, ...(raw.aliases.get(id) || [])]; // match GHSA id or its CVE alias
      const c = candidates.map((k) => baseline.get(k)).find(Boolean);
      if (!c) { unexplained.push(id); continue; }
      if (c.status === 'affected' && c.justification === 'mitigated') mitigated++;
      else byJust[c.justification] = (byJust[c.justification] || 0) + 1;
    }
    const notAffected = Object.values(byJust).reduce((a, b) => a + b, 0);

    // Fail-closed reconciliation. A published compliance artifact must add up.
    const errs = [];
    if (unexplained.length) errs.push(`${unexplained.length} suppressed advisories have no VEX entry in the baseline (e.g. ${unexplained.slice(0, 3).join(', ')})`);
    if (notAffected + mitigated !== suppressed) errs.push(`buckets (${notAffected} not-affected + ${mitigated} mitigated) != ${suppressed} suppressed`);
    if (delta.total + suppressed !== raw.total) errs.push(`surfacing (${delta.total}) + suppressed (${suppressed}) != raw (${raw.total})`);
    if (errs.length) {
      console.error('build-report: VEX numbers do not reconcile — refusing to render:\n  - ' + errs.join('\n  - '));
      process.exit(1);
    }
    derived = { raw, delta, suppressed, notAffected, mitigated, byJust };
  }

  const pct = raw.total ? Math.round(100 * suppressed / raw.total) : 0;
  advisoryRows = [
    { label: `Raw advisories detected (this scan · ${date})`, value: `${raw.total} (${sevStr(raw.bySev)})` },
    ...(derived ? [
      { label: 'Not affected — suppressed by the VEX baseline', value: `${derived.notAffected} (${raw.total ? Math.round(100 * derived.notAffected / raw.total) : 0}%)`, badge: 'b-green' },
      { label: 'Affected, mitigated — suppressed with a compensating control', value: `${derived.mitigated}`, badge: 'b-amber' },
    ] : [
      { label: 'Not applicable — suppressed by the VEX baseline', value: `${suppressed} (${pct}%)`, badge: 'b-green' },
    ]),
    { label: 'Surfacing after suppression — requires triage', value: `${delta.total} (${sevStr(delta.bySev)})`, badge: delta.total ? 'b-red' : 'b-green' },
  ];
} else {
  // Same four-bucket shape as the derived path (raw = surfacing + mitigated +
  // not-affected), so a local render never contradicts a CI render.
  advisoryRows = [
    { label: 'Raw advisories detected', value: `${cfg.advisories.total} (${cfg.advisories.critical} critical · ${cfg.advisories.high} high · ${cfg.advisories.moderate} moderate · ${cfg.advisories.low} low)` },
    { label: 'Not affected — suppressed by the VEX baseline', value: `${cfg.counts.notAffected.n} (${cfg.counts.notAffected.pct})`, badge: 'b-green' },
    { label: 'Affected, mitigated — suppressed with a compensating control', value: `${cfg.counts.mitigated.n} (${cfg.counts.mitigated.sev})`, badge: 'b-amber' },
    { label: 'Surfacing after suppression — requires triage', value: `${cfg.counts.affected.n} (${cfg.counts.affected.sev})`, badge: 'b-red' },
  ];
}

// §5/§6/§9 counts come from `derived` when a scan+baseline are present, so the body
// tables can never contradict the headline. cfg values remain the fallback.
const justCount = (justification) => {
  if (!derived) return null;
  // config justification strings may prefix/annotate the enum — match by containment
  for (const [enumKey, n] of Object.entries(derived.byJust)) if (justification.includes(enumKey)) return n;
  return 0;
};
// The advisory register (vulnerabilities.csv) is produced from the DELTA scan, so
// its row count must come from the delta scan whenever a live scan is available —
// never from the static config (which drifts). Independent of the VEX baseline.
const registerRows = deltaScan ? idSet(deltaScan).total : cfg.advisories.total;

// The narrative sections (§4 affected, headline, criticalNote, mitigated) come
// from the config, which is NOT auto-updated once AI triage is removed. If the
// live scan surfaces advisories the config doesn't reflect, the document must not
// claim "none require remediation": it renders a DRAFT banner and lists the
// surfacing advisories from the scan, so live metrics and static narrative can
// never disagree in a published attestation.
function surfacingRows(scan) {
  const seen = new Set(); const out = [];
  for (const r of (scan?.results || [])) for (const p of (r.packages || [])) for (const v of (p.vulnerabilities || [])) {
    const pkg = p.package?.name || '?';
    const key = `${v.id}|${pkg}`;
    if (!v.id || seen.has(key)) continue; seen.add(key);
    let sev = (v.database_specific?.severity || '').toUpperCase();
    if (sev === 'MEDIUM') sev = 'MODERATE';
    out.push({ id: v.id, pkg, sev: sev || 'UNKNOWN', summary: String(v.summary || '').replace(/\s+/g, ' ').slice(0, 140) });
  }
  return out.sort((a, b) => a.pkg.localeCompare(b.pkg) || a.id.localeCompare(b.id));
}
const surfacing = deltaScan ? surfacingRows(deltaScan) : [];
const isDraft = surfacing.length > 0;
const draftLine = `DRAFT — ${surfacing.length} advisor${surfacing.length === 1 ? 'y' : 'ies'} surfacing above the VEX baseline await triage. Sections 4–7 reflect the last triaged state and may be stale until a reviewer updates report-config.json.`;

const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
// report-config.json is written by the AI triage step, so its strings are
// UNTRUSTED. Escape everything, then re-enable only a small set of attribute-less
// formatting tags. This blocks <script>, <img onerror=…>, <b onmouseover=…>, etc.
// from reaching the headless Chrome that renders the PDF (which also runs with
// JavaScript disabled — see the workflow).
const RICH_TAGS = /&lt;(\/?(?:b|strong|code|em|i|br))&gt;/gi;
const rich = (s) => esc(s).replace(RICH_TAGS, '<$1>');
const stripTags = (s) => String(s ?? '').replace(/<[^>]+>/g, '');

// ========================= MARKDOWN =========================
const md = [];
md.push(`# ${cfg.title} — Software Supply Chain Security Report (SBOM & VEX)\n`);
if (isDraft) md.push(`> **${draftLine}**\n`);
md.push('| | |');
md.push('|---|---|');
md.push(`| **Product** | ${cfg.title} (\`${cfg.product}\`) — ${cfg.subtitle} · v${cfg.version} |`);
md.push(`| **Report type** | Software Bill of Materials (SBOM) & Vulnerability Exploitability eXchange (VEX) |`);
md.push(`| **Assessment date** | ${date} · Report version 1.0 |`);
md.push(`| **Prepared by** | Autonomy Logic — Engineering / Product Security |`);
md.push(`| **Classification** | Confidential |`);
md.push(`| **Security contact** | ${cfg.securityContact} |`);
md.push('\n---\n');
md.push('## Executive Summary\n');
md.push(`This report documents the third-party software composition and known-vulnerability posture of **${cfg.title}**. It is aligned with U.S. Executive Order 14028, the NTIA *Minimum Elements for an SBOM*, and the CISA Vulnerability Exploitability eXchange (VEX) guidance.\n`);
md.push(`> **Headline posture.** ${stripTags(cfg.headline)}\n`);
md.push('### Key metrics\n');
md.push('| Metric | Value |');
md.push('|---|---|');
md.push(`| Components inventoried (full transitive graph) | **${componentCount}** |`);
for (const r of advisoryRows) md.push(`| ${r.label} | ${r.value} |`);
md.push('\n## 1. Scope & System Description\n');
md.push(stripTags(cfg.scope) + '\n');
md.push(`> **Scope note.** ${stripTags(cfg.scopeNote)}\n`);
md.push('## 2. Methodology\n');
md.push(stripTags(cfg.methodologyNote) + ' For each relevant package, source code was analyzed to determine whether the vulnerable code path is invoked and whether its input is attacker-controlled.\n');
md.push('## 3. Software Bill of Materials Summary\n');
md.push('| License | Components |');
md.push('|---|---|');
for (const [l, n] of topLicenses) md.push(`| ${l} | ${n} |`);
md.push(`\n**License finding:** ${stripTags(cfg.licenseNote)}\n`);
md.push('## 4. Findings Requiring Remediation (Affected)\n');
if (isDraft) {
  md.push(`_Surfacing this scan — awaiting triage (${surfacing.length}):_\n`);
  md.push('| Advisory | Package | Severity | Summary |');
  md.push('|---|---|---|---|');
  for (const s of surfacing) md.push(`| ${s.id} | \`${s.pkg}\` | ${s.sev} | ${stripTags(s.summary)} |`);
  md.push('');
}
if (cfg.affected.length) {
  if (isDraft) md.push('_Previously triaged priorities (may be stale):_\n');
  md.push('| Priority | Component | Installed | Fixed in | Severity | Reachability rationale |');
  md.push('|---|---|---|---|---|---|');
  for (const f of cfg.affected) md.push(`| ${f.priority} | \`${f.component}\` | ${f.installed} | ${f.fixedIn} | ${f.severity} | ${stripTags(f.rationale)} |`);
} else if (!isDraft) md.push('**None.**');
md.push('\n## 5. Not Affected — VEX Justifications\n');
md.push('| VEX justification (CISA) | Count | Representative components | Basis |');
md.push('|---|---|---|---|');
for (const n of cfg.notAffected) md.push(`| \`${n.justification}\` | ${justCount(n.justification) ?? n.count} | ${stripTags(n.components)} | ${stripTags(n.basis)} |`);
if (cfg.criticalNote) md.push(`\n**On critical severity.** ${stripTags(cfg.criticalNote)}\n`);
md.push('\n## 6. Mitigated Findings\n');
md.push('| Component | Advisories | Existing control |');
md.push('|---|---|---|');
for (const m of cfg.mitigated) md.push(`| \`${m.component}\` | ${m.advisories} | ${stripTags(m.control)} |`);
md.push('\n## 7. Remediation Plan\n');
for (const r of cfg.remediation) md.push(`- ${stripTags(r)}`);
md.push('\n## 8. Secure Development & Supply-Chain Practices\n');
md.push('| Practice | Status |');
md.push('|---|---|');
for (const p of cfg.practices) md.push(`| ${p.practice} | ${p.status} |`);
md.push('\n## 9. Attached Artifacts\n');
md.push('| Artifact | Format | Purpose |');
md.push('|---|---|---|');
md.push(`| \`sbom/${cfg.sbomBasename}.cdx.json\` | CycloneDX 1.6 | Canonical machine-readable SBOM |`);
md.push(`| \`sbom/${cfg.sbomBasename}.spdx.json\` | SPDX 2.3 (ISO/IEC 5962) | Procurement / compliance SBOM |`);
md.push(`| \`sbom/${cfg.sbomBasename}.components.csv\` | CSV | Human-readable component inventory (${componentCount} rows) |`);
md.push(`| \`sbom/vulnerabilities.csv\` | CSV | Full annotated advisory register (${registerRows} rows) |`);
md.push(`\n---\n\n*Prepared by Autonomy Logic Engineering. Assessment date ${date}. Regenerate per release.*\n`);
const mdOut = md.join('\n');

// ========================= HTML =========================
const sevBadge = (s) => s; // severity strings already human
const row = (cells) => `<tr>${cells.map((c) => `<td>${c}</td>`).join('')}</tr>`;
const th = (cells) => `<tr>${cells.map((c) => `<th>${c}</th>`).join('')}</tr>`;
const badge = (txt, cls) => `<span class="badge ${cls}">${txt}</span>`;

const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>${esc(cfg.title)} — Security Report (SBOM & VEX)</title>
<style>
:root{--ink:#1a1f29;--muted:#5b6472;--line:#e3e7ee;--accent:#1f4b8e;--red:#c0392b;--amber:#b7791f;--green:#237a4b;--purple:#6b3fa0;--blue:#2b6cb0;}
*{box-sizing:border-box;}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--ink);line-height:1.55;max-width:940px;margin:0 auto;padding:48px 32px;}
h1{font-size:1.9rem;border-bottom:3px solid var(--accent);padding-bottom:.3em;margin-top:1.8em;}h1:first-of-type{margin-top:.2em;}
h2{font-size:1.25rem;margin-top:1.6em;color:var(--accent);}p,li{font-size:.95rem;}
code{background:#f2f4f8;padding:1px 5px;border-radius:4px;font-size:.86em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
table{border-collapse:collapse;width:100%;margin:1em 0;font-size:.86rem;}th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top;}th{background:#f6f8fb;font-weight:600;}
.meta td:first-child{font-weight:600;width:190px;background:#fafbfd;}
.callout{border-left:4px solid var(--accent);background:#f4f8ff;padding:14px 18px;border-radius:0 8px 8px 0;margin:1.2em 0;}.callout.good{border-color:var(--green);background:#f1f9f4;}.callout.warn{border-color:var(--red);background:#fdf1ef;}
.badge{display:inline-block;padding:1px 8px;border-radius:20px;font-size:.72rem;font-weight:700;color:#fff;}.b-red{background:var(--red);}.b-amber{background:var(--amber);}.b-green{background:var(--green);}
.muted{color:var(--muted);font-size:.85rem;}hr{border:0;border-top:1px solid var(--line);margin:2.5em 0;}
@media print{body{padding:0;max-width:none;}h1{page-break-after:avoid;}table{page-break-inside:avoid;}a{color:var(--ink);text-decoration:none;}}
</style></head><body>
<h1 style="border:0;margin-bottom:0;">${esc(cfg.title)}</h1>
<p class="muted" style="margin-top:0;">Software Supply Chain Security Report — SBOM &amp; VEX</p>
${isDraft ? `<div class="callout warn"><strong>${esc(draftLine)}</strong></div>` : ''}
<table class="meta">
${row(['Product', `${esc(cfg.title)} (<code>${esc(cfg.product)}</code>) — ${esc(cfg.subtitle)} · v${esc(cfg.version)}`])}
${row(['Report type', 'Software Bill of Materials (SBOM) &amp; Vulnerability Exploitability eXchange (VEX)'])}
${row(['Assessment date', `${esc(date)} · Report version 1.0`])}
${row(['Prepared by', 'Autonomy Logic — Engineering / Product Security'])}
${row(['Classification', 'Confidential'])}
${row(['Security contact', esc(cfg.securityContact)])}
</table>
<h1>Executive Summary</h1>
<p>This report documents the third-party software composition and known-vulnerability posture of <strong>${esc(cfg.title)}</strong>. It is aligned with U.S. Executive Order 14028, the NTIA <em>Minimum Elements for an SBOM</em>, and the CISA VEX guidance.</p>
<div class="callout good"><strong>Headline posture.</strong> ${rich(cfg.headline)}</div>
<h2>Key metrics</h2>
<table>${th(['Metric', 'Value'])}
${row(['Components inventoried (full transitive graph)', `<strong>${componentCount}</strong>`])}
${advisoryRows.map((r) => row([r.badge ? badge(r.label, r.badge) : esc(r.label), esc(r.value)])).join('\n')}
</table>
<h1>1. Scope &amp; System Description</h1><p>${rich(cfg.scope)}</p>
<div class="callout"><strong>Scope note.</strong> ${rich(cfg.scopeNote)}</div>
<h1>2. Methodology</h1><p>${rich(cfg.methodologyNote)} For each relevant package, source code was analyzed to determine whether the vulnerable code path is invoked and whether its input is attacker-controlled.</p>
<h1>3. Software Bill of Materials Summary</h1>
<table>${th(['License', 'Components'])}
${topLicenses.map(([l, n]) => row([esc(l), String(n)])).join('\n')}
</table>
<div class="callout"><strong>License finding:</strong> ${rich(cfg.licenseNote)}</div>
<h1>4. Findings Requiring Remediation (Affected)</h1>
${isDraft ? `<p class="muted"><strong>Surfacing this scan — awaiting triage (${surfacing.length}):</strong></p>
<table>${th(['Advisory', 'Package', 'Severity', 'Summary'])}
${surfacing.map((s) => row([esc(s.id), `<code>${esc(s.pkg)}</code>`, esc(s.sev), esc(s.summary)])).join('\n')}
</table>` : ''}
${cfg.affected.length ? `${isDraft ? '<p class="muted"><strong>Previously triaged priorities (may be stale):</strong></p>' : ''}<table>${th(['Priority', 'Component', 'Installed', 'Fixed in', 'Severity', 'Reachability rationale'])}
${cfg.affected.map((f) => row([f.priority, `<code>${esc(f.component)}</code>`, esc(f.installed), esc(f.fixedIn), f.severity, rich(f.rationale)])).join('\n')}
</table>` : (isDraft ? '' : '<div class="callout good"><strong>None.</strong></div>')}
<h1>5. Not Affected — VEX Justifications</h1>
<table>${th(['VEX justification (CISA)', 'Count', 'Representative components', 'Basis'])}
${cfg.notAffected.map((n) => row([`<code>${esc(n.justification)}</code>`, String(justCount(n.justification) ?? n.count), rich(n.components), rich(n.basis)])).join('\n')}
</table>
${cfg.criticalNote ? `<div class="callout"><strong>On critical severity.</strong> ${rich(cfg.criticalNote)}</div>` : ''}
<h1>6. Mitigated Findings</h1>
<table>${th(['Component', 'Advisories', 'Existing control'])}
${cfg.mitigated.map((m) => row([`<code>${esc(m.component)}</code>`, m.advisories, rich(m.control)])).join('\n')}
</table>
<h1>7. Remediation Plan</h1><ul>${cfg.remediation.map((r) => `<li>${rich(r)}</li>`).join('')}</ul>
<h1>8. Secure Development &amp; Supply-Chain Practices</h1>
<table>${th(['Practice', 'Status'])}
${cfg.practices.map((p) => row([esc(p.practice), p.status === 'In place' ? badge('In place', 'b-green') : badge('Recommended', 'b-amber')])).join('\n')}
</table>
<h1>9. Attached Artifacts</h1>
<table>${th(['Artifact', 'Format', 'Purpose'])}
${row([`<code>sbom/${cfg.sbomBasename}.cdx.json</code>`, 'CycloneDX 1.6', 'Canonical machine-readable SBOM'])}
${row([`<code>sbom/${cfg.sbomBasename}.spdx.json</code>`, 'SPDX 2.3 (ISO/IEC 5962)', 'Procurement / compliance SBOM'])}
${row([`<code>sbom/${cfg.sbomBasename}.components.csv</code>`, 'CSV', `Human-readable component inventory (${componentCount} rows)`])}
${row(['<code>sbom/vulnerabilities.csv</code>', 'CSV', `Full annotated advisory register (${registerRows} rows)`])}
</table>
<hr/><p class="muted">Prepared by Autonomy Logic Engineering. Assessment date ${esc(date)}. Regenerate per release.</p>
</body></html>`;

const base = `${cfg.title.replace(/[^A-Za-z0-9]+/g, '-')}-Security-Report`;
writeFileSync(`${outDir}/${base}.md`, mdOut);
writeFileSync(`${outDir}/${base}.html`, html);
console.log(`Wrote ${outDir}/${base}.md and .html (${componentCount} components, ${topLicenses.length} license classes)`);
