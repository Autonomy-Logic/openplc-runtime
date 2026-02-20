"""Flask Blueprint for Discovery REST endpoints.

Endpoints:
    GET  /api/discovery/interfaces          - List network interfaces (common)
    GET  /api/discovery/ethercat/status     - Check if EtherCAT discovery service is available
    POST /api/discovery/ethercat/scan       - Scan network for EtherCAT slaves (via SOEM plugin)
    POST /api/discovery/ethercat/validate   - Validate EtherCAT configuration
    POST /api/discovery/ethercat/test       - Test connection to specific EtherCAT slave
"""

import json
from dataclasses import asdict

from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import jwt_required

from webserver.discovery.ethercat_discovery import (
    DiscoveryStatus,
    _validate_interface_name,
    list_network_interfaces,
    test_connection,
    validate_config,
)

discovery_bp = Blueprint("discovery", __name__, url_prefix="/api/discovery")


@discovery_bp.route("/ethercat/status", methods=["GET"])
@jwt_required()
def ethercat_status():
    """
    Check if the EtherCAT discovery service is available.
    ---
    tags:
      - EtherCAT
    security:
      - BearerAuth: []
    responses:
      200:
        description: Service status retrieved successfully
        schema:
          type: object
          properties:
            available:
              type: boolean
              description: Whether the discovery service is available
            message:
              type: string
              description: Status message
    """
    # Discovery is built into the runtime via the native EtherCAT plugin (SOEM).
    # Verify the runtime is actually reachable before reporting available.
    runtime_manager = current_app.config["RUNTIME_MANAGER"]

    ping_response = runtime_manager.ping()
    if ping_response and ping_response.startswith("PING:OK"):
        return jsonify(
            {"available": True, "message": "Discovery service is ready (native SOEM plugin)"}
        )
    return jsonify({"available": False, "message": "PLC runtime is not reachable"})


@discovery_bp.route("/interfaces", methods=["GET"])
@jwt_required()
def network_interfaces():
    """
    List available network interfaces.
    ---
    tags:
      - Discovery
    security:
      - BearerAuth: []
    responses:
      200:
        description: Interfaces listed successfully
        schema:
          type: object
          properties:
            status:
              type: string
              enum:
                - success
                - error
            interfaces:
              type: array
              items:
                type: object
                properties:
                  name:
                    type: string
                    example: eth0
                  description:
                    type: string
                    example: Ethernet adapter
      500:
        description: Error retrieving interfaces
    """
    result = list_network_interfaces()
    status_code = 200 if result.get("status") == DiscoveryStatus.SUCCESS.value else 500
    return jsonify(result), status_code


