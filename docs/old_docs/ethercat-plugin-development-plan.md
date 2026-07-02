# Plano de Desenvolvimento - Plugin EtherCAT (Runtime)

**Produto:** OpenPLC Runtime v4
**Baseado em:** Levantamento de Requisitos - Protocolo EtherCAT (v1.3)
**Data:** 29 de Janeiro de 2026
**Tipo:** Plugin Nativo C/C++
**Revisao:** 3.0 - Arquitetura Editor-driven (discovery e parametrizacao no Editor)

---

## 1. Visao Geral

Este plano detalha a implementacao do plugin EtherCAT Master para o OpenPLC Runtime v4,
utilizando a biblioteca SOEM (Simple Open EtherCAT Master) conforme definido nos requisitos.

### 1.0 Separacao de Responsabilidades: Editor vs Runtime

A arquitetura do sistema EtherCAT segue o mesmo padrao dos demais plugins do OpenPLC:
o **Editor** e responsavel por toda a configuracao e parametrizacao, e o **Runtime** e
responsavel apenas pela execucao.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         OpenPLC EDITOR                                  │
│                                                                         │
│  - Discovery: scan de rede via Runtime Discovery Service (ja feito)     │
│  - Upload e parsing de arquivos ESI (XML)                               │
│  - Parametrizacao de couplers e modulos                                  │
│  - Configuracao de channels e located vars (%IX, %QX, etc.)             │
│  - Mapeamento de PDOs para variaveis IEC                                │
│  - Configuracao de SDOs (parametros dos slaves)                         │
│  - Geracao do JSON de configuracao final                                │
│                                                                         │
│  Resultado: ethercat_config.json (contrato Editor -> Runtime)           │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │
                                  │ Upload via programa (program.zip)
                                  │ JSON em core/generated/conf/
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         OpenPLC RUNTIME                                  │
│                                                                         │
│  - Recebe JSON de configuracao completo (via plugins.conf)              │
│  - Inicializa SOEM master na interface configurada                      │
│  - Valida topologia: slaves fisicos vs configuracao JSON                │
│  - Aplica configuracoes SDO nos slaves (Pre-Op)                         │
│  - Configura mapeamento de PDOs conforme JSON                           │
│  - Transiciona slaves para estado Operational                           │
│  - Executa ciclo de comunicacao (cycle_start / cycle_end)               │
│  - Monitora diagnosticos e erros                                        │
│                                                                         │
│  NAO faz: discovery, parsing ESI, parametrizacao de UI                  │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1.0.1 Fluxo de Configuracao (igual aos demais plugins)

O fluxo segue exatamente o padrao existente no OpenPLC para todos os plugins:

```
1. Editor parametriza dispositivos EtherCAT (couplers, modulos, channels)
2. Editor gera ethercat_config.json com toda a configuracao
3. Editor envia program.zip contendo o JSON em conf/ethercat_config.json
4. Runtime extrai para core/generated/conf/
5. update_plugin_configurations() copia JSON para diretorio do plugin
6. plugins.conf e atualizado com caminho do config
7. Plugin init() recebe caminho via plugin_runtime_args_t.plugin_specific_config_file_path
8. Plugin carrega JSON, inicializa SOEM, e opera
```

### 1.0.2 Discovery Service (JA IMPLEMENTADO)

O Discovery Service no Runtime ja esta implementado e funcional. Ele fornece endpoints
REST para que o Editor faca scan de rede e deteccao de dispositivos:

**Status: CONCLUIDO**

Arquivos implementados:
- `webserver/discovery/discovery_routes.py` - Endpoints REST
- `webserver/discovery/ethercat_discovery.py` - Logica de discovery
- `scripts/discovery/ethercat_scan.py` - Scanner usando pysoem
- `scripts/setup_discovery_venv.sh` - Setup do venv de discovery
- `tests/pytest/discovery/` - Testes unitarios (40+ testes)

Endpoints disponiveis:
- `GET /api/discovery/interfaces` - Lista interfaces de rede
- `GET /api/discovery/ethercat/status` - Status do servico
- `POST /api/discovery/ethercat/scan` - Scan da rede
- `POST /api/discovery/ethercat/validate` - Valida configuracao
- `POST /api/discovery/ethercat/test` - Testa conexao com slave

### 1.1 Escopo do MVP

Conforme o documento de requisitos, o MVP do **plugin Runtime** deve incluir:

- Carregamento do JSON de configuracao gerado pelo Editor
- Validacao de topologia (slaves fisicos vs configuracao)
- Mapeamento de PDOs para variaveis do PLC conforme JSON
- Aplicacao de configuracoes SDO nos slaves
- Operacao estavel com cycle time de 4 ms
- Suporte a CoE (SDO e PDO)
- Diagnostico basico de status e erros
- Suporte ao perfil DS401 (I/O Devices)

### 1.2 Itens Fora do Escopo (MVP)

- Distributed Clocks (RF04)
- Multiplos Masters (RF05)
- Hot-connect de dispositivos (RF08)
- Redundancia de cabo (RF10)
- EoE - Ethernet over EtherCAT (RF12)
- FoE - File over EtherCAT (RF13)
- Perfil DS402 - Motion Control (RF14)
- Controle de servo drives

### 1.3 Itens que NAO pertencem ao Runtime

Os seguintes itens sao responsabilidade exclusiva do **Editor**:

- Scan de rede / discovery de dispositivos (Editor usa Discovery Service do Runtime)
- Upload e parsing de arquivos ESI (XML)
- Interface de parametrizacao de couplers e modulos
- Interface de configuracao de channels e located vars
- Mapeamento visual de PDOs
- Geracao do JSON de configuracao

