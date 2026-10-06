# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Flask Blueprint for Discovery REST endpoints.

Endpoints:
    GET  /api/discovery/interfaces          - List network interfaces (common)
    GET  /api/discovery/ethercat/status     - Check if EtherCAT discovery service is available
    POST /api/discovery/ethercat/scan       - Scan network for EtherCAT slaves (via EtherDOG)
    POST /api/discovery/ethercat/validate   - Validate EtherCAT configuration
    POST /api/discovery/ethercat/test       - Test connection to specific EtherCAT slave
"""

import json

from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import jwt_required

from webserver.discovery.ethercat_discovery import (
    DiscoveryStatus,
    _validate_interface_name,
    validate_config,
)

discovery_bp = Blueprint("discovery", __name__, url_prefix="/api/discovery")


def _with_plugin_state(result: dict) -> dict:
    """Add the Editor's "plugin_state" key next to EtherDOG's per-master "state"."""
    for master in result.get("masters", []):
        if isinstance(master, dict) and "state" in master:
            master.setdefault("plugin_state", master["state"])
    return result


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
    # Discovery is served by EtherDOG, the EtherCAT master service.
    etherdog = current_app.config["ETHERDOG_MANAGER"]
    result = etherdog.plugin_style_command({"command": "status"}, timeout=5.0)
    if "error" in result:
        return jsonify({"available": False, "message": f"EtherDOG unavailable: {result['error']}"})
    return jsonify({"available": True, "message": "Discovery service is ready (EtherDOG)"})


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
    etherdog = current_app.config["ETHERDOG_MANAGER"]

    result = etherdog.plugin_style_command({"command": "list-interfaces"}, timeout=5.0)

    if "error" in result:
        return (
            jsonify(
                {
                    "status": DiscoveryStatus.ERROR.value,
                    "interfaces": [],
                    "message": result["error"],
                }
            ),
            500,
        )

    return (
        jsonify(
            {
                "status": result.get("status", "success"),
                "interfaces": result.get("interfaces", []),
                "message": result.get("message", ""),
            }
        ),
        200,
    )


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

    # Scans go straight to EtherDOG, so they work with no program loaded.
    etherdog = current_app.config["ETHERDOG_MANAGER"]

    result = etherdog.plugin_style_command(
        {"command": "scan", "params": {"interface": interface}}, timeout=10.0
    )

    if "error" in result:
        return (
            jsonify(
                {
                    "status": "error",
                    "devices": [],
                    "message": result["error"],
                    "scan_time_ms": 0,
                    "interface": interface,
                }
            ),
            500,
        )

    return (
        jsonify(
            {
                "status": result.get("status", "success"),
                "devices": result.get("devices", []),
                "message": result.get("message", ""),
                "scan_time_ms": 0,
                "interface": interface,
            }
        ),
        200,
    )


@discovery_bp.route("/ethercat/runtime-status", methods=["GET"])
@jwt_required()
def ethercat_runtime_status():
    """
    Get the current EtherCAT bus status from EtherDOG.
    ---
    tags:
      - EtherCAT
    security:
      - BearerAuth: []
    responses:
      200:
        description: Runtime status retrieved successfully
        schema:
          type: object
          properties:
            plugin_state:
              type: string
              enum:
                - IDLE
                - SCANNING
                - CONFIGURING
                - TRANSITIONING
                - OPERATIONAL
                - RECOVERING
                - ERROR
                - STOPPED
            slave_count:
              type: integer
            expected_wkc:
              type: integer
            slaves:
              type: array
              items:
                type: object
                properties:
                  position:
                    type: integer
                  name:
                    type: string
                  state:
                    type: string
                  al_status_code:
                    type: integer
                  error_count:
                    type: integer
                  has_error:
                    type: boolean
            metrics:
              type: object
              properties:
                cycle_count:
                  type: number
                wkc_error_count:
                  type: number
                avg_cycle_us:
                  type: number
                max_cycle_us:
                  type: number
                max_exchange_us:
                  type: number
                consecutive_wkc_errors:
                  type: integer
                recovery_attempts:
                  type: integer
      503:
        description: Runtime not available
    """
    etherdog = current_app.config["ETHERDOG_MANAGER"]

    result = etherdog.plugin_style_command({"command": "status"}, timeout=5.0)

    if "error" in result:
        return jsonify({"status": "error", "message": result["error"]}), 503
    return jsonify(_with_plugin_state(result)), 200


@discovery_bp.route("/ethercat/diagnostics", methods=["GET"])
@jwt_required()
def ethercat_diagnostics():
    """
    Get detailed EtherCAT diagnostic information from EtherDOG.
    ---
    tags:
      - EtherCAT
    security:
      - BearerAuth: []
    responses:
      200:
        description: Diagnostics retrieved successfully
        schema:
          type: object
          properties:
            plugin_state:
              type: string
            slave_count:
              type: integer
            slaves:
              type: array
              items:
                type: object
            timing:
              type: object
            recovery:
              type: object
            master_config:
              type: object
      503:
        description: Runtime not available
    """
    etherdog = current_app.config["ETHERDOG_MANAGER"]

    result = etherdog.plugin_style_command({"command": "diagnostics"}, timeout=5.0)

    if "error" in result:
        return jsonify({"status": "error", "message": result["error"]}), 503
    return jsonify(_with_plugin_state(result)), 200


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

    is_valid, error_msg = _validate_interface_name(interface)
    if not is_valid:
        return jsonify({"status": "error", "message": error_msg}), 400

    etherdog = current_app.config["ETHERDOG_MANAGER"]

    result = etherdog.plugin_style_command(
        {"command": "test", "params": {"interface": interface, "position": position}},
        timeout=10.0,
    )

    if "error" in result:
        return (
            jsonify(
                {
                    "status": "error",
                    "connected": False,
                    "device": None,
                    "message": result["error"],
                    "response_time_ms": 0,
                }
            ),
            500,
        )

    return (
        jsonify(
            {
                "status": result.get("status", "success"),
                "connected": result.get("connected", False),
                "device": result.get("device"),
                "message": result.get("message", ""),
                "response_time_ms": 0,
            }
        ),
        200,
    )
