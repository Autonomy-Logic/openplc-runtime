# Retrieve Project from PLC

A device keeps a copy of the source project it is running, so the project can be
opened again from a machine that does not have it. Upload a program from the
OpenPLC Editor or from openplc-web, and the source project travels with it;
later, anyone with an administrator account on that device can pull it back.

This answers the everyday case where the device is the only remaining record: an
engineer left, a laptop died, a panel has been running for two years and nobody
is sure which version of the project is in it.

## Read this first: the stored project is not encrypted

**The project is stored as a plain ZIP file on the device's filesystem. Anyone
who can read that filesystem can read the project** -- over SSH, with physical
access to the storage, from a backup image, or from a mounted volume in a
container deployment.

The access control on this feature is a single role check on the retrieval
endpoint: the HTTP API will not hand the archive to a non-administrator. That is
the whole of it. It protects the network path and nothing else.

There is also **no integrity guarantee.** Nothing signs or checksums the stored
project, so a project replaced on disk by someone with filesystem access is
retrieved as if it were the original, and neither the device nor the client can
tell. Do not describe this feature as verified, trusted, or tamper-proof,
because it is none of those things.

One more thing worth knowing: the **project name and its timestamp are
advertised in the unauthenticated UDP discovery reply**, which is how a client
can show what a device holds before anyone signs in. The name of a project is
therefore readable by anything on the same network. The archive is not.

If a deployment needs the project protected at rest, the answer is full-disk or
filesystem-level encryption on the device. This feature does not provide it and
is not a substitute for it.

## What is stored

The complete source project as the editor holds it -- POUs, data types, global
variable lists, device and pin configuration, the project manifest -- plus a
copy of every library the project references.

The `build/` directory is not included. It is generated output, it is the
largest part of a project, and it is reproduced by compiling.

The archive is capped at 100 MB (`MAX_SNAPSHOT_BYTES` in
`webserver/project_snapshot.py`). An upload carrying a larger one is accepted as
a program upload, but the project is not stored, and the response says so.

It lives beside the runtime's other persistent state, in `project_snapshot/`
under the persistent data directory:

| Deployment | Path |
| --- | --- |
| Native Linux | `/var/lib/openplc-runtime/project_snapshot/` |
| Container | `/var/run/runtime/project_snapshot/` |
| Overridden | `$OPENPLC_PERSISTENT_DATA_DIR/project_snapshot/` |

## How it stays current

The stored project always describes the program the device is actually running,
which takes three rules working together.

**Every upload replaces it.** When an upload reaches the point where the
existing program is being overwritten, the stored project is erased. The new one
is written only after the new program is safely in place.

**An upload that carries no project erases the stored one.** This is deliberate,
not an oversight. `openplc-cli`, older editors, and third-party clients upload
programs without a source project; if the previous one survived, the device would
advertise a project it is no longer running, which is worse than advertising
nothing.

**A failed compile leaves nothing behind.** The new project is held aside while
the build runs and becomes the stored one only if the build succeeds. Any other
outcome discards it. This matches what happens to the program itself: a failed
build leaves the device with no program at all, so leaving a stored project
would describe something that is not there.

The upshot is that a device never advertises a project it is not running. It may
advertise none -- that is the honest answer after a CLI upload or a failed build.

## Retrieving

The flow is the same on both clients: **File -> Retrieve Project from PLC**.

1. **Pick a device.** The desktop editor scans the local network by UDP
   broadcast; openplc-web asks each orchestrator's agent to run the same scan
   against the runtimes it manages, so both read the same source of truth -- the
   runtime's own advertisement. Each row shows the stored project's name and
   timestamp. A device storing nothing is shown as such and cannot be chosen.

2. **Give up the current connection, if it costs one.** The client holds a
   session for one device at a time, so retrieving from a different device ends
   the session you have. When that applies, you are told before it happens.
   Picking the device you are already signed in to reuses that session and skips
   straight to the retrieval.

3. **Sign in as an administrator.** Anything on the network can see *that* a
   project is stored and what it is called, because the discovery reply is
   unauthenticated; only an administrator can retrieve the archive itself. A
   non-administrator who performed the upload still cannot retrieve it.

4. **The project opens.** It is unpacked into a scratch directory and opened as
   an ordinary project.

5. **Libraries are reconciled.** Each library the project carries is compared
   against the ones on this machine by content hash, not by version number --
   two different builds can carry the same version label. Libraries that are
   missing, or present but different from what the project was built against,
   are offered for installation. A library that is present and identical needs
   nothing.

### A retrieved project has no home yet

It was unpacked into a scratch directory, not into a location anyone chose, so
**Save is refused and points at Save As.** Saving in place would report success
for work that is not where the user thinks it is, and on the desktop it would
sit in a temporary directory. Use **File -> Save As** to give it a home; from
then on it saves normally.

Compiling a retrieved project works before saving it: the build's own internal
flush is exempt from that refusal, because refusing it would not protect
anything -- it would only stop the project from compiling.

Save As is not yet implemented in openplc-web, where projects live in the user's
account rather than on disk (tracked as RTOP-280). Until it is, retrieve on the
web opens the project for inspection and compilation; keeping a copy means
opening it in the desktop editor.

## HTTP API

The retrieval endpoint requires authentication. See [API.md](API.md) for the
authentication flow.

What a device is storing is published two ways: its name and timestamp in the
unauthenticated discovery reply, which is what fills a client's device picker,
and the archive itself here. There is deliberately no authenticated
"describe it without fetching it" endpoint -- one existed briefly and had no
caller, because a picker has to describe a device *before* anyone signs in to
it, which an authenticated endpoint cannot do.

### `GET /api/project-snapshot`

The archive itself. **Administrators only** -- a non-administrator gets `403`.
`404` when nothing is stored.

```json
{
  "projectName": "Traffic Light",
  "formatVersion": 1,
  "filename": "project.zip",
  "contentBase64": "UEsDBBQ..."
}
```

The archive is base64 inside JSON rather than a binary body because openplc-web
reaches devices through the orchestrator agent's HTTP proxy, which decodes
responses as JSON or falls back to text; a binary body does not survive that
trip.

### Discovery reply

The UDP discovery reply on port 33333 carries two extra fields when a project is
stored:

```json
{
  "service": "openplc-runtime",
  "project_name": "Traffic Light",
  "project_timestamp": "2026-08-31T12:00:00Z"
}
```

Both keys are **absent**, not empty, when nothing is stored -- which is what lets
a client distinguish "nothing stored" from "stored but unnamed" without a
separate flag. This reply is not authenticated.

### Uploading with a project

`POST /api/upload-file` takes the archive as an additional multipart part
alongside the program. Clients that do not send one keep working unchanged; see
"How it stays current" for what that means for anything already stored.

The `projectSnapshot: true` capability flag tells a client the device supports
this at all.

## When a device has no administrator

Retrieval needs an administrator, so a device whose accounts contain none cannot
be retrieved from -- and, before this feature, nobody noticed, because nothing
else required the role.

The runtime repairs the clear-cut case at startup: **if there is exactly one
account and it is not an administrator, it is promoted.** A sole user is already
the whole of that device's administration, and the alternative is a device
nobody can retrieve from.

With several non-administrator accounts, nothing is promoted. Choosing one of
them would be inventing an authority the deployment never granted, and there is
no basis for picking. Recovering that device means restoring an administrator
account by other means.

## Related

- [API.md](API.md) -- the REST API and its authentication
- [SECURITY.md](SECURITY.md) -- authentication, TLS, and file validation
- [EDITOR_INTEGRATION.md](EDITOR_INTEGRATION.md) -- how the editor talks to the runtime