---

## 2. Arquitetura do Plugin

### 2.1 Estrutura de Arquivos

```
core/src/drivers/plugins/native/ethercat/
├── CMakeLists.txt                 # Build configuration
├── ethercat_plugin.c              # Main plugin entry (init, start, stop, cycle hooks)
├── ethercat_plugin.h              # Plugin interface definitions
├── ethercat_config.h              # Configuration structures
├── ethercat_config.c              # JSON config parser (cJSON)
├── ethercat_config.json           # Default/empty configuration file
├── ethercat_master.c              # SOEM wrapper - master operations
├── ethercat_master.h
├── ethercat_pdo_mapper.c          # PDO to IEC variable mapping (from JSON config)
├── ethercat_pdo_mapper.h
├── ethercat_diagnostics.c         # Status and error reporting
├── ethercat_diagnostics.h
├── ethercat_state_machine.c       # EtherCAT state transitions
├── ethercat_state_machine.h
└── libs/
    └── soem/                      # SOEM library (submodule or vendored)
```

**Nota:** Nao ha `ethercat_esi_parser.c` nem `ethercat_slave_manager.c`. O parsing
de ESI e feito no Editor, e o gerenciamento de slaves e baseado no JSON de configuracao.

### 2.2 Integracao com Plugin System

O plugin seguira o padrao nativo existente (similar ao S7Comm):

```c
// Funcoes obrigatorias
int init(void *args);              // Inicializacao com runtime_args
void start_loop(void);             // Inicia thread EtherCAT
void stop_loop(void);              // Para thread EtherCAT
void cleanup(void);                // Liberacao de recursos

// Funcoes de ciclo (chamadas com mutex held)
void cycle_start(void);            // Leitura de inputs dos slaves
void cycle_end(void);              // Escrita de outputs para slaves
```

### 2.3 Configuracao (plugins.conf)

```
ethercat,./build/plugins/libethercat_plugin.so,1,1,./core/src/drivers/plugins/native/ethercat/ethercat_config.json,
```

---

## 3. Contrato JSON: Editor -> Runtime

O JSON de configuracao e o **contrato** entre Editor e Runtime. O Editor gera este JSON
com **todos** os parametros necessarios para o funcionamento do plugin. O Runtime nao
faz discovery nem parsing ESI - tudo ja vem resolvido no JSON.

### 3.1 Estrutura Completa do JSON

A lista `slaves` e **flat** (plana), exatamente como o array `ec_slave[]` da SOEM.
Cada slave ocupa uma posicao no barramento, independente de ser coupler ou modulo.
O campo `position` corresponde diretamente ao indice `ec_slave[position]` da SOEM.

