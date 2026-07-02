# Plano de Desenvolvimento - Plugin EtherCAT com ebpfcat (Runtime)

**Produto:** OpenPLC Runtime v4
**Baseado em:** Levantamento de Requisitos - Protocolo EtherCAT (v1.3)
**Data:** 29 de Janeiro de 2026
**Tipo:** Plugin Python (ebpfcat)
**Status:** Alternativo ao plugin SOEM
**Revisao:** 2.0 - Arquitetura Editor-driven (discovery e parametrizacao no Editor)

---

## 1. Visao Geral

Este plano descreve uma implementacao **alternativa** do EtherCAT Master usando a biblioteca
ebpfcat. O objetivo e manter **compatibilidade total com o JSON de configuracao** gerado pelo
Editor, permitindo que o usuario escolha entre o backend SOEM (C/C++) ou ebpfcat (Python/eBPF)
sem modificar a configuracao.

### 1.1 Separacao de Responsabilidades

Assim como o plugin SOEM, o plugin ebpfcat **apenas executa** a configuracao recebida do Editor.
Toda a parametrizacao (discovery, ESI, couplers, modulos, channels, located vars) e feita no
Editor.

```
┌─────────────────────────────────────────────────────────────────┐
│                      OpenPLC Editor                              │
│         (Configuracao completa EtherCAT)                         │
│                                                                  │
│  Discovery, ESI parsing, parametrizacao,                         │
│  channels, located vars, PDO mapping, SDO config                 │
└─────────────────────────────────┬────────────────────────────────┘
                                  │
                                  ▼
                     ┌────────────────────────┐
                     │  ethercat_config.json   │  <-- Formato UNICO
                     │  (Gerado pelo Editor)   │      (contrato)
                     └────────────────────────┘
                                  │
                  ┌───────────────┴───────────────┐
                  ▼                               ▼
┌──────────────────────────────┐  ┌──────────────────────────────┐
│   Plugin SOEM (Nativo C/C++) │  │  Plugin ebpfcat (Python)     │
│   - Raw sockets              │  │  - eBPF/XDP                  │
│   - Qualquer NIC             │  │  - NICs com suporte XDP      │
│   - Kernel 4.x+              │  │  - Kernel 5.x+               │
│   - Mesmo JSON config        │  │  - Mesmo JSON config         │
└──────────────────────────────┘  └──────────────────────────────┘
```

### 1.2 Quando Usar Cada Backend

| Cenario | Backend Recomendado |
|---------|---------------------|
| Producao geral | SOEM |
| Hardware variado | SOEM |
| ARM embarcado | SOEM |
| Cycle time < 500us | ebpfcat |
| Ambiente de pesquisa | ebpfcat |
| Integracao com EPICS | ebpfcat |

### 1.3 Escopo do MVP (mesmo do SOEM)

- Carregamento do JSON de configuracao gerado pelo Editor
- Validacao de topologia (slaves fisicos vs configuracao)
- Mapeamento de PDOs para variaveis do PLC conforme JSON
- Aplicacao de configuracoes SDO nos slaves
- Operacao estavel com cycle time de 4 ms
- Suporte a CoE (SDO e PDO)
- Diagnostico basico de status e erros

### 1.4 Discovery Service (JA IMPLEMENTADO - Compartilhado)

O Discovery Service e compartilhado com o plugin SOEM e ja esta implementado.
Ver documentacao completa em `docs/old_docs/ethercat-plugin-development-plan.md` secao 1.0.2.

---

## 2. Arquitetura do Plugin

### 2.1 Estrutura de Arquivos

