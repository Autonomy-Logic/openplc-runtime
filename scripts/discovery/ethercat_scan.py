#!/usr/bin/env python3
"""EtherCAT network scanner using pysoem.

This script is executed in the discovery venv (venvs/discovery/) and communicates
with the webserver via JSON through stdout. It provides the following operations:
- list-interfaces: List available network interfaces
- scan: Scan for EtherCAT slaves on a network interface
- test: Test connection to a specific slave

Usage:
    python ethercat_scan.py list-interfaces
    python ethercat_scan.py scan --interface eth0 [--timeout 5000]
    python ethercat_scan.py test --interface eth0 --position 1 [--timeout 3000]

Output is always JSON to stdout. Errors are reported in the JSON response.
"""

import argparse
import json
import logging
import re
import sys
import time
from enum import Enum
from typing import Any

import pysoem

# Configure logging to stderr (stdout is reserved for JSON output)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

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


class EtherCATState(str, Enum):
    """EtherCAT slave states."""

    NONE = "NONE"
    INIT = "INIT"
    PREOP = "PRE-OP"
    BOOT = "BOOT"
    SAFEOP = "SAFE-OP"
    OP = "OP"
    UNKNOWN = "UNKNOWN"


# EtherCAT state mapping from pysoem state values
ETHERCAT_STATE_MAP = {
    0x00: EtherCATState.NONE,
    0x01: EtherCATState.INIT,
    0x02: EtherCATState.PREOP,
    0x03: EtherCATState.BOOT,
    0x04: EtherCATState.SAFEOP,
    0x08: EtherCATState.OP,
}


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


def _extract_slave_info(slave: Any, position: int) -> dict[str, Any]:
    """Extract device information from a pysoem slave object.

    Args:
        slave: pysoem slave object.
        position: 1-based position of the slave in the network.

    Returns:
        Dictionary with device information.
    """
    # Get slave state
    state_val = getattr(slave, "state", 0)
    state = ETHERCAT_STATE_MAP.get(state_val, EtherCATState.UNKNOWN).value

    # Check for CoE (CANopen over EtherCAT) support via mailbox protocol flags
    mbx_proto = getattr(slave, "mbx_proto", 0)
    has_coe = bool(mbx_proto & 0x04)  # Bit 2 = CoE

    # Decode name (pysoem may return bytes)
    slave_name = _decode_if_bytes(slave.name) if slave.name else f"Slave_{position}"

    return {
        "position": position,
        "name": slave_name,
        "vendor_id": getattr(slave, "man", 0),
        "product_code": getattr(slave, "id", 0),
        "revision": getattr(slave, "rev", 0),
        "serial_number": getattr(slave, "serial", 0),
        "config_address": getattr(slave, "configadr", 0) or getattr(slave, "config_address", 0),
        "alias": getattr(slave, "aliasadr", 0) or getattr(slave, "alias", 0),
        "state": state,
        "al_status_code": getattr(slave, "al_status", 0) or getattr(slave, "ALstatuscode", 0),
        "has_coe": has_coe,
        "input_bytes": getattr(slave, "input_bytes", 0) or getattr(slave, "Ibytes", 0),
        "output_bytes": getattr(slave, "output_bytes", 0) or getattr(slave, "Obytes", 0),
    }


def output_json(data: dict[str, Any]) -> None:
    """Output JSON response to stdout."""
    print(json.dumps(data, indent=2))


