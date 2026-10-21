# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Flask Blueprint for Discovery REST endpoints.

Endpoints:
    GET  /api/discovery/interfaces              - List network interfaces (common)
    GET  /api/discovery/ethercat/status         - Check if EtherCAT discovery service is available
    GET  /api/discovery/ethercat/capabilities   - EtherDOG features captured on hello
    POST /api/discovery/ethercat/scan           - Scan network for EtherCAT slaves (via EtherDOG)
    POST /api/discovery/ethercat/scan-modules   - Scan a modular coupler's 0xF050 module list
    GET  /api/discovery/ethercat/runtime-status - Bus status from EtherDOG
    GET  /api/discovery/ethercat/diagnostics    - Master-wide diagnostics from EtherDOG
    POST /api/discovery/ethercat/diagnostics    - Per-slave coupler + module diagnostics
    POST /api/discovery/ethercat/validate       - Validate EtherCAT configuration
    POST /api/discovery/ethercat/test           - Test connection to specific EtherCAT slave
"""

import json
import math

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


SLAVE_POSITION_MIN = 1
SLAVE_POSITION_MAX = 65535
TIMEOUT_MS_MIN = 100
TIMEOUT_MS_MAX = 60000
TIMEOUT_MS_DEFAULT = 5000


def _slave_request(data: dict[str, object]) -> tuple[str, int] | str:
    """The (bus_name, slave_position) of a per-slave request, or the reason it is invalid."""
    bus_name = data.get("bus_name")
    slave_pos = data.get("slave_position")
    if not isinstance(bus_name, str) or not bus_name:
        return "Missing or invalid 'bus_name'"
    if (
        not isinstance(slave_pos, int)
        or isinstance(slave_pos, bool)
        or not SLAVE_POSITION_MIN <= slave_pos <= SLAVE_POSITION_MAX
    ):
        return (
            f"'slave_position' must be an integer from {SLAVE_POSITION_MIN} to {SLAVE_POSITION_MAX}"
        )
    return bus_name, slave_pos


def _timeout_ms(data: dict[str, object]) -> int:
    """The request's `timeout_ms`, clamped to TIMEOUT_MS_MIN..TIMEOUT_MS_MAX."""
    value = data.get("timeout_ms")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return TIMEOUT_MS_DEFAULT
    return int(min(max(value, TIMEOUT_MS_MIN), TIMEOUT_MS_MAX))