```json
[
  {
    "name": "ethercat_master",
    "protocol": "ETHERCAT",
    "config": {
      "master": {
        "interface": "eth0",
        "cycle_time_us": 1000,
        "watchdog_timeout_cycles": 3,
        "log_level": "info"
      },
      "slaves": [
        {
          "position": 1,
          "name": "EK1100",
          "type": "coupler",
          "vendor_id": "0x00000002",
          "product_code": "0x044c2c52",
          "revision": "0x00120000",
          "channels": [],
          "sdo_configurations": [],
          "rx_pdos": [],
          "tx_pdos": []
        },
        {
          "position": 2,
          "name": "EL1008",
          "type": "digital_input",
          "vendor_id": "0x00000002",
          "product_code": "0x03f03052",
          "revision": "0x00120000",
          "channels": [
            {
              "index": 0,
              "name": "Input 1",
              "type": "digital_input",
              "bit_length": 1,
              "iec_location": "%IX0.0",
              "pdo_index": "0x1A00",
              "pdo_entry_index": "0x6000",
              "pdo_entry_subindex": 1
            },
            {
              "index": 1,
              "name": "Input 2",
              "type": "digital_input",
              "bit_length": 1,
              "iec_location": "%IX0.1",
              "pdo_index": "0x1A00",
              "pdo_entry_index": "0x6000",
              "pdo_entry_subindex": 2
            },
            {
              "index": 2,
              "name": "Input 3",
              "type": "digital_input",
              "bit_length": 1,
              "iec_location": "%IX0.2",
              "pdo_index": "0x1A00",
              "pdo_entry_index": "0x6000",
              "pdo_entry_subindex": 3
            },
            {
              "index": 3,
              "name": "Input 4",
              "type": "digital_input",
              "bit_length": 1,
              "iec_location": "%IX0.3",
              "pdo_index": "0x1A00",
              "pdo_entry_index": "0x6000",
              "pdo_entry_subindex": 4
            },
            {
              "index": 4,
              "name": "Input 5",
              "type": "digital_input",
              "bit_length": 1,
              "iec_location": "%IX0.4",
              "pdo_index": "0x1A00",
              "pdo_entry_index": "0x6000",
              "pdo_entry_subindex": 5
            },
            {
              "index": 5,
              "name": "Input 6",
              "type": "digital_input",
              "bit_length": 1,
              "iec_location": "%IX0.5",
              "pdo_index": "0x1A00",
              "pdo_entry_index": "0x6000",
              "pdo_entry_subindex": 6
            },
            {
              "index": 6,
              "name": "Input 7",
              "type": "digital_input",
              "bit_length": 1,
              "iec_location": "%IX0.6",
              "pdo_index": "0x1A00",
              "pdo_entry_index": "0x6000",
              "pdo_entry_subindex": 7
            },
            {
              "index": 7,
              "name": "Input 8",
              "type": "digital_input",
              "bit_length": 1,
              "iec_location": "%IX1.0",
              "pdo_index": "0x1A00",
              "pdo_entry_index": "0x6000",
              "pdo_entry_subindex": 8
            }
          ],
          "sdo_configurations": [],
          "rx_pdos": [],
          "tx_pdos": [
            {
              "index": "0x1A00",
              "name": "TxPDO-Map Inputs",
              "entries": [
                {
                  "index": "0x6000",
                  "subindex": 1,
                  "bit_length": 1,
                  "name": "Input 1",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x6000",
                  "subindex": 2,
                  "bit_length": 1,
                  "name": "Input 2",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x6000",
                  "subindex": 3,
                  "bit_length": 1,
                  "name": "Input 3",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x6000",
                  "subindex": 4,
                  "bit_length": 1,
                  "name": "Input 4",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x6000",
                  "subindex": 5,
                  "bit_length": 1,
                  "name": "Input 5",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x6000",
                  "subindex": 6,
                  "bit_length": 1,
                  "name": "Input 6",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x6000",
                  "subindex": 7,
                  "bit_length": 1,
                  "name": "Input 7",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x6000",
                  "subindex": 8,
                  "bit_length": 1,
                  "name": "Input 8",
                  "data_type": "BOOL"
                }
              ]
            }
          ]
        },
        {
          "position": 3,
          "name": "EL2008",
          "type": "digital_output",
          "vendor_id": "0x00000002",
          "product_code": "0x07d83052",
          "revision": "0x00120000",
          "channels": [
            {
              "index": 0,
              "name": "Output 1",
              "type": "digital_output",
              "bit_length": 1,
              "iec_location": "%QX0.0",
              "pdo_index": "0x1600",
              "pdo_entry_index": "0x7000",
              "pdo_entry_subindex": 1
            },
            {
              "index": 1,
              "name": "Output 2",
              "type": "digital_output",
              "bit_length": 1,
              "iec_location": "%QX0.1",
              "pdo_index": "0x1600",
              "pdo_entry_index": "0x7000",
              "pdo_entry_subindex": 2
            }
          ],
          "sdo_configurations": [],
          "rx_pdos": [
            {
              "index": "0x1600",
              "name": "RxPDO-Map Outputs",
              "entries": [
                {
                  "index": "0x7000",
                  "subindex": 1,
                  "bit_length": 1,
                  "name": "Output 1",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x7000",
                  "subindex": 2,
                  "bit_length": 1,
                  "name": "Output 2",
                  "data_type": "BOOL"
                }
              ]
            }
          ],
          "tx_pdos": []
        },
        {
          "position": 4,
          "name": "EL3062",
          "type": "analog_input",
          "vendor_id": "0x00000002",
          "product_code": "0x0bf63052",
          "revision": "0x00120000",
          "channels": [
            {
              "index": 0,
              "name": "Analog Input 1",
              "type": "analog_input",
              "bit_length": 16,
              "iec_location": "%IW0",
              "pdo_index": "0x1A00",
              "pdo_entry_index": "0x6000",
              "pdo_entry_subindex": 17
            },
            {
              "index": 1,
              "name": "Analog Input 2",
              "type": "analog_input",
              "bit_length": 16,
              "iec_location": "%IW1",
              "pdo_index": "0x1A01",
              "pdo_entry_index": "0x6010",
              "pdo_entry_subindex": 17
            }
          ],
          "sdo_configurations": [
            {
              "index": "0x8000",
              "subindex": 6,
              "value": 0,
              "data_type": "UINT16",
              "name": "Filter setting Ch.1",
              "description": "Filter constant for channel 1 (0=50Hz, 1=60Hz)"
            },
            {
              "index": "0x8000",
              "subindex": 21,
              "value": true,
              "data_type": "BOOL",
              "name": "Enable user scale Ch.1",
              "description": "Enable user-defined scaling for channel 1"
            },
            {
              "index": "0x8000",
              "subindex": 17,
              "value": 0,
              "data_type": "INT16",
              "name": "User scale offset Ch.1",
              "description": "User-defined offset for channel 1"
            },
            {
              "index": "0x8000",
              "subindex": 18,
              "value": 32767,
              "data_type": "INT32",
              "name": "User scale gain Ch.1",
              "description": "User-defined gain for channel 1"
            }
          ],
          "rx_pdos": [],
          "tx_pdos": [
            {
              "index": "0x1A00",
              "name": "TxPDO-Map Ch.1",
              "entries": [
                {
                  "index": "0x6000",
                  "subindex": 1,
                  "bit_length": 1,
                  "name": "Underrange",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x6000",
                  "subindex": 2,
                  "bit_length": 1,
                  "name": "Overrange",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x0000",
                  "subindex": 0,
                  "bit_length": 4,
                  "name": "padding",
                  "data_type": "PAD"
                },
                {
                  "index": "0x6000",
                  "subindex": 7,
                  "bit_length": 1,
                  "name": "Error",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x0000",
                  "subindex": 0,
                  "bit_length": 7,
                  "name": "padding",
                  "data_type": "PAD"
                },
                {
                  "index": "0x1800",
                  "subindex": 7,
                  "bit_length": 1,
                  "name": "TxPDO State",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x1800",
                  "subindex": 9,
                  "bit_length": 1,
                  "name": "TxPDO Toggle",
                  "data_type": "BOOL"
                },
                {
                  "index": "0x6000",
                  "subindex": 17,
                  "bit_length": 16,
                  "name": "Value",
                  "data_type": "INT16"
                }
              ]
            },
            {
              "index": "0x1A01",
              "name": "TxPDO-Map Ch.2",
              "entries": [
                {
                  "index": "0x6010",
                  "subindex": 17,
                  "bit_length": 16,
                  "name": "Value",
                  "data_type": "INT16"
                }
              ]
            }
          ]
        }
      ],
      "diagnostics": {
        "log_connections": true,
        "log_data_access": false,
        "log_errors": true,
        "max_log_entries": 10000,
        "status_update_interval_ms": 500
      }
    }
  }
]
```

### 3.2 Descricao dos Campos

#### 3.2.1 Nivel Raiz (padrao OpenPLC plugin config)

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|-------------|-----------|
| `name` | string | Sim | Identificador da instancia (ex: "ethercat_master") |
| `protocol` | string | Sim | Sempre "ETHERCAT" |
| `config` | object | Sim | Configuracao completa do plugin |

#### 3.2.2 config.master (parametros obrigatorios)

| Campo | Tipo | Obrigatorio | Default | Descricao |
|-------|------|-------------|---------|-----------|
| `interface` | string | Sim | - | Interface de rede (ex: "eth0", "enp2s0") |
| `cycle_time_us` | int | Nao | 1000 | Tempo de ciclo em microssegundos (min: 100) |
| `watchdog_timeout_cycles` | int | Nao | 3 | Ciclos sem resposta para acionar watchdog |
| `log_level` | string | Nao | "info" | Nivel de log: "debug", "info", "warn", "error" |

#### 3.2.3 config.slaves[] (lista flat de slaves)

Lista plana de todos os slaves no barramento EtherCAT, ordenada por `position`.
Corresponde diretamente ao array `ec_slave[]` da SOEM. Couplers e modulos estao
no mesmo nivel - a distincao e apenas pelo campo `type`.

**Slave:**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|-------------|-----------|
| `position` | int | Sim | Posicao no barramento EtherCAT (1-based), corresponde a `ec_slave[position]` |
| `name` | string | Sim | Nome do dispositivo (ex: "EK1100", "EL1008") |
| `type` | string | Sim | Tipo: "coupler", "digital_input", "digital_output", "analog_input", "analog_output" |
| `vendor_id` | string | Sim | Vendor ID hexadecimal (ex: "0x00000002") |
| `product_code` | string | Sim | Product Code hexadecimal |
| `revision` | string | Nao | Revision Number hexadecimal |
| `channels` | array | Sim | Lista de channels (pontos de I/O). Vazia para couplers |
| `sdo_configurations` | array | Nao | Configuracoes SDO para aplicar no startup |
| `rx_pdos` | array | Sim | RxPDOs (dados enviados para o slave - outputs) |
| `tx_pdos` | array | Sim | TxPDOs (dados recebidos do slave - inputs) |

#### 3.2.4 channels[] (pontos de I/O mapeados)

Cada channel representa um ponto de I/O individual que foi mapeado para uma
located var no programa IEC do PLC.

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|-------------|-----------|
| `index` | int | Sim | Indice do channel no slave (0-based) |
| `name` | string | Sim | Nome descritivo (ex: "Input 1") |
| `type` | string | Sim | Tipo de I/O: "digital_input", "digital_output", "analog_input", "analog_output" |
| `bit_length` | int | Sim | Tamanho em bits: 1 (BOOL), 8 (BYTE), 16 (INT/UINT), 32 (DINT/UDINT) |
| `iec_location` | string | Sim | Located var IEC 61131-3 (ex: "%IX0.0", "%QW2", "%IW0") |
| `pdo_index` | string | Sim | Indice do PDO que contem este channel (hex) |
| `pdo_entry_index` | string | Sim | Indice do entry no PDO (hex) |
| `pdo_entry_subindex` | int | Sim | Sub-indice do entry no PDO |

**Formato de iec_location:**
- `%IX<byte>.<bit>` - Input digital (BOOL)
- `%QX<byte>.<bit>` - Output digital (BOOL)
- `%IB<index>` - Input byte (BYTE)
- `%QB<index>` - Output byte (BYTE)
- `%IW<index>` - Input word (INT/UINT, 16 bits)
- `%QW<index>` - Output word (INT/UINT, 16 bits)
- `%ID<index>` - Input double word (DINT/UDINT, 32 bits)
- `%QD<index>` - Output double word (DINT/UDINT, 32 bits)
- `%IL<index>` - Input long word (LINT/ULINT, 64 bits)
- `%QL<index>` - Output long word (LINT/ULINT, 64 bits)

#### 3.2.5 sdo_configurations[] (parametros de startup)

Configuracoes SDO sao aplicadas durante a fase Pre-Operational, antes dos slaves
entrarem em modo Operational.

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|-------------|-----------|
| `index` | string | Sim | Indice do objeto SDO (hex, ex: "0x8000") |
| `subindex` | int | Sim | Sub-indice do objeto SDO |
| `value` | varies | Sim | Valor a ser escrito (tipo depende de data_type) |
| `data_type` | string | Sim | Tipo de dado: "BOOL", "INT8", "UINT8", "INT16", "UINT16", "INT32", "UINT32" |
| `name` | string | Nao | Nome descritivo do parametro |
| `description` | string | Nao | Descricao do parametro |

#### 3.2.6 rx_pdos[] e tx_pdos[] (mapeamento completo de PDOs)

Descrevem o layout completo dos PDOs conforme definido no ESI. O Runtime usa esta
informacao para calcular offsets de bytes/bits no process data image.