```
core/src/drivers/plugins/python/ethercat_ebpf/
├── __init__.py
├── plugin.py                      # Entry point (init, start_loop, stop_loop)
├── master.py                      # Wrapper do ebpfcat master
├── config_loader.py               # Carrega ethercat_config.json (formato compartilhado)
├── pdo_mapper.py                  # Mapeia PDOs para buffers OpenPLC
├── sdo_handler.py                 # Operacoes SDO
├── diagnostics.py                 # Coleta de status e erros
├── state_machine.py               # Gerencia estados dos slaves
├── devices/                       # Device classes especificos
│   ├── __init__.py
│   ├── generic_io.py              # Dispositivo I/O generico (DS401)
│   ├── beckhoff.py                # Suporte especifico Beckhoff
│   └── digital_io.py              # Modulos digitais
├── requirements.txt               # Dependencias (ebpfcat, etc.)
└── README.md
```

### 2.2 Configuracao em plugins.conf

```
# Backend SOEM (nativo) - padrao
ethercat,./build/plugins/libethercat_plugin.so,1,1,./core/src/drivers/plugins/native/ethercat/ethercat_config.json,

# Backend ebpfcat (Python) - alternativo
# ethercat_ebpf,./core/src/drivers/plugins/python/ethercat_ebpf/plugin.py,0,0,./core/src/drivers/plugins/native/ethercat/ethercat_config.json,./venvs/ethercat_ebpf
```

**Nota:** Ambos os plugins usam o **mesmo arquivo de configuracao** (`ethercat_config.json`),
gerado pelo Editor. O JSON segue o contrato definido em `docs/old_docs/ethercat-plugin-development-plan.md`
secao 3.

### 2.3 Formato JSON de Configuracao

O formato JSON e **identico** ao definido no plano SOEM. Ver secao 3 do documento
`docs/old_docs/ethercat-plugin-development-plan.md` para a especificacao completa.

Resumo da estrutura (lista flat de slaves, alinhada com `ec_slave[]` da SOEM):
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
          "channels": [ ... ],
          "sdo_configurations": [ ... ],
          "rx_pdos": [ ... ],
          "tx_pdos": [ ... ]
        }
      ],
      "diagnostics": { ... }
    }
  }
]
```

---

## 3. Etapas de Desenvolvimento

### Fase 1: Setup e Infraestrutura (2-3 semanas)

#### Etapa 1.1: Ambiente e Dependencias
**Duracao estimada:** 3-4 dias

**Tarefas:**
1. Criar estrutura de diretorios do plugin
2. Configurar virtual environment dedicado
3. Instalar ebpfcat e dependencias
4. Verificar requisitos de sistema (kernel, XDP)
5. Criar script de verificacao de compatibilidade

**Arquivos:**
- `requirements.txt`
- `__init__.py`
- `scripts/check_ebpf_support.sh`

**requirements.txt:**
```
ebpfcat>=1.0.0
```

**Criterio de Aceite:**
- Plugin carrega sem erros
- Dependencias instaladas
- Verificacao de compatibilidade funcional

#### Etapa 1.2: Loader de Configuracao Compartilhada
**Duracao estimada:** 2-3 dias

**Tarefas:**
1. Criar parser do JSON de configuracao (formato identico ao SOEM)
2. Validar formato (mesmo schema)
3. Converter para estruturas internas do ebpfcat
4. Implementar validacao estrutural

**Arquivos:**
- `config_loader.py`

**Implementacao:**
```python
# config_loader.py
import json
from dataclasses import dataclass, field
from typing import List, Optional
from pathlib import Path


@dataclass
class PdoEntry:
    index: str
    subindex: int
    bit_length: int
    name: str = ""
    data_type: str = "BOOL"


@dataclass
class Pdo:
    index: str
    name: str = ""
    entries: List[PdoEntry] = field(default_factory=list)


@dataclass
class SdoConfig:
    index: str
    subindex: int
    value: any = 0
    data_type: str = "UINT16"
    name: str = ""
    description: str = ""


@dataclass
class Channel:
    index: int
    name: str
    type: str
    bit_length: int
    iec_location: str
    pdo_index: str
    pdo_entry_index: str
    pdo_entry_subindex: int