def _decode_if_bytes(value: Any) -> str:
    """Decode bytes to string if necessary."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value else ""


def list_interfaces() -> None:
    """List available network interfaces for EtherCAT."""
    try:
        adapters = pysoem.find_adapters()
        interfaces = [
            {
                "name": _decode_if_bytes(adapter.name),
                "description": _decode_if_bytes(adapter.desc),
            }
            for adapter in adapters
        ]
        output_json(
            {
                "status": DiscoveryStatus.SUCCESS.value,
                "interfaces": interfaces,
                "message": f"Found {len(interfaces)} network interface(s)",
            }
        )
    except Exception as e:
        output_json(
            {
                "status": DiscoveryStatus.ERROR.value,
                "interfaces": [],
                "message": f"Error listing interfaces: {e}",
            }
        )


def scan_network(interface: str, timeout_ms: int = 5000) -> None:
    """Scan the EtherCAT network for slave devices."""
    start_time = time.time()

    # Validate interface name
    is_valid, error_msg = _validate_interface_name(interface)
    if not is_valid:
        output_json(
            {
                "status": DiscoveryStatus.ERROR.value,
                "devices": [],
                "message": error_msg,
                "scan_time_ms": int((time.time() - start_time) * 1000),
                "interface": interface,
            }
        )
        return

    try:
        master = pysoem.Master()
        master.open(interface)
    except pysoem.exceptions.ConnectionError as e:
        error_msg = str(e).lower()
        if "permission" in error_msg or "operation not permitted" in error_msg:
            output_json(
                {
                    "status": DiscoveryStatus.PERMISSION_DENIED.value,
                    "devices": [],
                    "message": "Permission denied. Run with CAP_NET_RAW capability or as root.",
                    "scan_time_ms": int((time.time() - start_time) * 1000),
                    "interface": interface,
                }
            )
            return
        elif "no such device" in error_msg or "not found" in error_msg:
            output_json(
                {
                    "status": DiscoveryStatus.INTERFACE_NOT_FOUND.value,
                    "devices": [],
                    "message": f"Network interface '{interface}' not found",
                    "scan_time_ms": int((time.time() - start_time) * 1000),
                    "interface": interface,
                }
            )
            return
        else:
            output_json(
                {
                    "status": DiscoveryStatus.ERROR.value,
                    "devices": [],
                    "message": f"Failed to open interface: {e}",
                    "scan_time_ms": int((time.time() - start_time) * 1000),
                    "interface": interface,
                }
            )
            return
    except Exception as e:
        output_json(
            {
                "status": DiscoveryStatus.ERROR.value,
                "devices": [],
                "message": f"Failed to open interface: {e}",
                "scan_time_ms": int((time.time() - start_time) * 1000),
                "interface": interface,
            }
        )
        return

    try:
        # Perform network scan to detect slaves
        num_slaves = master.config_init()

        if num_slaves == 0:
            master.close()
            scan_time_ms = int((time.time() - start_time) * 1000)
            output_json(
                {
                    "status": DiscoveryStatus.SUCCESS.value,
                    "devices": [],
                    "message": "No EtherCAT slaves found on the network",
                    "scan_time_ms": scan_time_ms,
                    "interface": interface,
                }
            )
            return

        devices: list[dict[str, Any]] = []

        for i, slave in enumerate(master.slaves):
            device = _extract_slave_info(slave, i + 1)
            devices.append(device)

        master.close()

        scan_time_ms = int((time.time() - start_time) * 1000)

        output_json(
            {
                "status": DiscoveryStatus.SUCCESS.value,
                "devices": devices,
                "message": f"Found {len(devices)} EtherCAT slave(s)",
                "scan_time_ms": scan_time_ms,
                "interface": interface,
            }
        )

    except Exception as e:
        try:
            master.close()
        except Exception as close_error:
            logger.debug(f"Error closing master connection: {close_error}")

        scan_time_ms = int((time.time() - start_time) * 1000)

        if "timeout" in str(e).lower():
            output_json(
                {
                    "status": DiscoveryStatus.TIMEOUT.value,
                    "devices": [],
                    "message": f"Scan timeout: {e}",
                    "scan_time_ms": scan_time_ms,
                    "interface": interface,
                }
            )
        else:
            output_json(
                {
                    "status": DiscoveryStatus.ERROR.value,
                    "devices": [],
                    "message": f"Scan error: {e}",
                    "scan_time_ms": scan_time_ms,
                    "interface": interface,
                }
            )


def test_connection(interface: str, position: int, timeout_ms: int = 3000) -> None:
    """Test connection to a specific EtherCAT slave device."""
    if position < 1:
        output_json(
            {
                "status": DiscoveryStatus.ERROR.value,
                "connected": False,
                "device": None,
                "message": "Device position must be >= 1",
                "response_time_ms": 0,
            }
        )
        return

    start_time = time.time()

    # Validate interface name
    is_valid, error_msg = _validate_interface_name(interface)
    if not is_valid:
        output_json(
            {
                "status": DiscoveryStatus.ERROR.value,
                "connected": False,
                "device": None,
                "message": error_msg,
                "response_time_ms": int((time.time() - start_time) * 1000),
            }
        )
        return

    try:
        master = pysoem.Master()
        master.open(interface)
    except pysoem.exceptions.ConnectionError as e:
        error_msg = str(e).lower()
        status = DiscoveryStatus.ERROR.value
        if "permission" in error_msg or "operation not permitted" in error_msg:
            status = DiscoveryStatus.PERMISSION_DENIED.value
            message = "Permission denied. Run with CAP_NET_RAW capability or as root."
        elif "no such device" in error_msg or "not found" in error_msg:
            status = DiscoveryStatus.INTERFACE_NOT_FOUND.value
            message = f"Network interface '{interface}' not found"
        else:
            message = f"Failed to open interface: {e}"

        output_json(
            {
                "status": status,
                "connected": False,
                "device": None,
                "message": message,
                "response_time_ms": int((time.time() - start_time) * 1000),
            }
        )
        return
    except Exception as e:
        output_json(
            {
                "status": DiscoveryStatus.ERROR.value,
                "connected": False,
                "device": None,
                "message": f"Failed to open interface: {e}",
                "response_time_ms": int((time.time() - start_time) * 1000),
            }
        )
        return

    try:
        num_slaves = master.config_init()

        if num_slaves == 0:
            master.close()
            output_json(
                {
                    "status": DiscoveryStatus.SUCCESS.value,
                    "connected": False,
                    "device": None,
                    "message": "No EtherCAT slaves found on the network",
                    "response_time_ms": int((time.time() - start_time) * 1000),
                }
            )
            return

        if position > num_slaves:
            master.close()
            output_json(
                {
                    "status": DiscoveryStatus.ERROR.value,
                    "connected": False,
                    "device": None,
                    "message": f"No device at position {position}. Found {num_slaves} slave(s).",
                    "response_time_ms": int((time.time() - start_time) * 1000),
                }
            )
            return

        # Get device at position (0-indexed in master.slaves)
        slave = master.slaves[position - 1]
        device = _extract_slave_info(slave, position)

        master.close()

        output_json(
            {
                "status": DiscoveryStatus.SUCCESS.value,
                "connected": True,
                "device": device,
                "message": f"Successfully connected to {device['name']} at position {position}",
                "response_time_ms": int((time.time() - start_time) * 1000),
            }
        )

    except Exception as e:
        try:
            master.close()
        except Exception as close_error:
            logger.debug(f"Error closing master connection: {close_error}")

        output_json(
            {
                "status": DiscoveryStatus.ERROR.value,
                "connected": False,
                "device": None,
                "message": f"Connection test error: {e}",
                "response_time_ms": int((time.time() - start_time) * 1000),
            }
        )


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="EtherCAT network scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # list-interfaces command
    subparsers.add_parser("list-interfaces", help="List available network interfaces")

    # scan command
    scan_parser = subparsers.add_parser("scan", help="Scan for EtherCAT slaves")
    scan_parser.add_argument(
        "--interface",
        "-i",
        required=True,
        help="Network interface name (e.g., eth0)",
    )
    scan_parser.add_argument(
        "--timeout",
        "-t",
        type=int,
        default=5000,
        help="Scan timeout in milliseconds (default: 5000)",
    )

    # test command
    test_parser = subparsers.add_parser("test", help="Test connection to a specific slave")
    test_parser.add_argument(
        "--interface",
        "-i",
        required=True,
        help="Network interface name (e.g., eth0)",
    )
    test_parser.add_argument(
        "--position",
        "-p",
        type=int,
        required=True,
        help="Slave position (1-based)",
    )
    test_parser.add_argument(
        "--timeout",
        "-t",
        type=int,
        default=3000,
        help="Connection timeout in milliseconds (default: 3000)",
    )

    args = parser.parse_args()

    if args.command == "list-interfaces":
        list_interfaces()
    elif args.command == "scan":
        scan_network(args.interface, args.timeout)
    elif args.command == "test":
        test_connection(args.interface, args.position, args.timeout)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
