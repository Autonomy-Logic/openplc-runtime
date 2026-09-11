"""Test-wide bootstrap for modules that reach ``webserver.config``.

``webserver.config`` has import-time side effects: it resolves a persistent
data dir, creates it, writes a ``.env`` and reads secrets out of it. The
platform defaults are absolute paths (``/run/runtime``, ``/var/lib/...``) that
are read-only or absent on a developer machine, so importing anything that
transitively pulls in ``webserver.config`` fails at collection time unless the
overrides are in place first.

Pointing them at a throwaway temp dir here -- in the ROOT conftest, which pytest
imports before any test module -- means a test module does not have to know
whether its import graph happens to reach config. It reaches it more often than
it looks: the discovery responder, for one, reports the stored project name, so
it now depends on the snapshot store, which depends on config.

``setdefault`` throughout, so a suite that wants its own directories (see
``restapi/conftest.py``) still gets them.
"""

import os
import secrets
import tempfile

_TMP = tempfile.mkdtemp(prefix="openplc-tests-")
os.environ.setdefault("OPENPLC_RUNTIME_DIR", os.path.join(_TMP, "run"))
os.environ.setdefault("OPENPLC_PERSISTENT_DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("SQLALCHEMY_DATABASE_URI", f"sqlite:///{os.path.join(_TMP, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", secrets.token_hex(32))
os.environ.setdefault("PEPPER", secrets.token_hex(32))
os.environ.setdefault("FLASK_ENV", "development")