@dataclass
class Slave:
    position: int
    name: str
    type: str
    vendor_id: str
    product_code: str
    revision: str = ""
    channels: List[Channel] = field(default_factory=list)
    sdo_configurations: List[SdoConfig] = field(default_factory=list)
    rx_pdos: List[Pdo] = field(default_factory=list)
    tx_pdos: List[Pdo] = field(default_factory=list)


@dataclass
class MasterConfig:
    interface: str
    cycle_time_us: int = 1000
    watchdog_timeout_cycles: int = 3
    log_level: str = "info"


@dataclass
class DiagnosticsConfig:
    log_connections: bool = True
    log_data_access: bool = False
    log_errors: bool = True
    max_log_entries: int = 10000
    status_update_interval_ms: int = 500


@dataclass
class EtherCATConfig:
    master: MasterConfig
    slaves: List[Slave]
    diagnostics: DiagnosticsConfig


def load_config(config_path: str) -> EtherCATConfig:
    """Carrega configuracao do JSON gerado pelo Editor."""
    with open(config_path, "r") as f:
        data = json.load(f)

    # Formato padrao OpenPLC: array com primeiro elemento
    plugin_data = data[0] if isinstance(data, list) else data
    ecat = plugin_data.get("config", plugin_data)

    master = MasterConfig(
        interface=ecat["master"]["interface"],
        cycle_time_us=ecat["master"].get("cycle_time_us", 1000),
        watchdog_timeout_cycles=ecat["master"].get("watchdog_timeout_cycles", 3),
        log_level=ecat["master"].get("log_level", "info"),
    )

    # Lista flat de slaves (alinhada com ec_slave[] da SOEM)
    slaves = []
    for slave_data in ecat.get("slaves", []):
        channels = [
            Channel(**ch) for ch in slave_data.get("channels", [])
        ]
        sdos = [
            SdoConfig(**sdo) for sdo in slave_data.get("sdo_configurations", [])
        ]
        rx_pdos = [
            Pdo(
                index=p["index"],
                name=p.get("name", ""),
                entries=[PdoEntry(**e) for e in p.get("entries", [])],
            )
            for p in slave_data.get("rx_pdos", [])
        ]
        tx_pdos = [
            Pdo(
                index=p["index"],
                name=p.get("name", ""),
                entries=[PdoEntry(**e) for e in p.get("entries", [])],
            )
            for p in slave_data.get("tx_pdos", [])
        ]
        slaves.append(Slave(
            position=slave_data["position"],
            name=slave_data["name"],
            type=slave_data["type"],
            vendor_id=slave_data["vendor_id"],
            product_code=slave_data["product_code"],
            revision=slave_data.get("revision", ""),
            channels=channels,
            sdo_configurations=sdos,
            rx_pdos=rx_pdos,
            tx_pdos=tx_pdos,
        ))

    diag_data = ecat.get("diagnostics", {})
    diagnostics = DiagnosticsConfig(
        log_connections=diag_data.get("log_connections", True),
        log_data_access=diag_data.get("log_data_access", False),
        log_errors=diag_data.get("log_errors", True),
        max_log_entries=diag_data.get("max_log_entries", 10000),
        status_update_interval_ms=diag_data.get("status_update_interval_ms", 500),
    )

    return EtherCATConfig(
        master=master,
        slaves=slaves,
        diagnostics=diagnostics,
    )
```

**Criterio de Aceite:**
- JSON parseado corretamente
- Estruturas identicas ao esperado pelo SOEM
- Validacao de campos obrigatorios

#### Etapa 1.3: Plugin Entry Point
**Duracao estimada:** 3-4 dias

**Tarefas:**
1. Implementar `init()` com PyCapsule
2. Implementar `start_loop()` e `stop_loop()`
3. Integrar com SafeBufferAccess
4. Integrar com sistema de logging
5. Carregar e validar JSON config

**Arquivos:**
- `plugin.py`

**Implementacao:**
```python
# plugin.py
"""
EtherCAT Master Plugin using ebpfcat.
Compativel com o mesmo JSON de configuracao do plugin SOEM.
O JSON e gerado pelo Editor com toda a parametrizacao.
"""
import threading
from typing import Optional