**Convencao de nomenclatura:**
- **RxPDO** = dados recebidos pelo slave = **outputs** do PLC
- **TxPDO** = dados transmitidos pelo slave = **inputs** do PLC

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|-------------|-----------|
| `index` | string | Sim | Indice do PDO (hex, ex: "0x1600", "0x1A00") |
| `name` | string | Nao | Nome descritivo do PDO |
| `entries` | array | Sim | Lista de entries dentro do PDO |

**PDO Entry:**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|-------------|-----------|
| `index` | string | Sim | Indice do objeto (hex). "0x0000" para padding |
| `subindex` | int | Sim | Sub-indice. 0 para padding |
| `bit_length` | int | Sim | Tamanho em bits |
| `name` | string | Nao | Nome descritivo |
| `data_type` | string | Sim | Tipo: "BOOL", "INT8", "UINT8", "INT16", "UINT16", "INT32", "UINT32", "PAD" |

**Nota sobre padding:** Entries com `index: "0x0000"` e `data_type: "PAD"` sao
espacos de preenchimento no PDO. O Runtime deve considerar esses bits no calculo
de offset, mas nao mapeia para nenhuma variavel.

#### 3.2.7 config.diagnostics (parametros opcionais)

| Campo | Tipo | Obrigatorio | Default | Descricao |
|-------|------|-------------|---------|-----------|
| `log_connections` | bool | Nao | true | Logar conexoes/desconexoes de slaves |
| `log_data_access` | bool | Nao | false | Logar acessos a dados (verbose) |
| `log_errors` | bool | Nao | true | Logar erros de comunicacao |
| `max_log_entries` | int | Nao | 10000 | Maximo de entradas no buffer de log |
| `status_update_interval_ms` | int | Nao | 500 | Intervalo de atualizacao de status |

### 3.3 Validacao no Runtime

O Runtime deve validar o JSON recebido em dois niveis:

**1. Validacao estrutural (no init()):**
- Campos obrigatorios presentes
- Tipos de dados corretos
- Valores dentro de faixas validas (cycle_time >= 1, positions > 0, etc.)
- Located vars com formato IEC valido
- Indices PDO/SDO em formato hexadecimal valido

**2. Validacao de topologia (no start_loop()):**
- Numero de slaves fisicos na rede == tamanho do array `slaves` no JSON
- Para cada slave: `ec_slave[position].man` == vendor_id e `ec_slave[position].id` == product_code do JSON
- Se houver divergencia, logar erro detalhado e abortar

---

## 4. Etapas de Desenvolvimento

### Fase 1: Foundation (3-4 semanas)

#### Etapa 1.1: Setup do Projeto e Integracao SOEM
**Duracao estimada:** 1 semana

**Tarefas:**
1. Criar estrutura de diretorios do plugin
2. Configurar CMakeLists.txt para compilar com SOEM
3. Integrar SOEM como submodulo git ou biblioteca vendorizada
4. Criar Makefile/script de build
5. Testar compilacao em x86_64, ARM64 e ARMv7

**Arquivos:**
- `CMakeLists.txt`
- `ethercat_plugin.h`
- `ethercat_plugin.c` (esqueleto inicial)

**Criterio de Aceite:**
- Plugin compila em todas as arquiteturas alvo
- SOEM linkado corretamente

#### Etapa 1.2: Estrutura Basica do Plugin e Config Parser
**Duracao estimada:** 1-2 semanas

**Tarefas:**
1. Implementar funcoes de lifecycle (`init`, `start_loop`, `stop_loop`, `cleanup`)
2. Implementar copia segura de `plugin_runtime_args_t`
3. Integrar sistema de logging (`plugin_logger.h`)
4. Implementar parser do JSON de configuracao usando cJSON
5. Implementar validacao estrutural do JSON
6. Mapear JSON para estruturas C internas

**Arquivos:**
- `ethercat_plugin.c`
- `ethercat_config.h`
- `ethercat_config.c`