def _is_uint(value: object, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum


def _bad_request(message: str):
    return jsonify({"status": "error", "message": message}), 400


def _master_index(etherdog, bus_name: str) -> int | tuple[str, int]:
    """EtherDOG's index of the master named ``bus_name``, or (message, HTTP status)."""
    status = etherdog.plugin_style_command({"command": "status"}, timeout=5.0)
    if "error" in status:
        return f"EtherDOG status unavailable: {status['error']}", 502
    masters = status.get("masters")
    for index, master in enumerate(masters if isinstance(masters, list) else []):
        if isinstance(master, dict) and master.get("name") == bus_name:
            return index
    return f"Bus '{bus_name}' is not in the loaded EtherCAT configuration", 404


def _slave_error(bus_name: str, slave_pos: int, message: str, code: int, **extra: object):
    body: dict[str, object] = {
        "status": "error",
        "bus_name": bus_name,
        "slave_position": slave_pos,
        "message": message,
    }
    body.update(extra)
    return jsonify(body), code


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


@discovery_bp.route("/ethercat/capabilities", methods=["GET"])
@jwt_required()
def ethercat_capabilities():
    """
    Return the features and version from the running EtherDOG's hello.

    A `status` roundtrip is sent first, so the hello is the current process's.
    ---
    tags:
      - EtherCAT
    security:
      - BearerAuth: []
    responses:
      200:
        description: Feature set retrieved
        schema:
          type: object
          properties:
            available:
              type: boolean
            features:
              type: array
              items:
                type: string
              example: ["ethercat.dynamic_alloc", "ethercat.scan_modules"]
            version:
              type: string
              example: 1.0.0
            message:
              type: string
    """
    etherdog = current_app.config["ETHERDOG_MANAGER"]
    probe = etherdog.plugin_style_command({"command": "status"}, timeout=5.0)
    if "error" in probe:
        return (
            jsonify(
                {
                    "available": False,
                    "features": [],
                    "version": None,
                    "message": probe["error"],
                }
            ),
            200,
        )
    features, version = etherdog.get_last_hello()
    return (
        jsonify(
            {
                "available": True,
                "features": features if features is not None else [],
                "version": version,
                "message": "ok",
            }
        ),
        200,
    )


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


@discovery_bp.route("/ethercat/scan-modules", methods=["POST"])
@jwt_required()
def ethercat_scan_modules():
    """
    Scan a modular coupler's 0xF050 module ident list.

    EtherDOG speaks `scan-modules` in terms of (master_index, slave_position); the master
    index is the position of the master named `bus_name` in EtherDOG's `status` reply. A bus
    that is not loaded yet (a project not uploaded) is scanned through its `interface`.

    EtherDOG returns `{"status":"success","idents":["0x00001A0F","0x00001A10"]}`, one ident per
    slot in slot order; the reply lists them as `modules: [{slot, ident}, ...]` with slots from 1.
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
            - bus_name
            - slave_position
          properties:
            bus_name:
              type: string
              example: ethercat0
            slave_position:
              type: integer
              example: 1
            interface:
              type: string
              example: eth0
              description: The bus's network interface, used when bus_name is not loaded
            timeout_ms:
              type: integer
              default: 5000
              minimum: 100
              maximum: 60000
    responses:
      200:
        description: Module list retrieved, one entry per occupied slot, slots from 1
      400:
        description: Invalid request parameters
      404:
        description: No master named bus_name in the loaded EtherCAT configuration and no interface
      502:
        description: EtherDOG refused the scan or returned an unknown reply
    """
    data = request.get_json(silent=True)
    parsed = _slave_request(data if isinstance(data, dict) else {})
    if isinstance(parsed, str):
        return _bad_request(parsed)
    bus_name, slave_pos = parsed
    etherdog_timeout = _timeout_ms(data) / 1000.0 + 2.0

    interface = data.get("interface")
    if interface is not None:
        if not isinstance(interface, str):
            return _bad_request("'interface' must be a string")
        is_valid, error_msg = _validate_interface_name(interface)
        if not is_valid:
            return _bad_request(error_msg)

    etherdog = current_app.config["ETHERDOG_MANAGER"]
    master = _master_index(etherdog, bus_name)
    if isinstance(master, tuple):
        if master[1] != 404 or interface is None:
            return _slave_error(bus_name, slave_pos, master[0], master[1], modules=[])
        params: dict[str, object] = {"interface": interface, "slave": slave_pos}
    else:
        params = {"master": master, "slave": slave_pos}
    result = etherdog.plugin_style_command(
        {"command": "scan-modules", "params": params},
        timeout=etherdog_timeout,
    )

    if "error" in result:
        return _slave_error(bus_name, slave_pos, result["error"], 502, modules=[])

    if result.get("status") != "success":
        return _slave_error(
            bus_name,
            slave_pos,
            result.get("message", "scan-modules did not succeed"),
            502,
            modules=[],
        )

    idents = result.get("idents") or []
    modules = [
        {"slot": slot, "ident": ident}
        for slot, ident in enumerate(idents, start=1)
        if isinstance(ident, str)
    ]

    return (
        jsonify(
            {
                "status": "success",
                "bus_name": bus_name,
                "slave_position": slave_pos,
                "modules": modules,
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


@discovery_bp.route("/ethercat/diagnostics", methods=["POST"])
@jwt_required()
def ethercat_slave_diagnostics():
    """
    Per-slave diagnostics for a modular coupler.

    Returns the coupler's ETG.5001 device-state word and the per-slot module
    status bytes. Forwards to EtherDOG's `diagnostics-slave` command, which
    performs the SDO reads against the live bus.
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
            - bus_name
            - slave_position
          properties:
            bus_name:
              type: string
              example: ethercat0
            slave_position:
              type: integer
              example: 1
    responses:
      200:
        description: >
          Per-slave diagnostics retrieved. `coupler.status_word` is a 16-bit integer, or null
          when EtherDOG could not read it; each module has a 1-based `slot` and a `status_byte`
          (0..255, or null when unread).
      400:
        description: Invalid request parameters
      404:
        description: No master named bus_name in the loaded EtherCAT configuration
      502:
        description: EtherDOG refused the diagnostics read or returned an unknown reply
    """
    data = request.get_json(silent=True)
    parsed = _slave_request(data if isinstance(data, dict) else {})
    if isinstance(parsed, str):
        return _bad_request(parsed)
    bus_name, slave_pos = parsed

    etherdog = current_app.config["ETHERDOG_MANAGER"]
    master = _master_index(etherdog, bus_name)
    if isinstance(master, tuple):
        return _slave_error(bus_name, slave_pos, master[0], master[1])
    result = etherdog.plugin_style_command(
        {"command": "diagnostics-slave", "params": {"master": master, "slave": slave_pos}},
        timeout=10.0,
    )

    if "error" in result:
        return _slave_error(bus_name, slave_pos, result["error"], 502)

    if result.get("status") != "success":
        return _slave_error(
            bus_name, slave_pos, result.get("message", "diagnostics-slave did not succeed"), 502
        )

    coupler_raw = result.get("coupler")
    coupler_raw = coupler_raw if isinstance(coupler_raw, dict) else {}
    status_word = coupler_raw.get("status_word")
    if status_word is not None and not _is_uint(status_word, 0xFFFF):
        return _slave_error(
            bus_name, slave_pos, "EtherDOG returned an invalid coupler status word", 502
        )
    poll_at_ms = coupler_raw.get("poll_at_ms")
    modules_raw = result.get("modules")
    modules = [
        {"slot": m["slot"], "status_byte": m.get("status_byte")}
        for m in (modules_raw if isinstance(modules_raw, list) else [])
        if isinstance(m, dict)
        and _is_uint(m.get("slot"), SLAVE_POSITION_MAX)
        and m["slot"] >= 1
        and (m.get("status_byte") is None or _is_uint(m.get("status_byte"), 0xFF))
    ]

    response = {
        "status": "success",
        "bus_name": bus_name,
        "slave_position": slave_pos,
        "coupler": {"status_word": status_word},
        "modules": modules,
    }
    if isinstance(poll_at_ms, (int, float)):
        response["coupler"]["poll_at_ms"] = poll_at_ms
    return jsonify(response), 200


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