from shared import (
    SafeBufferAccess,
    safe_extract_runtime_args_from_capsule,
    PluginLogger,
    SafeLoggingAccess,
)

from .config_loader import load_config, EtherCATConfig
from .master import EbpfcatMaster

# Globais do plugin
_runtime_args = None
_buffer_accessor: Optional[SafeBufferAccess] = None
_logger: Optional[PluginLogger] = None
_config: Optional[EtherCATConfig] = None
_master: Optional[EbpfcatMaster] = None
_master_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def init(args_capsule) -> bool:
    """Inicializa o plugin com argumentos do runtime."""
    global _runtime_args, _buffer_accessor, _logger, _config

    _runtime_args, error_msg = safe_extract_runtime_args_from_capsule(args_capsule)
    if _runtime_args is None:
        print(f"[ETHERCAT_EBPF] Failed to extract runtime args: {error_msg}")
        return False

    logging_accessor = SafeLoggingAccess(_runtime_args)
    _logger = PluginLogger()
    _logger.initialize(logging_accessor)
    _logger.info("Initializing EtherCAT ebpfcat plugin")

    _buffer_accessor = SafeBufferAccess(_runtime_args)
    if not _buffer_accessor.is_valid:
        _logger.error("Failed to create buffer accessor")
        return False

    # Carregar JSON config gerado pelo Editor
    config_path, err = _buffer_accessor.get_config_path()
    if err:
        _logger.error(f"Failed to get config path: {err}")
        return False

    try:
        _config = load_config(config_path)
        _logger.info(
            f"Loaded config: interface={_config.master.interface}, "
            f"cycle_time={_config.master.cycle_time_us}us, "
            f"slaves={len(_config.slaves)}"
        )
    except Exception as e:
        _logger.error(f"Failed to load config: {e}")
        return False

    _logger.info("EtherCAT ebpfcat plugin initialized successfully")
    return True


def start_loop() -> bool:
    """Inicia o loop do master EtherCAT."""
    global _master, _master_thread

    if _config is None or _buffer_accessor is None:
        _logger.error("Plugin not initialized")
        return False

    _logger.info("Starting EtherCAT master loop")
    _stop_event.clear()

    try:
        _master = EbpfcatMaster(
            config=_config,
            buffer_accessor=_buffer_accessor,
            logger=_logger,
        )

        # Inicializar: scan, validar topologia, aplicar SDOs
        if not _master.initialize():
            _logger.error("Failed to initialize EtherCAT master")
            return False

        _master_thread = threading.Thread(
            target=_master_loop,
            name="ethercat_ebpf_master",
            daemon=True,
        )
        _master_thread.start()

        _logger.info("EtherCAT master loop started")
        return True

    except Exception as e:
        _logger.error(f"Failed to start master: {e}")
        return False


def _master_loop():
    """Loop principal do master (executa em thread separada)."""
    while not _stop_event.is_set():
        try:
            _master.run_cycle()
        except Exception as e:
            _logger.error(f"Error in master cycle: {e}")


def stop_loop() -> bool:
    """Para o loop do master."""
    global _master, _master_thread

    _logger.info("Stopping EtherCAT master loop")
    _stop_event.set()

    if _master_thread is not None:
        _master_thread.join(timeout=5.0)
        _master_thread = None

    if _master is not None:
        _master.shutdown()
        _master = None

    _logger.info("EtherCAT master loop stopped")
    return True


def cleanup():
    """Limpa recursos do plugin."""
    global _runtime_args, _buffer_accessor, _logger, _config

    if _logger:
        _logger.info("Cleaning up EtherCAT ebpfcat plugin")

    _runtime_args = None
    _buffer_accessor = None
    _logger = None
    _config = None