@discovery_bp.route("/ethercat/scan", methods=["POST"])
@jwt_required()
def ethercat_scan():
    """
    Scan the EtherCAT network for slave devices.
    ---
    tags:
      - EtherCAT
    security:
      - BearerAuth: []
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - interface
          properties:
            interface:
              type: string
              example: eth0
              description: Network interface name
            timeout_ms:
              type: integer
              default: 5000
              description: Scan timeout in milliseconds
    responses:
      200:
        description: Scan completed successfully
        schema:
          type: object
          properties:
            status:
              type: string
              enum:
                - success
                - error
                - timeout
                - permission_denied
                - interface_not_found
                - not_available
            devices:
              type: array
              items:
                type: object
                properties:
                  position:
                    type: integer
                    example: 1
                  name:
                    type: string
                    example: EK1100
                  vendor_id:
                    type: integer
                    example: 2
                  product_code:
                    type: integer
                    example: 72100946
                  revision:
                    type: integer
                    example: 1179648
                  serial_number:
                    type: integer
                    example: 0
                  config_address:
                    type: integer
                    example: 0
                  alias:
                    type: integer
                    example: 0
                  state:
                    type: string
                    example: UNKNOWN
                  al_status_code:
                    type: integer
                    example: 0
                  has_coe:
                    type: boolean
                    example: false
                  input_bytes:
                    type: integer
                    example: 0
                  output_bytes:
                    type: integer
                    example: 0
            message:
              type: string
              example: Found 3 EtherCAT slave(s)
            scan_time_ms:
              type: integer
              example: 311
            interface:
              type: string
              example: eth0
      400:
        description: Invalid request parameters
      403:
        description: Permission denied
      404:
        description: Interface not found
      503:
        description: Discovery service not available
      504:
        description: Scan timeout
    """
    data = request.get_json(silent=True) or {}

    interface = data.get("interface")
    if not interface:
        return jsonify({"status": "error", "message": "Missing required field: 'interface'"}), 400

    is_valid, error_msg = _validate_interface_name(interface)
    if not is_valid:
        return jsonify({"status": "error", "message": error_msg}), 400

    # Route scan through the native EtherCAT plugin via unix socket
    runtime_manager = current_app.config["RUNTIME_MANAGER"]

    result = runtime_manager.send_plugin_command(
        "ethercat",
        json.dumps({"command": "scan", "params": {"interface": interface}}),
        timeout=10.0,
    )

    if "error" in result:
        return jsonify({
            "status": "error",
            "devices": [],
            "message": result["error"],
            "scan_time_ms": 0,
            "interface": interface,
        }), 500

    return jsonify({
        "status": result.get("status", "success"),
        "devices": result.get("devices", []),
        "message": result.get("message", ""),
        "scan_time_ms": 0,
        "interface": interface,
    }), 200


@discovery_bp.route("/ethercat/validate", methods=["POST"])
@jwt_required()
def ethercat_validate():
    """
    Validate an EtherCAT configuration before deployment.
    ---
    tags:
      - EtherCAT
    security:
      - BearerAuth: []
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            interface:
              type: string
              example: eth0
            slaves:
              type: array
              items:
                type: object
                properties:
                  position:
                    type: integer
                    example: 1
                  vendor_id:
                    type: integer
                    example: 2
                  product_code:
                    type: integer
                    example: 72227922
                  pdo_mapping:
                    type: object
            cycle_time_ms:
              type: integer
              example: 4
    responses:
      200:
        description: Configuration is valid
        schema:
          type: object
          properties:
            valid:
              type: boolean
            errors:
              type: array
              items:
                type: string
            warnings:
              type: array
              items:
                type: string
      400:
        description: Configuration is invalid or empty
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
    """
    Test connection to a specific EtherCAT slave device.
    ---
    tags:
      - EtherCAT
    security:
      - BearerAuth: []
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - interface
            - position
          properties:
            interface:
              type: string
              example: eth0
              description: Network interface name
            position:
              type: integer
              example: 1
              description: Slave position (1-based)
            timeout_ms:
              type: integer
              default: 3000
              description: Connection timeout in milliseconds
    responses:
      200:
        description: Connection test completed
        schema:
          type: object
          properties:
            status:
              type: string
              enum:
                - success
                - error
                - timeout
                - permission_denied
                - interface_not_found
                - not_available
            connected:
              type: boolean
            device:
              type: object
              description: Device info (null if not connected)
              properties:
                position:
                  type: integer
                  example: 1
                name:
                  type: string
                  example: EK1100
                vendor_id:
                  type: integer
                  example: 2
                product_code:
                  type: integer
                  example: 72100946
                revision:
                  type: integer
                  example: 1179648
                serial_number:
                  type: integer
                  example: 0
                config_address:
                  type: integer
                  example: 0
                alias:
                  type: integer
                  example: 0
                state:
                  type: string
                  example: UNKNOWN
                al_status_code:
                  type: integer
                  example: 0
                has_coe:
                  type: boolean
                  example: false
                input_bytes:
                  type: integer
                  example: 0
                output_bytes:
                  type: integer
                  example: 0
            message:
              type: string
            response_time_ms:
              type: integer
      400:
        description: Invalid request parameters
      403:
        description: Permission denied
      404:
        description: Interface not found
      503:
        description: Discovery service not available
      504:
        description: Connection timeout
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
