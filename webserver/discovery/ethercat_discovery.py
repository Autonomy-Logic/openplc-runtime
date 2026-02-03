"""EtherCAT network discovery service.

This module provides an interface to scan EtherCAT networks by invoking
the ethercat_scan.py script in the discovery venv (venvs/discovery/).
Communication is done via subprocess with JSON output.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from webserver.logger import get_logger

logger, _ = get_logger("ethercat_discovery")

# Interface name validation
# Linux interface names: eth0, enp3s0, eno1, wlan0, br-docker0, veth123abc
INTERFACE_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
MAX_INTERFACE_NAME_LENGTH = 15  # IFNAMSIZ - 1

# Paths relative to project root
PROJECT_ROOT = Path(__file__).parent.parent.parent
DISCOVERY_VENV = PROJECT_ROOT / "venvs" / "discovery"
DISCOVERY_SCRIPT = PROJECT_ROOT / "scripts" / "discovery" / "ethercat_scan.py"


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
class EtherCATScanResult:
    """Result of an EtherCAT network scan operation."""

    status: DiscoveryStatus
    devices: list[EtherCATDevice] = field(default_factory=list)
    message: str = ""
    scan_time_ms: int = 0
    interface: str = ""


@dataclass
class EtherCATValidationResult:
    """Result of an EtherCAT configuration validation."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class EtherCATConnectionTestResult:
    """Result of an EtherCAT connection test."""

    status: DiscoveryStatus
    connected: bool = False
    device: EtherCATDevice | None = None
    message: str = ""
    response_time_ms: int = 0


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


def _dict_to_device(dev_dict: dict[str, Any]) -> EtherCATDevice:
    """Convert a device dictionary to EtherCATDevice dataclass.

    Args:
        dev_dict: Dictionary with device information from scanner script.

    Returns:
        EtherCATDevice instance.
    """
    return EtherCATDevice(
        position=dev_dict.get("position", 0),
        name=dev_dict.get("name", ""),
        vendor_id=dev_dict.get("vendor_id", 0),
        product_code=dev_dict.get("product_code", 0),
        revision=dev_dict.get("revision", 0),
        serial_number=dev_dict.get("serial_number", 0),
        config_address=dev_dict.get("config_address", 0),
        alias=dev_dict.get("alias", 0),
        state=dev_dict.get("state", "UNKNOWN"),
        al_status_code=dev_dict.get("al_status_code", 0),
        has_coe=dev_dict.get("has_coe", False),
        input_bytes=dev_dict.get("input_bytes", 0),
        output_bytes=dev_dict.get("output_bytes", 0),
    )


def is_discovery_available() -> bool:
    """Check if the discovery venv and script are available.

    Returns:
        True if discovery venv exists and script is present.
    """
    venv_python = DISCOVERY_VENV / "bin" / "python"
    return venv_python.exists() and DISCOVERY_SCRIPT.exists()


def _run_discovery_script(args: list[str], timeout_seconds: int = 30) -> dict[str, Any]:
    """Run the discovery script in the discovery venv.

    Args:
        args: Command line arguments for the script.
        timeout_seconds: Subprocess timeout in seconds.

    Returns:
        Parsed JSON response from the script.
    """
    venv_python = DISCOVERY_VENV / "bin" / "python"

    if not venv_python.exists():
        return {
            "status": DiscoveryStatus.NOT_AVAILABLE.value,
            "message": f"Discovery venv not found at {DISCOVERY_VENV}. "
            "Run: scripts/setup_discovery_venv.sh",
        }

    if not DISCOVERY_SCRIPT.exists():
        return {
            "status": DiscoveryStatus.NOT_AVAILABLE.value,
            "message": f"Discovery script not found at {DISCOVERY_SCRIPT}",
        }

    cmd = [str(venv_python), str(DISCOVERY_SCRIPT)] + args

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

        if result.returncode != 0 and not result.stdout:
            return {
                "status": DiscoveryStatus.ERROR.value,
                "message": f"Script error: {result.stderr}",
            }

        return json.loads(result.stdout)

    except subprocess.TimeoutExpired:
        return {
            "status": DiscoveryStatus.TIMEOUT.value,
            "message": f"Discovery script timed out after {timeout_seconds} seconds",
        }
    except json.JSONDecodeError as e:
        return {
            "status": DiscoveryStatus.ERROR.value,
            "message": f"Failed to parse script output: {e}",
        }
    except Exception as e:
        return {
            "status": DiscoveryStatus.ERROR.value,
            "message": f"Failed to run discovery script: {e}",
        }


