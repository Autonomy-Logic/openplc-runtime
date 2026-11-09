# Docker Deployment

## Overview

OpenPLC Runtime v4 provides official Docker images for easy deployment across multiple platforms. The containerized runtime includes all dependencies and can be deployed with a single command.

## Official Image

**Registry:** GitHub Container Registry (GHCR)

**Image:** `ghcr.io/autonomy-logic/openplc-runtime:latest`

**Supported Architectures:**
- `linux/amd64` - x86_64 systems
- `linux/arm64` - ARM 64-bit (Raspberry Pi 4, etc.)
- `linux/arm/v7` - ARM 32-bit (Raspberry Pi 3, etc.)

## Base image

The runtime image is planned to build on its own base image, which holds what changes rarely: the OS, the system
packages and the toolchain.

- **Base image:** `ghcr.io/autonomy-logic/openplc-runtime-base`, public on ghcr.
- **Source:** `base/Dockerfile` and `base/VERSION` in this repository, built by a `base` job in
  `.github/workflows/docker.yml` modelled on the `bootloader` job.
- **Tag scheme and family:** `<debian>-<N>`. The target family is trixie (`trixie-<N>`); the fallback is
  bookworm (`bookworm-<N>`).
- **Upstream pin:** `base/Dockerfile` starts `FROM debian:<family>-slim@sha256:...`.
- **Contents:** the dpkg path-excludes (docs, man pages, locales, sanitizer runtimes) and the apt packages the
  final stage installs today (`python3`, `libpython3.11`, `g++`, `make`, `ccache`, `ethtool`, `openssl`,
  `ca-certificates`, `curl`).
- **Where the product pins it:** the `builder` stage and the final stage of `Dockerfile`, each with
  `FROM ghcr.io/autonomy-logic/openplc-runtime-base:<tag>@sha256:<digest>`.
- **Product layer order:** the single `COPY . .` of the `builder` stage is split by rate of change: plugin
  venvs, then the compiled core, then the webserver, scripts and sources, then the metadata.

**Status:** the base is not introduced yet. `Dockerfile` still builds both stages from the floating tag
`debian:bookworm-slim`. The concrete tag and digest are recorded here when the base is introduced.

### Policy

1. **One base per product image.** Each product image has its own base, public on ghcr as
   `ghcr.io/autonomy-logic/<product-image>-base`, holding what changes rarely: the OS, the system packages and
   the toolchains.
2. **The base lives with the product.** The base's Dockerfile, its `VERSION` file and its CI workflow live in
   the product's own repository.
3. **Everything that ships is pinned by digest.** The product starts
   `FROM ghcr.io/autonomy-logic/<product-image>-base:<tag>@sha256:<multi-arch digest>`. The base starts `FROM`
   an upstream image pinned by digest, never by a tag. A builder stage whose output ships is pinned by digest
   too.
4. **Tags are `<family>-<N>`**, read from the base's `VERSION` file. N increments on any base change or
   upstream digest refresh and resets on a family change. A tag is published only when it is absent, so a
   published tag is immutable. There is no `latest` or other floating tag, and old base versions are
   never deleted from ghcr, because a two-PR bump needs the previous one. The base workflow runs on a push to
   `development` or `main` that touches the base directory, or by manual dispatch; a product release never
   rebuilds its base.
5. **Family rule.** The base takes the newest Debian stable and the newest Python the product's dependencies
   support, chosen when the base is introduced, and the tag follows the chosen family. If the product's tests
   fail on it, the base keeps the image's current family and the reason is recorded with the change that
   introduces the base.
6. **Layer order.** Base, then lockfile-keyed dependencies, then rarely changed vendored files, then
   application code, then metadata last (`ARG VERSION`, `ENV`, `LABEL`, `HEALTHCHECK`, `CMD`). One `COPY` per
   rate of change. The final stage never runs `apt-get` or a network `pip install`.
7. **Reproducible layers.** Builds set `SOURCE_DATE_EPOCH` and use the buildx option
   `rewrite-timestamp=true`, so identical content yields identical layer digests.
8. **Bump triggers.** A base is bumped only for a needed system package or toolchain change, a fixable
   security issue, or a family change. Every bump costs one full download of the image on every device.

### Bump procedure

A bump is manual and takes two PRs:

1. **Base PR.** Change `base/Dockerfile` and/or `base/VERSION`. Once it is merged, the base workflow
   publishes the new tag and prints the product's `FROM` line, with tag and digest, in its job summary.
2. **Product pin PR.** Set that `FROM` line in both the `builder` stage and the final stage of `Dockerfile`.

A quarterly review looks at the bases; it forces no bump.

### Drift check

A CI job runs weekly and on manual dispatch and compares each pinned upstream digest with what its tag
resolves to now. When one has moved, it builds the base and the product on the new digest, runs the product
tests and reports the result in its job summary or in an issue. The drift check never bumps a pin.

