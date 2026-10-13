/**
 * Builds the Runtime v4 upload bundles used by the MQTT end-to-end tests through the Editor's
 * real compile pipeline and the real STruC++ compiler. Runs under the Editor's Jest (see
 * ../run.sh); writes one directory per scenario into MQTT_E2E_PAYLOAD_OUT.
 */

import { createHash } from 'node:crypto'
import { mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

import hals from '@root/backend/shared/firmware/hals.json'
import { __setStrucppRuntimeForTests } from '@root/backend/shared/library/strucpp-runtime'
import type { PLCProjectData } from '@root/backend/shared/types/PLC/open-plc'
import { runCompilePipeline, type RunCompilePipelineArgs } from '@root/backend/shared/compile/pipeline'
import type {
  MqttProjectIntegrationV1,
  MqttProjectSelectionV1,
  MqttSourceToken,
} from '@root/frontend/utils/mqtt/contract/contract'
import { createDefaultMqttIntegration } from '@root/frontend/utils/mqtt/project/model'
import type { CompilerPlatformPort } from '@root/middleware/shared/ports/compiler-platform-port'

const OUT = process.env.MQTT_E2E_PAYLOAD_OUT ?? ''
const HEADERS_DIR = join(process.cwd(), 'node_modules', 'strucpp', 'src', 'runtime', 'include')

const LONG_TEXT = 'L'.repeat(126)

const E2E_ST = `TYPE
  Reading : STRUCT
    Value : REAL;
    Count : DINT;
  END_STRUCT;
END_TYPE

FUNCTION_BLOCK Sensor
  VAR_INPUT
    Base : DINT;
  END_VAR
  VAR_OUTPUT
    Out : Reading;
    Seen : DINT;
  END_VAR
  Seen := Seen + 1;
  Out.Count := Base + Seen;
  Out.Value := 0.5;
END_FUNCTION_BLOCK

PROGRAM Main
  VAR
    Counter : DINT;
    Toggle : BOOL;
    Mb AT %QW0 : INT;
    Phase : DINT;
    Neg : REAL := -1.0;
    Flaky : REAL;
    Grid : ARRAY[-2..1, 3..4] OF INT;
    Vec : ARRAY[-1..1] OF Reading;
    S1 : Sensor;
    S2 : Sensor;
    LMin : LINT := -9223372036854775807 - 1;
    LMax : LINT := 9223372036854775807;
    UMax : ULINT := 18446744073709551615;
    WMax : LWORD := 16#FFFFFFFFFFFFFFFF;
    Unsafe : LINT := 9007199254740993;
    S8 : SINT := -128;
    U8 : USINT := 255;
    I16 : INT := -32768;
    U16 : UINT := 65535;
    D32 : DINT := -2147483647 - 1;
    U32 : UDINT := 4294967295;
    Bt : BYTE := 16#A5;
    Wd : WORD := 16#BEEF;
    Dw : DWORD := 16#DEADBEEF;
    Lr : LREAL := 0.1;
    T1 : TIME;
    Tn : TIME := T#1s2ms3us4ns;
    Td : TOD := TOD#12:34:56.789;
    D1 : DATE := D#2024-02-29;
    Dt1 : DT := DT#2024-02-29-12:34:56.789;
    Txt : STRING := 'line "1" $5C end';
    LongTxt : STRING := '${LONG_TEXT}';
    W : WSTRING := "A$D83D$DE00$0042";
    WOdd : WSTRING := "$D800x";
  END_VAR
  Counter := Counter + 1;
  Toggle := ((Counter / 25) MOD 2) = 1;
  Mb := DINT_TO_INT(Counter MOD 30000);
  T1 := T#0ms - T#1s500ms;
  Phase := (Counter / 50) MOD 2;
  IF Phase = 1 THEN
    Flaky := SQRT(Neg);
  ELSE
    Flaky := 0.25;
  END_IF;
  Grid[-2, 3] := Grid[-2, 3] + 1;
  Grid[1, 4] := Grid[1, 4] - 1;
  Vec[-1].Count := Counter;
  Vec[1].Value := DINT_TO_REAL(Counter);
  S1(Base := 1000);
  S2(Base := 2000);
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK task0(INTERVAL := T#10ms, PRIORITY := 0);
    PROGRAM instance0 WITH task0 : Main;
  END_RESOURCE
END_CONFIGURATION
`

const BENCH_ST = `PROGRAM Main
  VAR
    Arr : ARRAY[1..1000] OF DINT;
    I : DINT;
  END_VAR
  FOR I := 1 TO 1000 DO
    Arr[I] := Arr[I] + 1;
  END_FOR;
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK task0(INTERVAL := T#10ms, PRIORITY := 0);
    PROGRAM instance0 WITH task0 : Main;
  END_RESOURCE
END_CONFIGURATION
`

interface TypeRef {
  definition: string
  value: string
  data?: { baseType: { definition: string; value: string }; dimensions: Array<{ dimension: string }> }
}

const base = (value: string): TypeRef => ({ definition: 'base-type', value })
const arrayOf = (value: string, baseType: TypeRef, dims: string[]): TypeRef => ({
  definition: 'array',
  value,
  data: {
    baseType: { definition: baseType.definition, value: baseType.value },
    dimensions: dims.map((d) => ({ dimension: d })),
  },
})
const variable = (name: string, type: TypeRef, cls = 'local', location = '') => ({
  name,
  class: cls,
  type,
  location,
  documentation: '',
})

const E2E_VARIABLES = [
  variable('Counter', base('DINT')),
  variable('Toggle', base('BOOL')),
  variable('Mb', base('INT'), 'local', '%QW0'),
  variable('Phase', base('DINT')),
  variable('Neg', base('REAL')),
  variable('Flaky', base('REAL')),
  variable('Grid', arrayOf('ARRAY[-2..1, 3..4] OF INT', base('INT'), ['-2..1', '3..4'])),
  variable('Vec', arrayOf('ARRAY[-1..1] OF Reading', { definition: 'user-data-type', value: 'Reading' }, ['-1..1'])),
  variable('S1', { definition: 'derived', value: 'Sensor' }),
  variable('S2', { definition: 'derived', value: 'Sensor' }),
  variable('LMin', base('LINT')),
  variable('LMax', base('LINT')),
  variable('UMax', base('ULINT')),
  variable('WMax', base('LWORD')),
  variable('Unsafe', base('LINT')),
  variable('S8', base('SINT')),
  variable('U8', base('USINT')),
  variable('I16', base('INT')),
  variable('U16', base('UINT')),
  variable('D32', base('DINT')),
  variable('U32', base('UDINT')),
  variable('Bt', base('BYTE')),
  variable('Wd', base('WORD')),
  variable('Dw', base('DWORD')),
  variable('Lr', base('LREAL')),
  variable('T1', base('TIME')),
  variable('Tn', base('TIME')),
  variable('Td', base('TOD')),
  variable('D1', base('DATE')),
  variable('Dt1', base('DT')),
  variable('Txt', base('STRING')),
  variable('LongTxt', base('STRING')),
  variable('W', base('WSTRING')),
  variable('WOdd', base('WSTRING')),
]

const REGRESSION_SERVERS = [
  {
    name: 'modbus',
    protocol: 'modbus-tcp',
    modbusSlaveConfig: { enabled: true, networkInterface: '0.0.0.0', port: 5020 },
  },
  {
    name: 'opcua',
    protocol: 'opcua',
    opcuaServerConfig: {
      server: {
        enabled: true,
        name: 'E2E',
        applicationUri: 'urn:openplc:e2e',
        productUri: 'urn:openplc:e2e:product',
        bindAddress: '0.0.0.0',
        port: 4840,
        endpointPath: '/openplc',
      },
      securityProfiles: [
        {
          id: 'sp1',
          name: 'None-None',
          enabled: true,
          securityPolicy: 'None',
          securityMode: 'None',
          authMethods: ['Anonymous'],
        },
      ],
      security: {
        serverCertificateStrategy: 'auto_self_signed',
        serverCertificateCustom: null,
        serverPrivateKeyCustom: null,
        trustedClientCertificates: [],
      },
      users: [],
      cycleTimeMs: 100,
      addressSpace: {
        namespaceUri: 'urn:openplc:e2e:ns',
        nodes: [
          {
            id: 'n1',
            pouName: 'Main',
            variablePath: 'Counter',
            variableType: 'DINT',
            nodeId: 'ns=2;s=Counter',
            browseName: 'Counter',
            displayName: 'Counter',
            description: '',
            permissions: { viewer: 'r', operator: 'r', engineer: 'r' },
            nodeType: 'variable',
          },
        ],
      },
    },
  },
]

const project = (
  pouVariables: ReturnType<typeof variable>[],
  withTypes: boolean,
  mqttConfig: MqttProjectIntegrationV1 | null,
  extraServers: object[] = [],
): PLCProjectData =>
  ({
    pous: [
      {
        type: 'program',
        data: {
          language: 'st',
          name: 'Main',
          variables: pouVariables,
          body: { language: 'st', value: '' },
          documentation: '',
        },
      },
      ...(withTypes
        ? [
            {
              type: 'function-block',
              data: {
                language: 'st',
                name: 'Sensor',
                variables: [
                  variable('Base', base('DINT'), 'input'),
                  variable('Out', { definition: 'user-data-type', value: 'Reading' }, 'output'),
                  variable('Seen', base('DINT'), 'output'),
                ],
                body: { language: 'st', value: '' },
                documentation: '',
              },
            },
          ]
        : []),
    ],
    dataTypes: withTypes
      ? [
          {
            name: 'Reading',
            derivation: 'structure',
            variable: [
              { name: 'Value', type: base('REAL') },
              { name: 'Count', type: base('DINT') },
            ],
          },
        ]
      : [],
    configuration: {
      resource: {
        tasks: [{ name: 'task0', triggering: 'Cyclic', interval: 'T#10ms', priority: 0 }],
        instances: [{ name: 'instance0', task: 'task0', program: 'Main' }],
        globalVariables: [],
      },
    },
    servers: [...extraServers, ...(mqttConfig === null ? [] : [{ name: 'broker', protocol: 'mqtt', mqttConfig }])],
    remoteDevices: [],
  }) as unknown as PLCProjectData

const PROGRAM: MqttSourceToken[] = [
  { role: 'instance', name: 'instance0' },
  { role: 'program', name: 'Main' },
]
const v = (name: string): MqttSourceToken => ({ role: 'variable', name })
const f = (name: string): MqttSourceToken => ({ role: 'field', name })
const idx = (...indices: number[]): MqttSourceToken => ({ role: 'index', indices })
const select = (...rest: MqttSourceToken[]): MqttProjectSelectionV1 => ({ source: { tokens: [...PROGRAM, ...rest] } })

const E2E_SELECTIONS: MqttProjectSelectionV1[] = [
  select(v('Counter')),
  select(v('Toggle')),
  select(v('Flaky')),
  select(v('Grid'), idx(-2, 3)),
  select(v('Grid'), idx(1, 4)),
  select(v('Vec'), idx(-1), f('Count')),
  select(v('Vec'), idx(1), f('Value')),
  select(v('S1'), f('Out'), f('Count')),
  select(v('S2'), f('Out'), f('Count')),
  select(v('S2'), f('Seen')),
  ...['LMin', 'LMax', 'UMax', 'WMax', 'Unsafe', 'S8', 'U8', 'I16', 'U16', 'D32', 'U32', 'Bt', 'Wd', 'Dw', 'Lr'].map(
    (name) => select(v(name)),
  ),
  ...['T1', 'Tn', 'Td', 'D1', 'Dt1', 'Txt', 'LongTxt', 'W', 'WOdd'].map((name) => select(v(name))),
]

const BENCH_SELECTIONS: MqttProjectSelectionV1[] = Array.from({ length: 1000 }, (_, i) => select(v('Arr'), idx(i + 1)))

const integration = (
  mode: 'generic' | 'sparkplug_b',
  selections: MqttProjectSelectionV1[],
  generic: Partial<MqttProjectIntegrationV1['generic']> = {},
): MqttProjectIntegrationV1 => {
  const defaults = createDefaultMqttIntegration('plc-e2e')
  return {
    ...defaults,
    enabled: true,
    mode,
    connection: { host: 'broker', port: 1883, clientId: 'plc-e2e', tls: { enabled: false } },
    generic: { ...defaults.generic, ...generic },
    sparkplug: { groupId: 'openplc', edgeNodeId: 'plc-e2e', sampleIntervalMs: 100 },
    selections,
  }
}

const readHeaders = (): Record<string, string> => {
  const headers: Record<string, string> = {}
  for (const name of readdirSync(HEADERS_DIR)) {
    headers[`strucpp_runtime/include/${name}`] = readFileSync(join(HEADERS_DIR, name), 'utf8')
  }
  return headers
}

const build = async (st: string, data: PLCProjectData): Promise<Record<string, string | Uint8Array>> => {
  const materialized: Record<string, string | Uint8Array>[] = []
  const port = {
    computeMd5: (text: string) => Promise.resolve(createHash('md5').update(text).digest('hex')),
    transpileToSt: () => Promise.resolve({ ok: true, programSt: st }),
    installArduinoCore: () => Promise.resolve({ ok: true }),
    installArduinoLib: () => Promise.resolve({ ok: true }),
    compileArduino: () => Promise.resolve({ ok: false }),
    uploadRuntimeV4: () => Promise.resolve({ ok: false }),
    uploadArduinoBoard: () => Promise.resolve({ ok: false }),
    uploadRuntimeV3: () => Promise.resolve({ ok: false }),
    checkRuntimeVersion: () =>
      Promise.resolve({ ok: true, version: '4.3.0', minEditorVersion: null, supportsProjectSnapshot: false }),
    packageVppPlugin: () => Promise.resolve({ files: {} }),
    materializeRuntimeV4Bundle: (args: { bundle: Record<string, string | Uint8Array> }) => {
      materialized.push(args.bundle)
      return Promise.resolve({ written: Object.keys(args.bundle).length })
    },
  } as unknown as CompilerPlatformPort
  const errors: string[] = []
  const result = await runCompilePipeline(
    {
      projectData: data,
      boardTarget: 'OpenPLC Runtime v4',
      boardRuntime: 'openplc-compiler',
      boardEntry: hals['OpenPLC Runtime v4'] as unknown as RunCompilePipelineArgs['boardEntry'],
      devicePinMapping: [],
      isSimulator: false,
      isRuntimeV4: true,
      isRuntimeV3: false,
      compileOnly: true,
      libraryArchives: [],
      missingLibraries: [],
      firmwareSkeleton: {},
      strucppRuntimeHeaders: readHeaders(),
      avrLibStdCppInclude: '',
      arduinoCliParallel: false,
      deviceContext: { kind: 'editor-https', ip: '127.0.0.1', jwt: 'jwt' },
    },
    port,
    (event) => {
      if (event.level === 'error') errors.push(event.message)
    },
  )
  if (!result.success || materialized.length !== 1) {
    throw new Error(`compile failed: ${errors.join(' | ')}`)
  }
  return materialized[0]
}

const write = (name: string, bundle: Record<string, string | Uint8Array>): void => {
  const dir = join(OUT, name)
  rmSync(dir, { recursive: true, force: true })
  for (const [path, content] of Object.entries(bundle)) {
    const target = join(dir, path)
    mkdirSync(dirname(target), { recursive: true })
    writeFileSync(target, content)
  }
}

const SCENARIOS: Array<[string, string, PLCProjectData]> = [
  ['e2e-generic', E2E_ST, project(E2E_VARIABLES, true, integration('generic', E2E_SELECTIONS))],
  [
    'e2e-periodic',
    E2E_ST,
    project(
      E2E_VARIABLES,
      true,
      integration('generic', E2E_SELECTIONS, { publishMode: 'periodic', qos: 1, retain: true }),
    ),
  ],
  ['e2e-sparkplug', E2E_ST, project(E2E_VARIABLES, true, integration('sparkplug_b', E2E_SELECTIONS))],
  ['e2e-none', E2E_ST, project(E2E_VARIABLES, true, null)],
  ['e2e-regression', E2E_ST, project(E2E_VARIABLES, true, integration('generic', E2E_SELECTIONS), REGRESSION_SERVERS)],
  [
    'bench-none',
    BENCH_ST,
    project(
      [variable('Arr', arrayOf('ARRAY[1..1000] OF DINT', base('DINT'), ['1..1000'])), variable('I', base('DINT'))],
      false,
      null,
    ),
  ],
  [
    'bench-generic',
    BENCH_ST,
    project(
      [variable('Arr', arrayOf('ARRAY[1..1000] OF DINT', base('DINT'), ['1..1000'])), variable('I', base('DINT'))],
      false,
      integration('generic', BENCH_SELECTIONS),
    ),
  ],
  [
    'bench-sparkplug',
    BENCH_ST,
    project(
      [variable('Arr', arrayOf('ARRAY[1..1000] OF DINT', base('DINT'), ['1..1000'])), variable('I', base('DINT'))],
      false,
      integration('sparkplug_b', BENCH_SELECTIONS),
    ),
  ],
]

beforeAll(() => {
  __setStrucppRuntimeForTests(null)
})

describe('MQTT end-to-end payloads', () => {
  it.each(SCENARIOS)(
    'builds %s',
    async (name, st, data) => {
      expect(OUT).not.toBe('')
      const bundle = await build(st, data)
      write(name, bundle)
      expect(typeof bundle['debug-map.json']).toBe('string')
    },
    120_000,
  )
})