def list_network_interfaces() -> dict[str, Any]:
    """List available network interfaces for EtherCAT.

    Returns:
        Dictionary with status, interfaces list, and message.
    """
    if not is_discovery_available():
        return {
            "status": DiscoveryStatus.NOT_AVAILABLE.value,
            "interfaces": [],
            "message": "Discovery service not available. "
            "Run: scripts/setup_discovery_venv.sh",
        }

    return _run_discovery_script(["list-interfaces"])


def scan_network(interface: str, timeout_ms: int = 5000) -> EtherCATScanResult:
    """Scan the EtherCAT network for slave devices.

    Args:
        interface: Network interface name (e.g., 'eth0', 'enp3s0').
        timeout_ms: Scan timeout in milliseconds (default: 5000).

    Returns:
        EtherCATScanResult containing discovered devices and status.
    """
    # Validate interface name
    is_valid, error_msg = _validate_interface_name(interface)
    if not is_valid:
        return EtherCATScanResult(
            status=DiscoveryStatus.ERROR,
            message=error_msg,
            interface=interface,
        )

    if not is_discovery_available():
        return EtherCATScanResult(
            status=DiscoveryStatus.NOT_AVAILABLE,
            message="Discovery service not available. "
            "Run: scripts/setup_discovery_venv.sh",
            interface=interface,
        )

    # Calculate subprocess timeout (scan timeout + buffer)
    subprocess_timeout = (timeout_ms / 1000) + 10

    result = _run_discovery_script(
        ["scan", "--interface", interface, "--timeout", str(timeout_ms)],
        timeout_seconds=int(subprocess_timeout),
    )

    # Convert devices from dict to EtherCATDevice objects
    devices = [_dict_to_device(d) for d in result.get("devices", [])]

    return EtherCATScanResult(
        status=DiscoveryStatus(result.get("status", "error")),
        devices=devices,
        message=result.get("message", ""),
        scan_time_ms=result.get("scan_time_ms", 0),
        interface=result.get("interface", interface),
    )


def test_connection(
    interface: str,
    device_position: int,
    timeout_ms: int = 3000,
) -> EtherCATConnectionTestResult:
    """Test connection to a specific EtherCAT slave device.

    Args:
        interface: Network interface name.
        device_position: Position of the slave device (1-based).
        timeout_ms: Connection timeout in milliseconds.

    Returns:
        EtherCATConnectionTestResult with connection status.
    """
    # Validate interface name
    is_valid, error_msg = _validate_interface_name(interface)
    if not is_valid:
        return EtherCATConnectionTestResult(
            status=DiscoveryStatus.ERROR,
            message=error_msg,
        )

    if device_position < 1:
        return EtherCATConnectionTestResult(
            status=DiscoveryStatus.ERROR,
            message="Device position must be >= 1",
        )

    if not is_discovery_available():
        return EtherCATConnectionTestResult(
            status=DiscoveryStatus.NOT_AVAILABLE,
            message="Discovery service not available. "
            "Run: scripts/setup_discovery_venv.sh",
        )

    # Calculate subprocess timeout
    subprocess_timeout = (timeout_ms / 1000) + 10

    result = _run_discovery_script(
        [
            "test",
            "--interface",
            interface,
            "--position",
            str(device_position),
            "--timeout",
            str(timeout_ms),
        ],
        timeout_seconds=int(subprocess_timeout),
    )

    # Convert device dict to EtherCATDevice if present
    device = _dict_to_device(result["device"]) if result.get("device") else None

    return EtherCATConnectionTestResult(
        status=DiscoveryStatus(result.get("status", "error")),
        connected=result.get("connected", False),
        device=device,
        message=result.get("message", ""),
        response_time_ms=result.get("response_time_ms", 0),
    )


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