## The managed install (recommended)

Everything below describes running the runtime container by hand, which is
still supported and is what the rest of this document covers. On a device you
intend to keep, prefer the managed install:

```bash
curl -fsSL https://runtime.getedge.me | sudo bash
```

That installs Docker if needed, then two containers: the runtime, and a
**bootloader** on port 8445 that starts it, watches it, and can change its
version from the OpenPLC Editor without SSH. Neither has a systemd unit --
Docker's restart policy starts the bootloader, and the bootloader starts the
runtime.

- **Runtime data** lives in `/var/lib/openplc-runtime` on the host: `.env`,
  `restapi.db`, the stored project, retained variables, VPP licences. A version
  change never touches it. A native install uses the same directory, so moving
  a device to containers carries its users and project across.
- **Bootloader state** lives in `/var/lib/openplc-bootloader`: the container
  spec, including this board's device mounts. Separate on purpose, so "erase
  all runtime data" cannot leave a board without its SPI mount.
- **A systemd OpenPLC already on the device** (`openplc.service` from v3,
  `openplc-runtime.service` from a source install) is stopped and disabled
  first -- both bind 8443. What was displaced is recorded so it can be put
  back.

To remove it:

```bash
curl -fsSL https://runtime.getedge.me | sudo bash -s -- --uninstall --yes
```

Containers and images go, any displaced systemd runtime comes back, and
`/var/lib/openplc-runtime` is kept unless `--purge` is given -- a native
install shares that directory, so deleting it is not assumed to be safe.
Docker itself is left installed.

`sudo ./install.sh --native` from a checkout still builds from source, for
MSYS2 and for targets that cannot run containers.

## Quick Start

### Pull and Run

```bash
docker pull ghcr.io/autonomy-logic/openplc-runtime:latest

docker run -d \
  --name openplc-runtime \
  -p 8443:8443 \
  --cap-add=SYS_NICE \
  --cap-add=SYS_RESOURCE \
  -v openplc-runtime-data:/var/run/runtime \
  ghcr.io/autonomy-logic/openplc-runtime:latest
```

**Note:** The runtime will listen on port 8443 for connections from the OpenPLC Editor. Do not open https://localhost:8443 in a browser - there is no web interface. Connect to it from the OpenPLC Editor desktop application.

### Stop and Remove

```bash
docker stop openplc-runtime
docker rm openplc-runtime
```

## Volume Management

### Persistent Data

The runtime stores important data in `/var/run/runtime/`:

**Contents:**
- `.env` - Environment variables (JWT secret, database URI, pepper)
- `restapi.db` - SQLite database with user accounts
- Socket files (created at runtime, ephemeral)

**Volume Mount:**
```bash
-v openplc-runtime-data:/var/run/runtime
```

### Named Volume (Recommended)

Using a named volume provides persistence and easy management:

```bash
# Create named volume
docker volume create openplc-runtime-data

# Run with named volume
docker run -d \
  --name openplc-runtime \
  -p 8443:8443 \
  -v openplc-runtime-data:/var/run/runtime \
  ghcr.io/autonomy-logic/openplc-runtime:latest
```

**Benefits:**
- Data persists across container restarts
- Easy backup and restore
- Docker manages volume location

### Bind Mount (Alternative)

For direct access to runtime data:

```bash
mkdir -p /path/to/runtime-data

docker run -d \
  --name openplc-runtime \
  -p 8443:8443 \
  -v /path/to/runtime-data:/var/run/runtime \
  ghcr.io/autonomy-logic/openplc-runtime:latest
```

**Benefits:**
- Direct file system access
- Easy inspection and backup
- Can use existing directory

**Considerations:**
- Requires proper permissions
- Host path must exist
- Less portable across systems

## Port Mapping

### Default Port

The runtime exposes port 8443 for REST API access from the OpenPLC Editor:

```bash
-p 8443:8443
```

### Custom Port

To use a different host port:

```bash
-p 9443:8443  # Editor connects to https://localhost:9443
```

### Localhost Only

To restrict access to localhost:

```bash
-p 127.0.0.1:8443:8443
```

### Multiple Interfaces

To bind to specific interface:

```bash
-p 192.168.1.100:8443:8443
```

## Environment Variables

### Database URI

Override the default database location:

```bash
docker run -d \
  --name openplc-runtime \
  -p 8443:8443 \
  -e SQLALCHEMY_DATABASE_URI=sqlite:////var/run/runtime/custom.db \
  -v openplc-runtime-data:/var/run/runtime \
  ghcr.io/autonomy-logic/openplc-runtime:latest
```

### JWT Secret

Provide a custom JWT secret (not recommended, auto-generated is secure):

