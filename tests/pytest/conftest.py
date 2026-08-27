"""Suite-wide test setup.

Point the runtime's persistent and ephemeral directories at a temp location
BEFORE any test imports ``webserver.config`` (directly, or transitively via
``webserver.vpp_license_debug`` / ``webserver.plcapp_management``). ``config``
creates ``PERSISTENT_DATA_DIR`` / ``RUNTIME_DIR`` at import time; without this
override the import would try to create ``/var/lib/openplc-runtime`` on a CI box
and the whole module would be skipped instead of tested.

``setdefault`` so a more specific conftest (e.g. restapi) can still choose its
own paths.
"""
import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "openplc-runtime-tests")
os.environ.setdefault("OPENPLC_RUNTIME_DIR", os.path.join(_TMP, "run"))
os.environ.setdefault("OPENPLC_PERSISTENT_DATA_DIR", os.path.join(_TMP, "data"))
