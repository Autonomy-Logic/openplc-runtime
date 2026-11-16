/**
 * Generates the HMI of the end-to-end test with the editor's own generator (non-preview): one screen with a switch
 * writing instance0.Main.Run, a lamp showing it and a value display of instance0.Main.Level. Tag addresses come from
 * the program bundle's debug-map.json and the MD5 from its defines.h. Writes <out>/hmi/{manifest.json,src/,client/}.
 *
 * Run from the editor worktree root:
 *   TS_NODE_PROJECT=<editor root>/tsconfig.json npx ts-node -T -r tsconfig-paths/register <this file> \
 *     <editor root> <bundle src dir> <out dir>
 */
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

interface TagAddress {
  arr: number
  elem: number
}
interface Diagnostic {
  file: string
  message: string
  line?: number
}
interface UnitResult {
  ok: boolean
  errors: Diagnostic[]
  warnings: Diagnostic[]
  files: Record<string, string>
  tags: Array<{ path: string; type: number; maxLength: number; address: TagAddress | null }>
  elementIds: Array<{ id: number; screen: string; element: string }>
  fonts: { variant: 'full' | 'compact'; faces: number[] }
}
interface GeneratorModule {
  generateHmiUnit: (input: object) => UnitResult
}
interface SettingsModule {
  hmiSettingsSchema: { parse: (value: object) => object }
}
interface FontsModule {
  buildHmiFontTableSource: (fonts: Array<{ id: number; variant: string; bytes: Uint8Array }>) => Promise<string>
}
interface VettedModule {
  vettedHmiFont: (id: number, variant: string) => { file: string } | undefined
}
interface DebugLeaf {
  arrayIdx: number
  elemIdx: number
  path: string
}

export const SCREEN = `FUNCTION_BLOCK Overview EXTENDS HmiScreen
  VAR
    RunSwitch : HmiSwitch := (Caption := 'Run');
    RunLamp : HmiLamp := (Caption := 'Running');
    LevelValue : HmiValue := (Caption := 'Level', Decimals := 1);
  END_VAR
  RunSwitch.Value := instance0.Main.Run;
  RunLamp.Value := instance0.Main.Run;
  LevelValue.Value := instance0.Main.Level;
END_FUNCTION_BLOCK
`

/** Main's declarations and the configuration, as screens compile against them. */
const PLC_DECLARATIONS = `PROGRAM Main
  VAR
    Run : BOOL;
    Level : REAL;
  END_VAR
  ;
END_PROGRAM
CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK task0(INTERVAL := T#20ms, PRIORITY := 1);
    PROGRAM instance0 WITH task0 : Main;
  END_RESOURCE
END_CONFIGURATION
`

/** Program instance name to program type, upper case, as in the configuration above. */
const INSTANCES: Record<string, string> = { INSTANCE0: 'MAIN' }

/**
 * Debug address of a screen tag. The debug map names a program variable `<INSTANCE>.<VAR>` (no program type, no
 * configuration/resource prefix); the generator's paths may carry both, so they are dropped here.
 */
function resolver(leaves: DebugLeaf[], seen: string[]): (path: string) => TagAddress | null {
  const byPath = new Map(leaves.map((l) => [l.path.toUpperCase(), { arr: l.arrayIdx, elem: l.elemIdx }]))
  return (path: string) => {
    seen.push(path)
    let p = path.toUpperCase()
    for (const prefix of ['CONFIG0.RES0.', 'CONFIG0.']) if (p.startsWith(prefix)) p = p.slice(prefix.length)
    const parts = p.split('.')
    if (parts.length >= 3 && INSTANCES[parts[0]] === parts[1]) p = [parts[0], ...parts.slice(2)].join('.')
    return byPath.get(p) ?? null
  }
}

