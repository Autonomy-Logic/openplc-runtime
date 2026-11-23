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

The runtime image is built on the OpenPLC runtime base image, published from
[autonomy-docker](https://github.com/Autonomy-Logic/autonomy-docker#readme):

| | |
|---|---|
| Name | `ghcr.io/autonomy-logic/openplc-runtime-base` |
| Tag | `trixie-1` (Debian trixie, GCC 14, Python 3.13) |
| Digest (multi-arch index: amd64, arm64, arm/v7) | `sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b` |

The `Dockerfile` writes this reference in full, tag and digest, in the `FROM` of
each of its three stages (`venvs`, `builder` and the final stage), with no
build argument, and sets the labels `org.opencontainers.image.base.name` and
`org.opencontainers.image.base.digest` of the image to the same reference.
Changing the base means changing all of them together; `tests/image/run.sh`
fails when they disagree.

The base provides Python, the compiler toolchain uploaded programs are built
with, `curl`, `ethtool`, `openssl`, the `/var/run/runtime` directory and the
dpkg path-excludes, so the final stage installs nothing. When autonomy-docker
publishes a security bump of the base, the pin here follows it within 7 days
for a Critical vulnerability and within 30 days for a High one.

### Base image check

`scripts/verify-base-image.sh [--no-attestation] [Dockerfile]` (default:
`Dockerfile` in the working directory) prints one `error:` line per broken rule
and exits non-zero when any rule is broken. Without the network
(`--no-attestation`) it checks the `Dockerfile` rules shared with
`tests/image/static.sh` (every `FROM` pinned by digest with no variable, one
pin for all stages, no `# syntax` line, `COPY --from` and `RUN --mount` only
from stages of the file) and that the pinned package is exactly
`ghcr.io/autonomy-logic/openplc-runtime-base`, written as `<name>:<tag>@<digest>`.
With the network it also checks, with the `docker` and `gh` on `PATH`, that:

- the tag resolves to the pinned digest in the registry
  (`docker buildx imagetools inspect`);
- `gh attestation verify oci://<name>@<digest> --repo Autonomy-Logic/autonomy-docker
  --signer-workflow Autonomy-Logic/autonomy-docker/.github/workflows/build.yml
  --source-ref refs/heads/main --deny-self-hosted-runners` succeeds, and one
  verified attestation names `ghcr.io/autonomy-logic/openplc-runtime-base`
  with the pinned digest as its subject.

It runs in CI in two places, with `GH_TOKEN` set to the workflow token and no
registry login:

- `.github/workflows/base-image-check.yml`, job "Verify Base Image
  Attestation", on every pull request and on pushes to `development` and
  `main`, with read-only permissions;
- the `build` job of `.github/workflows/docker.yml`, as the first step after
  checkout, so a release build stops before QEMU, the registry login and the
  build.

Locally, `bash scripts/verify-base-image.sh` needs `docker` with buildx and an
authenticated `gh`.

### Bumping the pin

1. Take a tag of `ghcr.io/autonomy-logic/openplc-runtime-base` published by
   the autonomy-docker build workflow on `main`, and read its digest:
   `docker buildx imagetools inspect ghcr.io/autonomy-logic/openplc-runtime-base:<tag> --format '{{.Manifest.Digest}}'`.
2. Write `<name>:<tag>@<digest>` in the three `FROM` lines of the
   `Dockerfile`, the two base labels, `BASE_PIN` in `tests/image/helpers.sh`
   and the table above.
3. Run `bash scripts/verify-base-image.sh` and `bash tests/image/run.sh`; both
   must pass.
4. Open the pull request; its "Verify Base Image Attestation" check must pass.

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

The build has three stages, all on the pinned base:

- `venvs` copies in only `requirements.txt`, `pyproject.toml`, the plugin
  `requirements.txt` files, `install.sh` and `scripts/manage_plugin_venvs.sh`,
  and runs `./install.sh --native deps venvs`. A new Python plugin with a
  `requirements.txt` needs its own `COPY` line in this stage.
- `builder` copies the source and runs `./install.sh --native deps compile`
  (CMake comes from apt).
- The final stage stacks, in this order: the venvs (`/workdir/venvs`), the
  compiled output (`/workdir/build`), the application files, then the metadata.

A change to application code rebuilds only the layers above the venvs; a
change to a requirements file rebuilds the venvs layer and the layers above it.
For a reproducible venvs layer, build with `SOURCE_DATE_EPOCH` and rewritten
timestamps on a `docker-container` buildx builder:

```bash
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) docker buildx build \
  --output type=oci,dest=openplc-runtime.oci.tar,rewrite-timestamp=true .
```

`install.sh --native` takes optional step names, run in this order whatever
order they are given in: `deps` (system packages and CMake), `venvs` (the
runtime venv and one venv per Python plugin) and `compile` (the runtime core,
the native plugins and EtherDOG, then the `.installed` marker). With no step
named it runs all three and then installs the systemd service, as before.

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
dispatch. There is no pull-request trigger here. A branch push publishes only
the bootloader.

The runtime image (`build` job) builds for a `v*` tag push and for a manual run
only, for the platforms of the `platforms` input (default `linux/amd64`,
`linux/arm64`, `linux/arm/v7`). `scripts/publish-plan.sh` decides the package,
the tags and the version baked into the image (`RUNTIME_VERSION`, reported by
`/api/version`), so there are three kinds of run:

| Run | Package | Tags | Version |
|---|---|---|---|
| Release: push of a `vX.Y.Z` tag | `ghcr.io/autonomy-logic/openplc-runtime` | the tag, plus `latest` when the tag has no `-` suffix (`v4.4.0-rc.1` gets no `latest`) | the tag |
| Manual release: "Run workflow" with `release_tag` | `ghcr.io/autonomy-logic/openplc-runtime` | `release_tag` only, never `latest` | `release_tag` |
| Measurement build: "Run workflow" with `test_tag` | `ghcr.io/autonomy-logic/openplc-runtime-test` | `test_tag` only | `test_tag` |

A manual run sets exactly one of `release_tag` and `test_tag`, and the tag must
be a valid image tag other than `latest`. Every run checks, after the registry
login, that its tag does not exist yet in its package
(`docker buildx imagetools inspect`) and stops before the build when it does: a
published tag is never overwritten, a manual run never moves `latest`, and
rebuilding an older version means publishing it under a new tag. The run also
stops when the registry answer is not a clear "not found" (an authentication,
permission, rate-limit or network error), since it cannot tell then whether
the tag exists. A manual release of a version that is pushed as a git tag later
makes that tag push stop, so `latest` moves only through a tag push of a version
not yet published.

Every runtime image is built with `SOURCE_DATE_EPOCH` set to the time of the
built commit and `rewrite-timestamp=true` on the registry output, so the file
timestamps of a layer come from the commit and not from the build time, and
carries a buildx
provenance attestation (`mode=max`) and an SBOM attestation. Two measurement
builds of commits that change only application code can therefore be published
and compared layer by layer without touching the release package.

The `build` job runs `scripts/verify-base-image.sh` first (see "Base image
check"), which needs `attestations: read` and `GH_TOKEN`.

- The bootloader is versioned separately, from `bootloader/VERSION`. A version
  already in the registry is not rebuilt, so a run of runtime releases does not
  republish an unchanged bootloader; `latest` moves only for a stable version
  from a release tag or `main`.

**Tests** -- `.github/workflows/tests.yml`

Runs on every pull request: `gofmt`, `go vet` and `go test -race` for the
bootloader, `pytest tests/pytest` on Python 3.13, `shellcheck` on the installer scripts,
`tests/image/run.sh` (the static checks of the `Dockerfile`), and a check that
every caller of `install.sh` asks for the build it needs.

`bash tests/image/run.sh --build` also builds the image for the host
architecture and checks the built layers, then checks a running container of
it: TLS 1.0 and 1.1 refused and 1.2 and 1.3 accepted on port 8443, uploads
refused without a valid token and for unsafe archives, the webserver and the
Python plugins on Python 3.13, and OPC-UA offering only the configured security
profile. It runs by hand, on a buildx builder with the `docker-container` driver
named by `IMAGE_TEST_BUILDER`.

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
