"""Keep the destructive integration suite out of a bare ``pytest`` run.

``pytest.ini`` sets ``testpaths = tests`` and ``python_files = test_*.py``, so
``pytest`` from the repository root used to collect ``test_bootloader.py``.
Every case there starts by wiping ``/var/lib/openplc-runtime`` and force-
removing the runtime container. Run as root on a device -- or in the dev
container -- that destroys users, credentials, the stored project, retained
variables and any VPP licences, and stops a running PLC. Nothing about the
command typed suggests that.

The suite is meant to run only through ``tests/integration/harness.sh``, which
builds a disposable Docker-in-Docker host for it.
"""

collect_ignore = ["test_bootloader.py"]