async function main(): Promise<void> {
  const [editor, bundle, out] = process.argv.slice(2)
  if (!editor || !bundle || !out) throw new Error('usage: gen_unit.ts <editor root> <bundle src dir> <out dir>')
  const gen = require(join(editor, 'src/backend/shared/hmi/unit/generate-hmi-unit')) as GeneratorModule
  const settingsMod = require(join(editor, 'src/middleware/shared/utils/hmi/settings')) as SettingsModule
  const fontsMod = require(join(editor, 'src/backend/shared/hmi/fonts/font-table-source')) as FontsModule
  const vettedMod = require(join(editor, 'src/backend/shared/hmi/fonts/vetted-fonts')) as VettedModule

  const debugMap = JSON.parse(readFileSync(join(bundle, 'debug-map.json'), 'utf8')) as { leaves: DebugLeaf[] }
  const md5 = /PROGRAM_MD5 "([0-9a-f]{32})"/.exec(readFileSync(join(bundle, 'defines.h'), 'utf8'))?.[1]
  if (!md5) throw new Error('no PROGRAM_MD5 in defines.h')
  const seen: string[] = []
  const result = gen.generateHmiUnit({
    screens: [{ name: 'Overview', source: SCREEN }],
    widgets: [],
    settings: settingsMod.hmiSettingsSchema.parse({
      startScreen: 'Overview',
      framePeriodMs: 100,
      roles: { Overview: { engineer: true, operator: true, viewer: true } },
    }),
    plcVariables: [
      { path: 'instance0.Main.Run', type: 'BOOL' },
      { path: 'instance0.Main.Level', type: 'REAL' },
    ],
    plcDeclarations: PLC_DECLARATIONS,
    projectNames: ['Main'],
    resolve: resolver(debugMap.leaves, seen),
    plcMd5: md5,
  })
  if (!result.ok) throw new Error(`generator failed: ${JSON.stringify(result.errors)}`)
  if (result.tags.some((t) => t.address === null))
    throw new Error(`unresolved tags ${JSON.stringify(result.tags)} (resolve saw ${JSON.stringify(seen)})`)

  const hmi = join(out, 'hmi')
  const src = join(hmi, 'src')
  const core = join(src, 'core')
  const client = join(hmi, 'client')
  for (const d of [src, core, client]) mkdirSync(d, { recursive: true })
  for (const [name, text] of Object.entries(result.files)) writeFileSync(join(src, name), text)
  writeFileSync(join(src, 'hmi_unit.h'), readFileSync(join(editor, 'resources/sources/hmi/unit/hmi_unit.h')))
  const coreDir = join(editor, 'resources/sources/hmi/core')
  for (const f of readdirSync(coreDir)) if (/\.(c|h)$/.test(f)) writeFileSync(join(core, f), readFileSync(join(coreDir, f)))
  mkdirSync(join(core, 'third_party'), { recursive: true })
  writeFileSync(join(core, 'third_party', 'stb_truetype.h'), readFileSync(join(coreDir, 'third_party', 'stb_truetype.h')))
  const fonts = result.fonts.faces.map((id) => {
    const vetted = vettedMod.vettedHmiFont(id, result.fonts.variant)
    if (!vetted) throw new Error(`font ${id} not vetted`)
    return {
      id,
      variant: result.fonts.variant,
      bytes: new Uint8Array(readFileSync(join(editor, 'resources/sources/hmi/fonts', vetted.file))),
    }
  })
  writeFileSync(join(src, 'hmi_fonts.c'), await fontsMod.buildHmiFontTableSource(fonts))
  writeFileSync(join(hmi, 'manifest.json'), '{"format": 1}\n')
  writeFileSync(
    join(client, 'index.html'),
    '<!doctype html><html><head><meta charset="utf-8"><title>HMI e2e</title></head><body>HMI e2e client</body></html>\n',
  )
  writeFileSync(
    join(out, 'unit.json'),
    JSON.stringify({ md5, tags: result.tags, elementIds: result.elementIds, resolved: seen, warnings: result.warnings }, null, 2),
  )
  console.log(`unit generated: ${Object.keys(result.files).length} files, tags ${JSON.stringify(result.tags)}`)
}

main().catch((e: unknown) => {
  console.error(e instanceof Error ? e.message : e)
  process.exit(1)
})
