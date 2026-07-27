#!/usr/bin/env bash
#
# generate-sbom.sh (Python variant) — Reproducible SBOM for a Python service.
#
# requirements.txt here is UNPINNED, so we resolve it into a CLEAN runtime venv
# and snapshot the fully-resolved environment. cyclonedx-py runs from a SEPARATE
# tool venv pointed at the clean one, so the SBOM captures ONLY the app's real
# dependency tree (tooling never contaminates the resolved set).
#
# Produces under ./sbom/:  <name>.cdx.json (CycloneDX 1.6) · <name>.spdx.json
# (SPDX 2.3) · <name>.components.csv. Name comes from security/report-config.json
# so this is package-manager-agnostic.
#
# Env: SBOM_PIP_ARGS lets a repo force wheels for native deps, e.g.
#      SBOM_PIP_ARGS="--only-binary av"  (aiortc/PyAV).

set -euo pipefail
cd "$(dirname "$0")/.."

NAME="$(node -p "require('./security/report-config.json').sbomBasename")"
OUT="sbom"
mkdir -p "$OUT"
PIP_ARGS="${SBOM_PIP_ARGS:-}"

VENV=".sbom-venv"       # clean runtime env (app deps only)
TOOLS=".sbom-tools"     # cyclonedx-py, isolated from the runtime env
trap 'rm -rf "$VENV" "$TOOLS"' EXIT

echo "==> Resolving requirements into a clean venv for ${NAME}"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --disable-pip-version-check --upgrade pip
# shellcheck disable=SC2086
"$VENV/bin/pip" install --quiet --disable-pip-version-check $PIP_ARGS -r requirements.txt

echo "==> Generating CycloneDX 1.6 SBOM (cyclonedx-py, from the clean env)"
python3 -m venv "$TOOLS"
"$TOOLS/bin/pip" install --quiet --disable-pip-version-check --upgrade pip cyclonedx-bom
"$TOOLS/bin/cyclonedx-py" environment "$VENV" \
  --output-format JSON \
  --spec-version 1.6 \
  --output-file "$OUT/$NAME.cdx.json"

echo "==> Converting to SPDX 2.3"
node scripts/cdx-to-spdx.mjs "$OUT/$NAME.cdx.json" "$OUT/$NAME.spdx.json"

echo "==> Emitting flattened CSV"
NAME="$NAME" OUT="$OUT" node -e '
const fs=require("fs");
const name=process.env.NAME, out=process.env.OUT;
const b=require(`./${out}/${name}.cdx.json`);
const esc=s=>{s=String(s==null?"":s);return /[",\n]/.test(s)?"\""+s.replace(/"/g,"\"\"")+"\"":s;};
const rows=[["name","version","type","purl","license"]];
for(const c of (b.components||[])){
  const lic=(c.licenses||[]).map(l=>l.license?.id||l.expression||l.license?.name||"").join(" / ");
  rows.push([(c.group?c.group+"/":"")+c.name,c.version||"",c.type||"",c.purl||"",lic]);
}
fs.writeFileSync(`${out}/${name}.components.csv`,rows.map(r=>r.map(esc).join(",")).join("\n"));
console.log(`Wrote ${out}/${name}.components.csv:`,rows.length-1,"components");
'

echo "==> Done. Artifacts in ./$OUT/"
ls -la "$OUT"
