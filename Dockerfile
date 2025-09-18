# Dockerfile
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    python3 python3-venv python3-pip bash \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workdir
COPY . .
RUN mkdir -p /var/run/runtime
RUN chmod +x install.sh scripts/* build/*
RUN ./install.sh docker

EXPOSE 8443

CMD ["bash", "-c", "./build/plc_main & .venv/bin/python3 webserver/app.py"]
