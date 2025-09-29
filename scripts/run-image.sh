#!/usr/bin/env bash
# Run container mounting only source code (preserves built venv)
docker run --rm -it \
    -v $(pwd)/core:/workdir/core \
    -v $(pwd)/webserver:/workdir/webserver \
    -v $(pwd)/scripts:/workdir/scripts \
    --cap-add=sys_nice \
    --ulimit rtprio=99 \
    --ulimit memlock=-1 \
    -p 8443:8443 \
    build-env
