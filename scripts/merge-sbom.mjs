#!/usr/bin/env node
// merge-sbom.mjs — combine per-ecosystem CycloneDX BOMs and the hand-maintained
// component list into one product SBOM.
//
//   node scripts/merge-sbom.mjs --out <cdx.json> --name <product> --version <v> --license <spdx-id>
//        [--merge <cdx.json>]...  [--nest <cdx.json>]... [--nest-version <v>]
//        [--components <components.json>] [--root <dir>]
//
// --merge  the input's root is a placeholder: its components become the product's own.
// --nest   the input's root is a shipped sub-application of this repository: it
//          stays as a component the product depends on, with its own dependency
//          edges, and takes the product licence when it declares none, and
//          --nest-version when it declares no version.
// --components  security/components.json: vendored or fetched code no package
//          manager declares. An entry with versionFrom {file, pattern} has its
//          version checked against that file (relative to --root, default cwd);
//          a mismatch exits 1 so the list cannot silently drift from the code.

import { readFileSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { refOf } from './lib/cdx.mjs';

const opts = { merge: [], nest: [] };
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i += 2) {
  const k = argv[i]?.replace(/^--/, '');
  const v = argv[i + 1];
  if (!k || v === undefined) usage(`missing value for ${argv[i]}`);
  if (k === 'merge' || k === 'nest') opts[k].push(v);
  else opts[k] = v;
}
if (!opts.out || !opts.name || !opts.version) usage('--out, --name and --version are required');

function usage(msg) {
  console.error(`merge-sbom: ${msg}\nusage: merge-sbom.mjs --out <cdx.json> --name <n> --version <v> [--license <id>] [--merge <cdx>]... [--nest <cdx>]... [--nest-version <v>] [--components <json>] [--root <dir>]`);
  process.exit(1);
}
const fail = (msg) => { console.error(`::error::merge-sbom: ${msg}`); process.exit(1); };
const readJson = (p) => {
  try { return JSON.parse(readFileSync(p, 'utf8')); } catch (e) { return fail(`cannot read/parse "${p}" (${e.code || e.message})`); }
};

const rootRef = `pkg:github/autonomy-logic/${opts.name}@${opts.version}`;
const root = {
  type: 'application',
  name: opts.name,
  version: opts.version,
  'bom-ref': rootRef,
  purl: rootRef,
  ...(opts.license ? { licenses: [{ license: { id: opts.license } }] } : {}),
};

const components = [];
const seen = new Set();
const add = (c) => { const r = refOf(c); if (!seen.has(r)) { seen.add(r); components.push(c); } };
const deps = new Map();
const addDeps = (ref, on) => {
  const set = deps.get(ref) || new Set();
  for (const d of on) if (d !== ref) set.add(d);
  deps.set(ref, set);
};
const rootDeps = new Set();
let tools = [];

function absorb(path, nest) {
  const bom = readJson(path);
  if (bom.bomFormat && bom.bomFormat !== 'CycloneDX') fail(`"${path}" is not CycloneDX`);
  const inRoot = bom.metadata?.component;
  const inRootRef = inRoot ? refOf(inRoot) : null;
  tools = tools.concat(bom.metadata?.tools?.components || []);
  if (nest && inRoot) {
    const { components: nested = [], ...self } = inRoot;
    if (!(self.licenses || []).length && root.licenses) self.licenses = root.licenses;
    if (!self.version && opts['nest-version']) self.version = opts['nest-version'];
    add(self);
    nested.forEach(add);
    rootDeps.add(inRootRef);
  }
  (bom.components || []).forEach(add);
  for (const d of bom.dependencies || []) {
    const on = d.dependsOn || [];
    if (!nest && d.ref === inRootRef) on.forEach((x) => rootDeps.add(x));
    else addDeps(d.ref, on.filter((x) => nest || x !== inRootRef));
  }
}
opts.merge.forEach((p) => absorb(p, false));
opts.nest.forEach((p) => absorb(p, true));

if (opts.components) {
  const list = readJson(opts.components).components;
  if (!Array.isArray(list)) fail(`"${opts.components}" has no "components" array`);
  for (const entry of list) {
    const { versionFrom, ...c } = entry;
    if (!c.name || !c.version || !c.purl || !(c.licenses || []).length) {
      fail(`hand-maintained component ${JSON.stringify(c.name)} needs name, version, purl and licenses`);
    }
    if (versionFrom) {
      let text;
      try { text = readFileSync(join(opts.root || '.', versionFrom.file), 'utf8'); } catch (e) {
        fail(`${c.name}: versionFrom file "${versionFrom.file}" is unreadable (${e.code || e.message})`);
      }
      const m = text.match(new RegExp(versionFrom.pattern));
      if (!m) fail(`${c.name}: pattern not found in ${versionFrom.file}`);
      const found = m.slice(1).join('.');
      if (found !== c.version) fail(`${c.name}: components.json says ${c.version} but ${versionFrom.file} says ${found}`);
    }
    c['bom-ref'] = c['bom-ref'] || c.purl;
    add(c);
    rootDeps.add(c['bom-ref']);
  }
}

const toolSeen = new Set();
tools = tools.filter((t) => { const k = `${t.name}@${t.version}`; if (toolSeen.has(k)) return false; toolSeen.add(k); return true; });

const dependencies = [{ ref: rootRef, dependsOn: [...rootDeps].sort() }];
for (const [ref, on] of deps) dependencies.push({ ref, dependsOn: [...on].sort() });

const bom = {
  bomFormat: 'CycloneDX',
  specVersion: '1.6',
  serialNumber: `urn:uuid:${randomUUID()}`,
  version: 1,
  metadata: {
    timestamp: new Date().toISOString(),
    tools: { components: tools },
    lifecycles: [{ phase: 'build' }],
    component: root,
  },
  components,
  dependencies,
};
writeFileSync(opts.out, JSON.stringify(bom, null, 2) + '\n');
console.log(`Wrote ${opts.out}: root ${opts.name}@${opts.version} + ${components.length} components`);