```

**Criterio de Aceite:**
- Plugin carrega e inicializa
- Logging funciona
- Configuracao carregada do JSON

---

### Fase 2: Integracao com ebpfcat (3-4 semanas)

#### Etapa 2.1: Wrapper do Master ebpfcat
**Duracao estimada:** 1-2 semanas

**Tarefas:**
1. Criar classe wrapper para ebpfcat.Master
2. Implementar scan de rede e validacao de slaves contra JSON
3. Implementar inicializacao de slaves
4. Gerenciar ciclo de comunicacao

**Arquivos:**
- `master.py`

**Fluxo de inicializacao (mesmo do SOEM):**
```
initialize():
  1. Criar ebpfcat.Master na interface do JSON
  2. Scan da rede
  3. Validar slaves: slaves fisicos == lista slaves no JSON
     - Comparar vendor_id, product_code de cada slave por position
     - Se divergir -> logar erro e retornar False
  4. Aplicar SDOs conforme sdo_configurations de cada slave no JSON
  5. Configurar PDO mapping conforme JSON
  6. Transicionar slaves para OP
```

**Criterio de Aceite:**
- Master inicializa com ebpfcat
- Slaves validados contra JSON
- Scan detecta slaves
- Ciclo executa sem erros

#### Etapa 2.2: Mapeamento de PDOs
**Duracao estimada:** 1 semana

**Tarefas:**
1. Calcular offsets de bytes/bits a partir dos PDOs no JSON
2. Considerar padding entries no calculo
3. Mapear PDOs do ebpfcat para buffers OpenPLC
4. Suportar tipos de dados (bool, int, dint, lint)
5. Usar journal writes para saidas
6. Implementar leitura de entradas

**Arquivos:**
- `pdo_mapper.py`

**Criterio de Aceite:**
- PDOs lidos dos slaves
- Valores escritos nos buffers corretos
- Journal writes funcionando

#### Etapa 2.3: Aplicacao de SDOs
**Duracao estimada:** 3-4 dias

**Tarefas:**
1. Implementar leitura de SDO
2. Implementar escrita de SDO
3. Iterar sobre sdo_configurations de cada slave no JSON
4. Aplicar configuracoes no startup (Pre-Op)

**Arquivos:**
- `sdo_handler.py`

**Criterio de Aceite:**
- SDOs lidos corretamente
- Configuracoes aplicadas no startup conforme JSON

#### Etapa 2.4: Maquina de Estados
**Duracao estimada:** 3-4 dias

**Tarefas:**
1. Implementar transicoes Init -> Pre-Op -> Safe-Op -> Op
2. Tratar erros de transicao
3. Monitorar estado atual

**Arquivos:**
- `state_machine.py`

**Criterio de Aceite:**
- Slaves transicionam para OP
- Erros detectados e reportados

---

### Fase 3: Diagnostico e API (1-2 semanas)

#### Etapa 3.1: Sistema de Diagnostico
**Duracao estimada:** 3-4 dias

**Tarefas:**
1. Coletar status do master
2. Coletar status de cada slave
3. Registrar erros com timestamp
4. Expor via estrutura interna

**Arquivos:**
- `diagnostics.py`

**Criterio de Aceite:**
- Diagnosticos coletados
- Formato compativel com SOEM

#### Etapa 3.2: Integracao com API REST
**Duracao estimada:** 2-3 dias

**Tarefas:**
1. Expor diagnosticos nos mesmos endpoints do SOEM
2. Formato de resposta identico

**Criterio de Aceite:**
- Mesmos endpoints funcionais
- Respostas compativeis

---

### Fase 4: Testes e Documentacao (1-2 semanas)

#### Etapa 4.1: Testes
**Duracao estimada:** 1 semana

**Tarefas:**
1. Testes unitarios
2. Testes de integracao
3. Testes com hardware real (se disponivel)
4. Comparacao de performance com SOEM

**Criterio de Aceite:**
- Testes passando
- Documentacao de limitacoes

#### Etapa 4.2: Documentacao
**Duracao estimada:** 2-3 dias

**Tarefas:**
1. Documentar requisitos de sistema (kernel, XDP)
2. Documentar diferenças em relação ao SOEM
3. Guia de troubleshooting

**Criterio de Aceite:**
- Documentacao completa

---

## 4. Requisitos de Sistema (ebpfcat)

### 4.1 Kernel Linux

| Requisito | Especificacao |
|-----------|---------------|
| Versao minima | 5.4+ (recomendado 5.10+) |
| Configuracoes | CONFIG_BPF=y, CONFIG_BPF_SYSCALL=y |
| Permissoes | CAP_BPF, CAP_NET_ADMIN ou root |

### 4.2 Driver de Rede

O driver deve suportar XDP. Drivers compativeis incluem:

| Driver | Suporte XDP |
|--------|-------------|
| mlx5 (Mellanox) | Completo |
| i40e (Intel) | Completo |
| ixgbe (Intel) | Completo |
| igb (Intel) | Parcial |
| e1000e (Intel) | Limitado |
| virtio-net | Sim (VMs) |
| r8169 (Realtek) | Nao |

### 4.3 Verificacao de Compatibilidade

Script para verificar sistema:

```bash
#!/bin/bash
# check_ebpf_support.sh