**Estruturas C para configuracao:**
```c
// Tamanhos maximos
#define ECAT_MAX_SLAVES      64
#define ECAT_MAX_MODULES     32
#define ECAT_MAX_CHANNELS    64
#define ECAT_MAX_PDO_ENTRIES 32
#define ECAT_MAX_PDOS        16
#define ECAT_MAX_SDOS        32
#define ECAT_MAX_NAME_LEN    64
#define ECAT_MAX_IEC_LOC_LEN 16

typedef struct {
    char     index[12];        // hex string "0x6000"
    uint8_t  subindex;
    uint8_t  bit_length;
    char     name[ECAT_MAX_NAME_LEN];
    char     data_type[12];    // "BOOL", "INT16", "PAD", etc.
} ecat_pdo_entry_t;

typedef struct {
    char             index[12];  // hex string "0x1A00"
    char             name[ECAT_MAX_NAME_LEN];
    ecat_pdo_entry_t entries[ECAT_MAX_PDO_ENTRIES];
    int              entry_count;
} ecat_pdo_t;

typedef struct {
    char     index[12];        // hex string "0x8000"
    uint8_t  subindex;
    int32_t  value;
    char     data_type[12];
    char     name[ECAT_MAX_NAME_LEN];
} ecat_sdo_config_t;

typedef struct {
    int      index;
    char     name[ECAT_MAX_NAME_LEN];
    char     type[20];         // "digital_input", "analog_output", etc.
    uint8_t  bit_length;
    char     iec_location[ECAT_MAX_IEC_LOC_LEN];
    char     pdo_index[12];
    char     pdo_entry_index[12];
    uint8_t  pdo_entry_subindex;
} ecat_channel_t;

typedef struct {
    int               position;        // ec_slave[position] na SOEM (1-based)
    char              name[ECAT_MAX_NAME_LEN];
    char              type[20];        // "coupler", "digital_input", etc.
    uint32_t          vendor_id;
    uint32_t          product_code;
    uint32_t          revision;
    ecat_channel_t    channels[ECAT_MAX_CHANNELS];
    int               channel_count;
    ecat_sdo_config_t sdo_configs[ECAT_MAX_SDOS];
    int               sdo_count;
    ecat_pdo_t        rx_pdos[ECAT_MAX_PDOS];
    int               rx_pdo_count;
    ecat_pdo_t        tx_pdos[ECAT_MAX_PDOS];
    int               tx_pdo_count;
} ecat_slave_t;

typedef struct {
    char             interface[32];
    int              cycle_time_us;
    int              watchdog_timeout_cycles;
    char             log_level[8];
} ecat_master_config_t;

typedef struct {
    bool             log_connections;
    bool             log_data_access;
    bool             log_errors;
    int              max_log_entries;
    int              status_update_interval_ms;
} ecat_diagnostics_config_t;

typedef struct {
    ecat_master_config_t      master;
    ecat_slave_t              slaves[ECAT_MAX_SLAVES];  // flat list
    int                       slave_count;
    ecat_diagnostics_config_t diagnostics;
} ecat_config_t;

// API
int  ecat_config_parse(const char *config_path, ecat_config_t *config);
int  ecat_config_validate(const ecat_config_t *config);
void ecat_config_init_defaults(ecat_config_t *config);
void ecat_config_free(ecat_config_t *config);
```

**Criterio de Aceite:**
- Plugin carrega e descarrega sem erros
- JSON parseado corretamente para estruturas C
- Validacao rejeita JSON invalido com mensagens claras
- Logs aparecem no sistema centralizado

#### Etapa 1.3: Inicializacao do Master EtherCAT
**Duracao estimada:** 1 semana

**Tarefas:**
1. Implementar wrapper para `ec_init()` do SOEM
2. Configurar interface de rede (raw sockets)
3. Implementar scan e validacao de topologia contra JSON
4. Verificar permissoes necessarias (CAP_NET_RAW)
5. Implementar tratamento de erros de inicializacao

**Arquivos:**
- `ethercat_master.c`
- `ethercat_master.h`

**Fluxo de inicializacao:**
```
init():
  1. Carregar JSON config
  2. Validar estrutura do JSON

start_loop():
  3. ec_init(interface)
  4. ec_config_init() - scan da rede
  5. Validar topologia: slaves fisicos == JSON config
     - Comparar vendor_id, product_code de cada slave
     - Se divergir -> logar erro e abortar
  6. Continuar com configuracao dos slaves
```

**Requisitos Atendidos:**
- RNF06: Compatibilidade com qualquer NIC com raw sockets
- RNF07: Linux kernel 4.x ou superior

**Criterio de Aceite:**
- Master inicializa corretamente em interface de rede
- Topologia validada contra configuracao JSON
- Erros de permissao e topologia reportados adequadamente

---

### Fase 2: Core Features (4-6 semanas)

#### Etapa 2.1: Maquina de Estados EtherCAT
**Duracao estimada:** 1-2 semanas

**Tarefas:**
1. Implementar transicoes: Init -> Pre-Op -> Safe-Op -> Op
2. Implementar transicoes de erro e recuperacao
3. Gerenciar estado individual de cada slave
4. Implementar timeout de transicao

**Arquivos:**
- `ethercat_state_machine.c`
- `ethercat_state_machine.h`

**Requisitos Atendidos:**
- RF07: Transicionar dispositivos pelos estados EtherCAT
- Criterio: Transicao completa em menos de 2 segundos

**Criterio de Aceite:**
- Todos os slaves transicionam para estado OP
- Erros de transicao detectados e reportados

#### Etapa 2.2: Aplicacao de SDOs (Configuracao Pre-Operational)
**Duracao estimada:** 1 semana

**Tarefas:**
1. Implementar leitura de SDOs (`ec_SDOread`)
2. Implementar escrita de SDOs (`ec_SDOwrite`)
3. Iterar sobre `sdo_configurations` de cada slave no JSON
4. Aplicar SDOs na fase Pre-Op antes de transicionar para Safe-Op
5. Tratar erros de SDO (device nao suporta, valor invalido, etc.)

**Arquivos:**
- Extensao de `ethercat_master.c`

**Fluxo:**
```
Para cada slave no JSON:
  Se sdo_configurations nao vazio:
    Para cada SDO:
      ec_SDOwrite(slave.position, index, subindex, value, sizeof(value))
      Se erro -> logar e decidir (abortar ou continuar)
```

**Requisitos Atendidos:**
- RF11: Implementar CoE (CANopen over EtherCAT)
- Criterio: Acesso completo a SDOs

**Criterio de Aceite:**
- Parametros de slaves configuraveis via SDO conforme JSON
- Erros de SDO tratados adequadamente

#### Etapa 2.3: Mapeamento de PDOs
**Duracao estimada:** 2 semanas

**Tarefas:**
1. Calcular offsets de bytes/bits no process data image a partir dos PDOs no JSON
2. Considerar padding entries no calculo de offset
3. Implementar estrutura de mapeamento PDO <-> buffer IEC
4. Parse de iec_location (%IX, %QX, %IW, etc.) para tipo/indice de buffer
5. Construir tabela de mapeamento rapido para uso no ciclo

