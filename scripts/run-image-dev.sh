#!/usr/bin/env bash
# Run container mounting only source code (preserves built venv)
# Named volume for persistent runtime data (DB, .env, etc)
docker run --rm -it \
    -v $(pwd)/venvs:/workdir/venvs \
    -v $(pwd)/webserver:/workdir/webserver \
    -v $(pwd)/tests:/workdir/tests \
    -v $(pwd)/scripts:/workdir/scripts \
    -v openplc-runtime-data:/var/run/runtime \
    --cap-add=sys_nice \
    --ulimit rtprio=99 \
    --ulimit memlock=-1 \
    -p 8443:8443 \
    openplc-dev