echo "=== Verificacao de Suporte eBPF/XDP ==="

# Kernel version
KERNEL=$(uname -r)
echo "Kernel: $KERNEL"

# eBPF support
if [ -d /sys/fs/bpf ]; then
    echo "eBPF filesystem: OK"
else
    echo "eBPF filesystem: NOT MOUNTED"
fi

# XDP support na interface
IFACE=${1:-eth0}
if ethtool -i $IFACE 2>/dev/null | grep -q driver; then
    DRIVER=$(ethtool -i $IFACE | grep driver | awk '{print $2}')
    echo "Interface $IFACE driver: $DRIVER"

    # Testar XDP
    if ip link set dev $IFACE xdp off 2>/dev/null; then
        echo "XDP support on $IFACE: OK"
    else
        echo "XDP support on $IFACE: UNKNOWN (needs test)"
    fi
else
    echo "Interface $IFACE: NOT FOUND"
fi

# Capabilities
if capsh --print | grep -q cap_bpf; then
    echo "CAP_BPF: Available"
else
    echo "CAP_BPF: Requires root or capabilities"
fi
```

---

## 5. Comparacao de Implementacao

| Aspecto | Plugin SOEM | Plugin ebpfcat |
|---------|-------------|----------------|
| Linguagem | C/C++ | Python |
| Tipo plugin | Nativo (type=1) | Python (type=0) |
| Ciclo hooks | cycle_start/cycle_end | Thread separada |
| Buffer access | Direto com mutex | SafeBufferAccess |
| Configuracao | ethercat_config.json | ethercat_config.json |
| JSON format | Identico | Identico |
| Kernel | 4.x+ | 5.x+ |
| NIC | Qualquer | XDP compativel |
| Plataformas | x86, ARM64, ARMv7 | x86 (ARM experimental) |

---

## 6. Riscos Especificos do ebpfcat

| Risco | Probabilidade | Impacto | Mitigacao |
|-------|---------------|---------|-----------|
| Driver sem XDP | Alta | Bloqueante | Documentar NICs compativeis |
| API ebpfcat instavel | Media | Alto | Fixar versao, testar atualizacoes |
| Performance em Python | Media | Medio | Ciclo critico em eBPF, nao Python |
| Kernel antigo | Media | Bloqueante | Documentar requisitos |
| Suporte ARM | Alta | Bloqueante | Marcar como experimental |

---

## 7. Referencias

- ebpfcat: https://ebpfcat.readthedocs.io/
- ebpfcat GitHub: https://github.com/tecki/ebpfcat
- eBPF Documentation: https://ebpf.io/
- XDP Tutorial: https://github.com/xdp-project/xdp-tutorial
- Plugin Python existente (modbus_master): `core/src/drivers/plugins/python/modbus_master/`
- JSON config spec: `docs/old_docs/ethercat-plugin-development-plan.md` secao 3
- Discovery Service (ja implementado): `webserver/discovery/`