**Arquivos:**
- `ethercat_pdo_mapper.c`
- `ethercat_pdo_mapper.h`

**Estrutura de mapeamento interno:**
```c
typedef enum {
    ECAT_DIR_INPUT,   // Slave -> PLC (TxPDO)
    ECAT_DIR_OUTPUT,  // PLC -> Slave (RxPDO)
} ecat_direction_t;

typedef enum {
    ECAT_IEC_BOOL,    // 1 bit  -> bool_input/bool_output
    ECAT_IEC_BYTE,    // 8 bit  -> byte_input/byte_output
    ECAT_IEC_INT,     // 16 bit -> int_input/int_output
    ECAT_IEC_DINT,    // 32 bit -> dint_input/dint_output
    ECAT_IEC_LINT,    // 64 bit -> lint_input/lint_output
} ecat_iec_type_t;

typedef struct {
    // Localizacao no process data image do SOEM
    int              slave_index;      // Indice do slave no SOEM (0-based)
    int              pdi_byte_offset;  // Offset em bytes no process data do slave
    int              pdi_bit_offset;   // Offset em bits (0-7, para BOOL)
    int              bit_length;       // Tamanho em bits

    // Localizacao no buffer do PLC
    ecat_direction_t direction;
    ecat_iec_type_t  iec_type;
    int              buffer_index;     // Indice no array do buffer
    int              bit_index;        // Bit dentro do buffer (para BOOL)
} ecat_pdo_map_entry_t;

typedef struct {
    ecat_pdo_map_entry_t *entries;
    int                   input_count;
    int                   output_count;
    int                   total_count;
} ecat_pdo_map_t;

// API
int  ecat_pdo_map_build(const ecat_config_t *config, ecat_pdo_map_t *map);
void ecat_pdo_map_free(ecat_pdo_map_t *map);
```

**Algoritmo de calculo de offset:**
```
Para cada slave na lista:
  slave_index = slave.position - 1  (0-based para SOEM)
  Para cada TxPDO do slave:
    bit_offset = 0
    Para cada entry do PDO:
      Se entry.data_type != "PAD" E channel mapeado para esta entry:
        Criar map_entry com:
          - pdi_byte_offset = bit_offset / 8
          - pdi_bit_offset = bit_offset % 8
          - parse iec_location do channel -> direction, iec_type, buffer_index, bit_index
      bit_offset += entry.bit_length
  (mesmo para RxPDOs)
```

**Requisitos Atendidos:**
- RF03: Permitir mapeamento visual de PDOs (suporte no runtime)
- N03: Mapear variaveis do PLC para PDOs

**Criterio de Aceite:**
- Offsets calculados corretamente a partir do JSON
- Padding considerado no calculo
- Located vars parseadas para tipos/indices de buffer corretos
- Dados fluem entre slaves e image tables

#### Etapa 2.4: Ciclo de Comunicacao
**Duracao estimada:** 1-2 semanas

**Tarefas:**
1. Implementar troca ciclica de PDOs (`ec_send_processdata`, `ec_receive_processdata`)
2. Implementar cycle_start: ler process data -> copiar para buffers PLC (inputs)
3. Implementar cycle_end: copiar buffers PLC -> escrever process data (outputs)
4. Usar journal writes para operacoes atomicas nos buffers
5. Verificar working counter a cada ciclo

**Arquivos:**
- Extensao de `ethercat_plugin.c`
- Extensao de `ethercat_master.c`

**Fluxo do ciclo:**
```
cycle_start():  (chamado com mutex held)
  1. ec_receive_processdata(timeout)  // Recebe dados dos slaves
  2. Verificar working counter
  3. Para cada input mapping:
     Ler bytes/bits do process data image do slave
     Escrever no buffer PLC via journal_write_*()

cycle_end():  (chamado com mutex held)
  1. Para cada output mapping:
     Ler bytes/bits do buffer PLC
     Escrever no process data image do slave
  2. ec_send_processdata()  // Envia dados para os slaves
```

**Requisitos Atendidos:**
- RF06: Executar ciclo EtherCAT sincronizado com task
- RNF01: Cycle time minimo de 1 ms
- RNF02: Cycle time recomendado de 4 ms
- RNF03: Jitter maximo de 500 us

**Criterio de Aceite:**
- Comunicacao ciclica estavel
- Jitter dentro do especificado
- Operacao continua sem erros por 1 hora

---

### Fase 3: Diagnostico e Robustez (2-3 semanas)

#### Etapa 3.1: Sistema de Diagnostico
**Duracao estimada:** 1-2 semanas

**Tarefas:**
1. Implementar coleta de status por slave (estado, erros)
2. Implementar contadores de erro (CRC, frame, lost link)
3. Registrar eventos em log com timestamp
4. Expor metricas via estrutura interna

**Arquivos:**
- `ethercat_diagnostics.c`
- `ethercat_diagnostics.h`

**Requisitos Atendidos:**
- RF16: Exibir status de cada dispositivo em tempo real
- RF17: Registrar log de erros com timestamp (10.000 eventos)
- RF18: Fornecer contadores de erros por dispositivo
- RF19: Permitir leitura de registradores ESC

**Estrutura de Diagnostico:**
```c
typedef struct {
    int slave_index;
    uint16_t al_status;          // EtherCAT state
    uint16_t al_status_code;     // Error code
    uint32_t crc_errors;
    uint32_t frame_errors;
    uint32_t lost_links;
    uint64_t last_error_timestamp;
} ecat_slave_diagnostics_t;

typedef struct {
    uint32_t cycle_count;
    uint32_t cycle_time_us;
    uint32_t max_jitter_us;
    uint32_t working_counter_errors;
} ecat_master_diagnostics_t;
```