```bash
-e JWT_SECRET_KEY=your_64_character_hex_string_here
```

### Pepper

Provide a custom pepper for password hashing:

```bash
-e PEPPER=your_64_character_hex_string_here
```

## Real-Time Scheduling

For real-time performance, the container may need privileged mode:

```bash
docker run -d \
  --name openplc-runtime \
  --privileged \
  -p 8443:8443 \
  -v openplc-runtime-data:/var/run/runtime \
  ghcr.io/autonomy-logic/openplc-runtime:latest
```

**Alternative (More Secure):**

Grant specific capabilities instead of full privileged mode:

```bash
docker run -d \
  --name openplc-runtime \
  --cap-add=SYS_NICE \
  -p 8443:8443 \
  -v openplc-runtime-data:/var/run/runtime \
  ghcr.io/autonomy-logic/openplc-runtime:latest
```

**Capabilities:**
- `SYS_NICE` - Required for SCHED_FIFO real-time scheduling

## Docker Compose

### Basic Configuration

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  openplc-runtime:
    image: ghcr.io/autonomy-logic/openplc-runtime:latest
    container_name: openplc-runtime
    ports:
      - "8443:8443"
    volumes:
      - openplc-runtime-data:/var/run/runtime
    restart: unless-stopped

volumes:
  openplc-runtime-data:
```

**Start:**
```bash
docker-compose up -d
```

**Stop:**
```bash
docker-compose down
```

### Advanced Configuration

```yaml
version: '3.8'

