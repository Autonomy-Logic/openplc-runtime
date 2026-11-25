# Security envelope end-to-end check

`e2e_security.py` runs a runtime image in Docker with its data directory on a volume and drives
the REST API over HTTPS: the four roles on each endpoint, the refused role name `user`, token
renewal and reuse, signing out of one session, account-wide session ends (role change, deletion)
and sign-in throttling. It restarts the container wherever a rule must hold across a restart.

```bash
docker build -t openplc-runtime:p8-e2e .
python3 tests/security/e2e_security.py openplc-runtime:p8-e2e
# Also check the upgrade of a device that has an account with the old role user:
python3 tests/security/e2e_security.py openplc-runtime:p8-e2e --upgrade-from <older runtime image>
```

It uses host port 18443, the container `openplc-p8-e2e` and the volume `openplc-p8-e2e-data`, and
removes both when it ends. Needs only Python 3.10+ and Docker.