**Criterio de Aceite:**
- Status de slaves disponivel em tempo real
- Erros registrados com timestamp
- Contadores incrementados corretamente

#### Etapa 3.2: Watchdog de Comunicacao
**Duracao estimada:** 1 semana

**Tarefas:**
1. Implementar deteccao de perda de comunicacao
2. Implementar acao de watchdog (transicao para Safe-Op)
3. Implementar recuperacao automatica
4. Configurar timeout via `watchdog_timeout_cycles` do JSON

**Arquivos:**
- Extensao de `ethercat_master.c`

**Requisitos Atendidos:**
- RF09: Implementar watchdog de comunicacao
- Criterio: Detectar perda em maximo 3 ciclos

**Criterio de Aceite:**
- Perda de comunicacao detectada rapidamente
- Sistema transiciona para estado seguro
- Recuperacao automatica quando comunicacao retorna

---

### Fase 4: Perfil DS401 e Finalizacao (2-3 semanas)

#### Etapa 4.1: Suporte ao Perfil DS401 (I/O Devices)
**Duracao estimada:** 1-2 semanas

**Tarefas:**
1. Validar mapeamento padrao DS401 para I/O digital (funciona via PDO mapping do JSON)
2. Validar mapeamento padrao DS401 para I/O analogico
3. Testar com modulos I/O de diferentes fabricantes
4. Tratar particularidades de cada tipo de modulo (scaling, ranges, etc.)

**Requisitos Atendidos:**
- RF15: Suportar perfil DS401 (I/O Devices)

**Criterio de Aceite:**
- Modulos I/O digitais funcionais
- Modulos I/O analogicos funcionais
- Compatibilidade com pelo menos 2 fabricantes

#### Etapa 4.2: API REST para Status
**Duracao estimada:** 1 semana

**Tarefas:**
1. Expor status EtherCAT via REST API do webserver
2. Implementar endpoints para:
   - Lista de slaves
   - Status individual de slave
   - Metricas do master
   - Logs de eventos

**Endpoints:**
```
GET /api/ethercat/slaves          # Lista slaves detectados
GET /api/ethercat/slaves/{id}     # Status de slave especifico
GET /api/ethercat/master/status   # Metricas do master
GET /api/ethercat/diagnostics     # Diagnostico completo
```

**Criterio de Aceite:**
- Endpoints funcionais
- Dados atualizados em tempo real

#### Etapa 4.3: Testes e Documentacao
**Duracao estimada:** 1 semana

**Tarefas:**
1. Criar testes unitarios (cobertura minima 80%)
2. Criar testes de integracao
3. Testar em hardware real (Beckhoff, Omron)
4. Escrever documentacao tecnica
5. Criar 3 projetos de exemplo

**Requisitos Atendidos:**
- RNF14: Cobertura minima de 80% em testes unitarios
- RNF12: Minimo 3 projetos de exemplo

**Criterio de Aceite:**
- Testes passando
- Documentacao completa
- Exemplos funcionais

---

## 5. Dependencias e Requisitos de Sistema

### 5.1 Dependencias de Build
- CMake 3.10+
- GCC/Clang com suporte a C99
- SOEM library (Git submodule)
- cJSON (ja utilizado no projeto para parsing JSON)
- pthread

**Nota:** libxml2 **nao e necessaria** no Runtime. O parsing de ESI (XML) e feito
inteiramente no Editor.

### 5.2 Dependencias de Runtime
- Linux kernel 4.x+ com suporte a raw sockets
- Permissao CAP_NET_RAW ou execucao como root
- Interface de rede Ethernet

### 5.3 Plataformas Alvo
- x86_64 (principal)
- ARM64 (Raspberry Pi 4, Orange Pi)
- ARMv7 (Raspberry Pi 3, BeagleBone)

---

## 6. Criterios de Qualidade

### 6.1 Performance
- Cycle time minimo: 1 ms
- Cycle time recomendado: 4 ms
- Jitter maximo: 500 us
- Operacao continua: 72 horas sem erros criticos

### 6.2 Codigo
- Seguir padrao do projeto: snake_case, 4-space indent
- Documentacao inline para funcoes publicas
- Sem warnings em compilacao

### 6.3 Testes
- Cobertura unitaria: 80%+
- Testes de integracao com hardware real
- Testes de estresse (carga maxima por 24h)

---

## 7. Riscos e Mitigacoes

| Risco | Mitigacao |
|-------|-----------|
| Complexidade SOEM | Spike tecnico inicial, consulta a documentacao |
| Incompatibilidade de dispositivos | Testar com multiplos fabricantes desde inicio |
| Performance em ARM | Benchmarks continuos em hardware alvo |
| Jitter excessivo | Usar PREEMPT_RT kernel quando necessario |
| JSON do Editor incompleto/invalido | Validacao rigorosa no init() com mensagens claras |
| Divergencia topologia vs JSON | Validacao no start_loop() com log detalhado |

---

## 8. Referencias

- SOEM: https://github.com/OpenEtherCATsociety/SOEM
- ETG.1000: EtherCAT Specification
- CiA 401: Device Profile for I/O Modules
- Plugin S7Comm existente: `core/src/drivers/plugins/native/s7comm/`
- Plugin Driver API: `core/src/drivers/plugin_driver.h`
- Discovery Service (ja implementado): `webserver/discovery/`