services:
  openplc-runtime:
    image: ghcr.io/autonomy-logic/openplc-runtime:latest
    container_name: openplc-runtime
    cap_add:
      - SYS_NICE
    ports:
      - "127.0.0.1:8443:8443"
    volumes:
      - openplc-runtime-data:/var/run/runtime
      - ./plugins.conf:/workdir/plugins.conf:ro
    environment:
      - TZ=America/New_York
    restart: unless-stopped
    healthcheck:
      # /api/version, not /api/ping: ping is behind @jwt_required(), so a
      # healthcheck against it always gets a 401 and `curl -f` always fails.
      test: ["CMD", "curl", "-k", "-f", "https://localhost:8443/api/version"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 90s

volumes:
  openplc-runtime-data:
```

**Features:**
- Localhost-only access
- Real-time scheduling capability
- Custom plugin configuration
- Timezone setting
- Health check monitoring
- Auto-restart on failure

## Building Custom Images

### From Source

Clone the repository and build:

```bash
git clone https://github.com/Autonomy-Logic/openplc-runtime.git
cd openplc-runtime
git checkout development

docker build -t my-openplc-runtime .
```

### Using Build Script

```bash
bash scripts/build-docker-image.sh
```

### Multi-Architecture Build

For building images that support multiple architectures:

```bash
docker buildx create --use
docker buildx build \
  --platform linux/amd64,linux/arm64,linux/arm/v7 \
  -t my-openplc-runtime:latest \
  --push \
  .
```

## Development Container

For development and testing, use the development image:

```bash
bash scripts/build-docker-image-dev.sh
bash scripts/run-image-dev.sh
```

**Features:**
- Includes test dependencies
- Development tools installed
- Source code mounted as volume
- Interactive shell access

## Networking

### Bridge Network (Default)

Containers use Docker's default bridge network:

```bash
docker run -d \
  --name openplc-runtime \
  -p 8443:8443 \
  ghcr.io/autonomy-logic/openplc-runtime:latest
```

### Host Network

For direct host network access (Linux only):

```bash
docker run -d \
  --name openplc-runtime \
  --network host \
  ghcr.io/autonomy-logic/openplc-runtime:latest
```

**Note:** Port mapping is not needed with host network.

### Custom Network

Create and use a custom network:

```bash
docker network create openplc-net

docker run -d \
  --name openplc-runtime \
  --network openplc-net \
  -p 8443:8443 \
  ghcr.io/autonomy-logic/openplc-runtime:latest
```

## Logging

### View Logs

```bash
docker logs openplc-runtime
```

### Follow Logs

```bash
docker logs -f openplc-runtime
```

### Limit Log Output

```bash
docker logs --tail 100 openplc-runtime
```

### Log Drivers

Configure log driver in docker-compose.yml:

```yaml
services:
  openplc-runtime:
    image: ghcr.io/autonomy-logic/openplc-runtime:latest
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

## Backup and Restore

### Backup Volume

```bash
# Create backup
docker run --rm \
  -v openplc-runtime-data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/openplc-backup.tar.gz -C /data .
```

### Restore Volume

```bash
# Restore backup
docker run --rm \
  -v openplc-runtime-data:/data \
  -v $(pwd):/backup \
  alpine tar xzf /backup/openplc-backup.tar.gz -C /data
```

### Export Container

```bash
docker export openplc-runtime > openplc-container.tar
```

### Import Container

```bash
docker import openplc-container.tar my-openplc-runtime:backup
```

## Security Considerations

### Container Isolation

**Benefits:**
- Process isolation from host
- File system isolation
- Network isolation (unless host network used)

**Limitations:**
- Privileged mode reduces isolation
- Volume mounts expose host directories
- Port exposure creates network access

### Image Verification

Verify image authenticity:

```bash
docker pull ghcr.io/autonomy-logic/openplc-runtime:latest
docker inspect ghcr.io/autonomy-logic/openplc-runtime:latest
```

### Secrets Management

**Do Not:**
- Hard-code secrets in docker-compose.yml
- Commit .env files to version control
- Use default passwords in production

**Do:**
- Use Docker secrets (Swarm mode)
- Use environment files (.env) with proper permissions
- Rotate secrets regularly

### Network Security

**Recommendations:**
- Use firewall rules to restrict access
- Bind to localhost for local-only access
- Use reverse proxy for additional security
- Enable TLS certificate validation

## Troubleshooting

### Container Won't Start

**Check logs:**
```bash
docker logs openplc-runtime
```

**Common issues:**
- Port already in use
- Volume permission errors
- Insufficient resources

### Cannot Connect from OpenPLC Editor

**Verify container is running:**
```bash
docker ps | grep openplc-runtime
```

**Check port mapping:**
```bash
docker port openplc-runtime
```

**Test connectivity:**
```bash
curl -k https://localhost:8443/api/version
```

`/api/version` is unauthenticated; `/api/ping` requires a token and will
answer `401` even on a perfectly healthy runtime.

### Real-Time Performance Issues

**Solutions:**
- Use `--privileged` or `--cap-add=SYS_NICE`
- Increase container resources
- Use host network mode
- Check host system load

### Volume Permission Errors

**Fix permissions:**
```bash
docker run --rm \
  -v openplc-runtime-data:/data \
  alpine chown -R 1000:1000 /data
```

### Out of Disk Space

**Clean up:**
```bash
docker system prune -a
docker volume prune
```

## Performance Optimization

### Resource Limits

Limit container resources:

```yaml
services:
  openplc-runtime:
    image: ghcr.io/autonomy-logic/openplc-runtime:latest
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 1G
        reservations:
          cpus: '1'
          memory: 512M
```

### CPU Pinning

Pin container to specific CPU cores:

```bash
docker run -d \
  --name openplc-runtime \
  --cpuset-cpus="0,1" \
  -p 8443:8443 \
  ghcr.io/autonomy-logic/openplc-runtime:latest
```

### Memory Optimization

```bash
docker run -d \
  --name openplc-runtime \
  --memory="1g" \
  --memory-swap="2g" \
  -p 8443:8443 \
  ghcr.io/autonomy-logic/openplc-runtime:latest
```

## CI/CD Integration

### GitHub Actions

**Images** -- `.github/workflows/docker.yml`

Triggers: a `v*` release tag, a push to `development` or `main`, or a manual
dispatch. There is no pull-request trigger here: a branch push produces no
semver tag for the metadata step.

- The runtime image builds for release tags and manual runs only, multi-arch,
  tagged `vX.Y.Z` and (for a stable release) `latest`.
- The bootloader is versioned separately, from `bootloader/VERSION`. A version
  already in the registry is not rebuilt, so a run of runtime releases does not
  republish an unchanged bootloader; `latest` moves only for a stable version
  from a release tag or `main`.

**Tests** -- `.github/workflows/tests.yml`

Runs on every pull request: `gofmt`, `go vet` and `go test -race` for the
bootloader, `pytest tests/pytest`, `shellcheck` on the installer scripts, and a
check that every caller of `install.sh` asks for the build it needs.

Not in CI: `tests/integration/harness.sh` (it needs a privileged
Docker-in-Docker host) and anything hardware-dependent. Both are run by hand.

### Using in CI/CD

Example GitHub Actions workflow:

```yaml
name: Test with OpenPLC Runtime

on: [push]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      openplc:
        image: ghcr.io/autonomy-logic/openplc-runtime:latest
        ports:
          - 8443:8443
        volumes:
          - openplc-data:/var/run/runtime
    steps:
      - uses: actions/checkout@v2
      - name: Test API
        run: |
          curl -k https://localhost:8443/api/version
```

## Related Documentation

- [Editor Integration](EDITOR_INTEGRATION.md) - How OpenPLC Editor connects to runtime
- [Architecture](ARCHITECTURE.md) - System overview
- [Security](SECURITY.md) - Security considerations
- [Troubleshooting](TROUBLESHOOTING.md) - Common issues
- [API Reference](API.md) - REST endpoints
