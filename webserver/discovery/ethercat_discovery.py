# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""EtherCAT discovery helpers.

Validation utilities for EtherCAT configuration and interface names.
Network operations (scan, list-interfaces, test) are handled by the
native EtherCAT plugin via plugin commands routed through the unix socket.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Interface name validation
# Linux interface names: eth0, enp3s0, eno1, wlan0, br-docker0, veth123abc
INTERFACE_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
MAX_INTERFACE_NAME_LENGTH = 15  # IFNAMSIZ - 1


class DiscoveryStatus(str, Enum):
    """Status codes for discovery operations."""

    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    INTERFACE_NOT_FOUND = "interface_not_found"
    NOT_AVAILABLE = "not_available"


@dataclass
class EtherCATDevice:
    """Information about a discovered EtherCAT slave device."""

    position: int
    name: str
    vendor_id: int = 0
    product_code: int = 0
    revision: int = 0
    serial_number: int = 0
    config_address: int = 0
    alias: int = 0
    state: str = "UNKNOWN"
    al_status_code: int = 0
    has_coe: bool = False
    input_bytes: int = 0
    output_bytes: int = 0


@dataclass
class EtherCATValidationResult:
    """Result of an EtherCAT configuration validation."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _validate_interface_name(interface: str) -> tuple[bool, str]:
    """Validate network interface name.

    Args:
        interface: Network interface name to validate.

    Returns:
        Tuple of (is_valid, error_message).
    """
    if not interface:
        return False, "Interface name cannot be empty"
    if len(interface) > MAX_INTERFACE_NAME_LENGTH:
        return False, f"Interface name too long (max {MAX_INTERFACE_NAME_LENGTH} chars)"
    if not INTERFACE_NAME_PATTERN.match(interface):
        return False, "Invalid interface name format"
    return True, ""


def validate_config(config: dict[str, Any]) -> EtherCATValidationResult:
    """Validate an EtherCAT configuration before deployment.

    This validation runs locally without requiring the discovery venv.

    Args:
        config: Configuration dictionary to validate.

    Returns:
        EtherCATValidationResult indicating if config is valid.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Check required fields
    if "interface" not in config:
        errors.append("Missing required field: 'interface'")

    if "slaves" not in config:
        errors.append("Missing required field: 'slaves'")
    elif not isinstance(config["slaves"], list):
        errors.append("Field 'slaves' must be a list")
    elif len(config["slaves"]) == 0:
        warnings.append("No slaves configured")
    else:
        # Validate each slave configuration
        for i, slave in enumerate(config["slaves"]):
            slave_prefix = f"slaves[{i}]"

            if "position" not in slave:
                errors.append(f"{slave_prefix}: Missing required field 'position'")
            elif not isinstance(slave["position"], int) or slave["position"] < 1:
                errors.append(f"{slave_prefix}: 'position' must be a positive integer")

            if "vendor_id" not in slave:
                msg = f"{slave_prefix}: Missing 'vendor_id' - device matching may fail"
                warnings.append(msg)

            if "product_code" not in slave:
                msg = f"{slave_prefix}: Missing 'product_code' - device matching may fail"
                warnings.append(msg)

            # Validate PDO mappings if present
            if "pdo_mapping" in slave:
                pdo = slave["pdo_mapping"]
                if "inputs" in pdo:
                    for j, inp in enumerate(pdo.get("inputs", [])):
                        if "address" not in inp:
                            msg = f"{slave_prefix}.pdo_mapping.inputs[{j}]: Missing 'address'"
                            errors.append(msg)
                if "outputs" in pdo:
                    for j, out in enumerate(pdo.get("outputs", [])):
                        if "address" not in out:
                            msg = f"{slave_prefix}.pdo_mapping.outputs[{j}]: Missing 'address'"
                            errors.append(msg)

    # Validate optional cycle_time
    if "cycle_time_ms" in config:
        cycle_time = config["cycle_time_ms"]
        if not isinstance(cycle_time, (int, float)) or cycle_time <= 0:
            errors.append("'cycle_time_ms' must be a positive number")
        elif cycle_time < 1:
            warnings.append("'cycle_time_ms' < 1ms may not be achievable without PREEMPT_RT kernel")

    return EtherCATValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )
