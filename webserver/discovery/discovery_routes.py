"""Flask Blueprint for EtherCAT Discovery REST endpoints.

Endpoints:
    GET  /api/discovery/ethercat/status     - Check if discovery service is available
    GET  /api/discovery/ethercat/interfaces - List network interfaces
    POST /api/discovery/ethercat/scan       - Scan network for EtherCAT slaves
    POST /api/discovery/ethercat/validate   - Validate configuration
    POST /api/discovery/ethercat/test       - Test connection to specific slave
"""

from dataclasses import asdict

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from webserver.discovery.ethercat_discovery import (
    DiscoveryStatus,
    is_discovery_available,
    list_network_interfaces,
    scan_network,
    test_connection,
    validate_config,
)

discovery_bp = Blueprint("discovery", __name__, url_prefix="/api/discovery")


@discovery_bp.route("/ethercat/status", methods=["GET"])
@jwt_required()
def ethercat_status():
    """Check if the EtherCAT discovery service is available.

    Returns:
        JSON with availability status and message.
    """
    available = is_discovery_available()
    if available:
        message = "Discovery service is ready"
    else:
        message = "Discovery venv not configured. Run: scripts/setup_discovery_venv.sh"
    return jsonify({"available": available, "message": message})


@discovery_bp.route("/ethercat/interfaces", methods=["GET"])
@jwt_required()
def ethercat_interfaces():
    """List available network interfaces for EtherCAT.

    Returns:
        JSON with list of interfaces and their descriptions.
    """
    result = list_network_interfaces()
    status_code = 200 if result.get("status") == DiscoveryStatus.SUCCESS.value else 500
    return jsonify(result), status_code


@discovery_bp.route("/ethercat/scan", methods=["POST"])
@jwt_required()
def ethercat_scan():
    """Scan the EtherCAT network for slave devices.

    Request body:
        {
            "interface": "eth0",       # Required: network interface name
            "timeout_ms": 5000         # Optional: scan timeout (default: 5000)
        }

    Returns:
        JSON with scan results including discovered devices.
    """
    data = request.get_json(silent=True) or {}

    interface = data.get("interface")
    if not interface:
        return jsonify({"status": "error", "message": "Missing required field: 'interface'"}), 400

    timeout_ms = data.get("timeout_ms", 5000)
    if not isinstance(timeout_ms, int) or timeout_ms <= 0:
        return (
            jsonify({"status": "error", "message": "'timeout_ms' must be a positive integer"}),
            400,
        )

    result = scan_network(interface, timeout_ms)

    # Convert dataclass to dict for JSON response
    response = {
        "status": result.status.value,
        "devices": [asdict(device) for device in result.devices],
        "message": result.message,
        "scan_time_ms": result.scan_time_ms,
        "interface": result.interface,
    }

    if result.status == DiscoveryStatus.SUCCESS:
        status_code = 200
    else:
        status_code = _status_to_http_code(result.status)
    return jsonify(response), status_code


@discovery_bp.route("/ethercat/validate", methods=["POST"])
@jwt_required()
def ethercat_validate():
    """Validate an EtherCAT configuration before deployment.

    Request body:
        {
            "interface": "eth0",
            "slaves": [
                {
                    "position": 1,
                    "vendor_id": 0x00000002,
                    "product_code": 0x044c2c52,
                    "pdo_mapping": {...}
                }
            ],
            "cycle_time_ms": 4
        }

    Returns:
        JSON with validation result, errors, and warnings.
    """
    data = request.get_json(silent=True) or {}

    if not data:
        return jsonify({"valid": False, "errors": ["Empty configuration"], "warnings": []}), 400

    result = validate_config(data)

    response = {
        "valid": result.valid,
        "errors": result.errors,
        "warnings": result.warnings,
    }

    status_code = 200 if result.valid else 400
    return jsonify(response), status_code


@discovery_bp.route("/ethercat/test", methods=["POST"])
@jwt_required()
def ethercat_test():
    """Test connection to a specific EtherCAT slave device.

    Request body:
        {
            "interface": "eth0",       # Required: network interface name
            "position": 1,             # Required: slave position (1-based)
            "timeout_ms": 3000         # Optional: timeout (default: 3000)
        }

    Returns:
        JSON with connection test result and device info if successful.
    """
    data = request.get_json(silent=True) or {}

    interface = data.get("interface")
    if not interface:
        return jsonify({"status": "error", "message": "Missing required field: 'interface'"}), 400

    position = data.get("position")
    if position is None:
        return jsonify({"status": "error", "message": "Missing required field: 'position'"}), 400
    if not isinstance(position, int) or position < 1:
        return jsonify({"status": "error", "message": "'position' must be a positive integer"}), 400

    timeout_ms = data.get("timeout_ms", 3000)
    if not isinstance(timeout_ms, int) or timeout_ms <= 0:
        return (
            jsonify({"status": "error", "message": "'timeout_ms' must be a positive integer"}),
            400,
        )

    result = test_connection(interface, position, timeout_ms)

    # Convert dataclass to dict for JSON response
    response = {
        "status": result.status.value,
        "connected": result.connected,
        "device": asdict(result.device) if result.device else None,
        "message": result.message,
        "response_time_ms": result.response_time_ms,
    }

    if result.status == DiscoveryStatus.SUCCESS:
        status_code = 200
    else:
        status_code = _status_to_http_code(result.status)
    return jsonify(response), status_code


def _status_to_http_code(status: DiscoveryStatus) -> int:
    """Convert DiscoveryStatus to appropriate HTTP status code."""
    status_map = {
        DiscoveryStatus.SUCCESS: 200,
        DiscoveryStatus.ERROR: 500,
        DiscoveryStatus.TIMEOUT: 504,
        DiscoveryStatus.PERMISSION_DENIED: 403,
        DiscoveryStatus.INTERFACE_NOT_FOUND: 404,
        DiscoveryStatus.NOT_AVAILABLE: 503,
    }
    return status_map.get(status, 500)
