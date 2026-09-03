"""Host metadata for the editor's Runtime Status header.

Its own blueprint rather than another route in ``webserver/restapi.py``: that
module is at pylint's per-module line ceiling, and host facts are a different
concern from the PLC control and user-management surface that fills it. Mounted
under ``/api`` so the route reads ``/api/device-info`` like every other
endpoint the editor calls.

Authenticated, unlike ``/api/capabilities``. The split is deliberate:
``updatePolicy`` has to be readable BEFORE login so a client can decide which
actions to offer, whereas kernel and architecture are only ever shown to
somebody already looking at a device they hold credentials for.
"""

from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required

from webserver.update_policy import device_info

runtime_info_bp = Blueprint("runtime_info", __name__, url_prefix="/api")


@runtime_info_bp.route("/device-info", methods=["GET"])
@jwt_required()
def restapi_device_info():
    """Return host facts about the device this runtime is running on.
    ---
    tags:
      - Runtime
    security:
      - BearerAuth: []
    responses:
      200:
        description: Device information retrieved
        schema:
          type: object
          properties:
            hostname:
              type: string
            architecture:
              type: string
              description: Machine architecture reported by the kernel (e.g. aarch64)
            kernel:
              type: string
              description: Kernel release string
            system:
              type: string
              description: Operating system name
            containerized:
              type: boolean
              description: Whether the runtime is running inside a container
            updatePolicy:
              type: string
              enum: [self, managed, manual, none]
              description: Which mechanism may change this runtime's version
            sidecarPort:
              type: integer
              description: Port of the managing sidecar; null unless updatePolicy is "self"
      401:
        description: Missing or invalid token
    """
    return jsonify(device_info()), 200
