# Azure Elastic SAN Connect Script -- FreeBSD (PoC)

Standalone, production-quality **proof of concept** script that connects one
or more Azure Elastic SAN volumes to a FreeBSD 13+ host with **one iSCSI
session per volume**, using FreeBSD's native iSCSI initiator
(`iscsid`/`iscsictl`/`iscsi.conf`, `sysrc`/`service`).

Stock FreeBSD rejects attempts to create duplicate sessions for the same target
name and portal. This PoC therefore requires `--num-of-sessions 1` and does not
silently reduce larger values. NetApp-specific initiator/target behavior that
may permit multiple sessions needs separate validation and is outside this PoC.

This is a **separate implementation** from
[`CLI (Linux) Multi-Session Connect Scripts`](../CLI%20(Linux)%20Multi-Session%20Connect%20Scripts/connect_for_documentation.py).
FreeBSD's iSCSI stack (`iscsid`/`iscsictl`, `/etc/iscsi.conf`, `sysrc`/`service`)
is fundamentally different from Linux's (`open-iscsi`/`iscsiadm`, distro
package managers), so the two scripts intentionally do not share a code path.
The zonal-affinity resolution logic (IMDS lookup, subscription/region checks,
ARM Locations mapping, IQN decoration) is kept semantically identical between
the two scripts.

## Prerequisites

* **FreeBSD 13 or later** with the native iSCSI initiator available:
  `/dev/iscsi` must exist (the `iscsi_initiator` kernel driver is built into
  the `GENERIC` kernel; if you run a custom kernel, add
  `device iscsi` / `device iscsi_initiator`, or load it at boot with
  `iscsi_load="YES"` in `/boot/loader.conf`, or `kldload iscsi`).
* **Python 3** (the script uses only the standard library).
* **Azure CLI (`az`) with the `elastic-san` extension.** On FreeBSD, Azure CLI
  is provided by the FreeBSD Ports Collection / community packages (e.g.
  [`sysutils/py-azure-cli`](https://www.freshports.org/sysutils/py-azure-cli/)),
  **not** by Microsoft's first-class supported install matrix
  (<https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux>
  covers Linux distributions only). The script checks that `az` is on `PATH`
  and that the `elastic-san` extension is installed
  (`az extension add -n elastic-san`), but it cannot verify broader
  Azure-CLI-on-FreeBSD compatibility -- validate that yourself before relying
  on this in production.
* **Root** for any mutating action (writing `/etc/iscsi.conf`,
  enabling/starting `iscsid`, adding iSCSI sessions). `--dry-run` does not
  require root and performs no mutation.

This script intentionally does **not**:
* call `iscsiadm`, `systemd`, or any Linux package manager;
* call `iscsictl -R -a` (or otherwise disconnect/remove sessions);
* restart the `iscsid` service, or touch sessions it did not create;
* assemble `gmultipath` devices. Session creation is not multipath-device
  assembly, and this one-session PoC does not automate `gmultipath`. Any future
  multi-path design must separately map the correct `/dev/da*` devices and
  validate the platform-specific topology.

## Usage

```sh
# Preview only -- no root required, no mutating commands are run.
python3 connect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group \
  -n volume1 volume2 --dry-run

# Connect volume1 and volume2 with one session each (run as root).
python3 connect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group \
  -n volume1 volume2

# Opt in to provisional zonal-affinity IQN routing.
python3 connect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group \
  -n volume1 --enable-zonal-affinity

# Target a specific subscription (both spellings are equivalent).
python3 connect_for_documentation.py --elastic-san-subscription <sub-id-or-name> ...
python3 connect_for_documentation.py --subscription <sub-id-or-name> ...
```

| Flag | Alias | Meaning |
| --- | --- | --- |
| `--elastic-san-subscription` | `--subscription` | Elastic SAN subscription name or ID (default: current `az` context) |
| `-g`, `--resource-group` | | Resource group of the Elastic SAN |
| `-e`, `--elastic-san` | | Elastic SAN name |
| `-v`, `--volume-group` | | Volume group name |
| `-n`, `--volumes` | | One or more volume names |
| `-s`, `--num-of-sessions` | | Sessions per volume; must be exactly 1 (default: 1 -- see "Session count" below) |
| `--enable-zonal-affinity` | | Opt in to provisional logical→physical AZ IQN suffix (see below) |
| `--dry-run` | | Read-only discovery/planning only; no mutation (see below) |

## `--dry-run`

`--dry-run` performs every read-only step -- Azure CLI/extension check, IMDS
and Azure Resource Manager zonal lookups (if `--enable-zonal-affinity` is
set), the per-volume storage-target lookup, and nickname/config-block
computation -- and prints the plan (resolved targets, nicknames, and the
selected entries that would be merged into the managed block). It does **not**
run `sysrc`, `service`, or `iscsictl`, and does **not** write
`/etc/iscsi.conf`. It does not require root.

Because inspecting existing sessions requires opening `/dev/iscsi` (which
generally requires root), `--dry-run` does not inspect current session state
and plans one session per volume. The real run checks readiness before
submitting a session -- see "Idempotency and reruns" below.
Because the existing config is intentionally not opened in unprivileged
dry-run mode, retained managed entries for unselected volumes are not included
in the preview.

## What gets written to `/etc/iscsi.conf`

For every volume the script generates a deterministic nickname
(`esan-<volume-group>-<volume>-s1`, sanitized to `[a-z0-9_-]`) and a matching
stanza. If that readable nickname would exceed FreeBSD's 128-character limit,
the readable base is truncated and a `-<12 hex SHA-256>-s1` suffix derived from
the complete unbounded identity is appended. This keeps the nickname stable,
bounded, and collision-resistant:

```
esan-my-volume-group-volume1-s1 {
	TargetName    = "iqn.2005-03.com.microsoft:<...>"
	TargetAddress = "10.0.0.4:3260"
	HeaderDigest  = CRC32C
	DataDigest    = CRC32C
	Enable        = On
}
```

All of this lives inside a clearly delimited managed block:

```
# BEGIN AZURE ELASTIC SAN FREEBSD MANAGED BLOCK -- DO NOT EDIT
...stanzas...
# END AZURE ELASTIC SAN FREEBSD MANAGED BLOCK
```

This marker pair is intentionally origin-neutral (no "generated by ..." text)
and is byte-for-byte identical to the one the Azure portal's generated
FreeBSD connect script uses. Both tools write to the same `/etc/iscsi.conf`,
so each must recognize the other's managed block.

Anything outside that block -- your own stanzas, comments, CHAP secrets for
other targets, etc. -- is preserved **byte-for-byte**. If `/etc/iscsi.conf`
already has a managed block from a previous run (from this script or the
portal's), it is parsed strictly. Unknown, malformed, incomplete, or duplicate
content inside the markers is rejected rather than discarded. Entries selected
in the current run replace entries with the same deterministic nickname;
managed entries for unselected volumes are retained. The merged block is sorted
by nickname, so selecting A then B or B then A converges to the same bytes. If
the file has only one marker, duplicate markers, or non-standalone marker text,
the script refuses to touch it rather than guessing.

`TargetName`/`TargetAddress` values are validated before they are written:
anything containing a quote, brace, `#`, `;`, or a control character
(including newlines) is rejected outright rather than escaped, since
`iscsi.conf(5)`'s grammar has no documented escape sequence for those
characters -- allowing them through could let a malformed API response inject
a second statement or stanza into the file.

We deliberately do not set `LoginTimeout`/`PingTimeout` in the generated
stanzas. `iscsi.conf(5)` exposes both, but this PoC has no evidence-based
"safe default" for either across Elastic SAN's network paths and simply
inherits FreeBSD's built-in defaults (`kern.iscsi.login_timeout` = 60s,
`kern.iscsi.ping_timeout` = 5s). Tune them explicitly in your own
`/etc/iscsi.conf` if your environment needs different values -- outside the
managed block, so they survive reruns.

### Atomic write, backup, and rollback

Every write to `/etc/iscsi.conf` goes through: write a temp file in the same
directory → `fsync` → preserve the original file's permissions and ownership
(where the platform supports it) → `os.replace` (atomic rename). Before any
of that, if `/etc/iscsi.conf` already existed, its exact current bytes are
copied to a backup file (`/etc/iscsi.conf.bak.pre-esan-connect.<pid>`,
created with restrictive `0600` permissions regardless of the original file's
mode, since preserved unrelated stanzas may contain CHAP secrets). The backup
is **not** deleted automatically; it's left behind for manual recovery.

If starting/configuring the requested sessions then fails partway through,
the script restores `/etc/iscsi.conf` from that backup (or deletes the file
entirely if it did not exist before this run) before re-raising the error.
This only ever rolls back the **config file** -- it never disconnects or
removes any iSCSI session. After `iscsictl -A` has been submitted, a session
may already be live or may become live after a timeout even though the config
file was restored. Always inspect `iscsictl -L -v` before retrying a failed run.

## Idempotency and reruns

Re-running the script with the same arguments regenerates an identical
managed block (nicknames are deterministic), so the config file converges
rather than accumulating duplicate stanzas.

Before adding a session, the script parses `Target name`, `Target portal`,
`Enable`, and `Session state` from `iscsictl -L -v`. It matches by **target IQN
only**, not by the original portal: a successful iSCSI `TargetAddress` login
redirect legitimately changes the portal shown by FreeBSD.

* A matching `Connected` session is idempotent success and is skipped.
* A matching disconnected, disabled, or otherwise non-connected session fails
  with recovery guidance. The script neither adds a duplicate nor removes the
  stale session.
* Multiple matching sessions fail because this PoC supports exactly one.
* With no match, the script submits exactly
  `iscsictl -A -n <nickname> -c /etc/iscsi.conf` (without `-w`), then polls
  `iscsictl -L -v` for up to 30 seconds until that target IQN is `Connected`.
  Unrelated disconnected sessions are ignored. A timeout is explicit and does
  not remove or roll back a session that may already have been created.

## `iscsid` service management

The script enables `iscsid` persistently with `sysrc iscsid_enable=YES`, but
only writes that value if `sysrc -n iscsid_enable` doesn't already report
`YES`; an unset variable (reported by `sysrc` as a non-zero exit) is treated
as disabled and set to `YES` (idempotent, avoids noisy reruns). It checks
whether `iscsid` is already running with `service iscsid onestatus` and only
runs `service iscsid start` if it isn't. It never restarts `iscsid` and never
touches sessions/targets it did not create itself.

## Session count

The default and only accepted value is **1 session per volume**. Stock FreeBSD
rejects duplicate target-name/portal sessions, so values such as `-s 2` or
`-s 32` fail before Azure discovery or local mutation; they are never silently
capped to 1. NetApp-specific multi-session behavior is a separate validation
track and must not be inferred from this PoC.

## Zonal affinity (`--enable-zonal-affinity`)

Identical semantics to the Linux script: resolves the VM's availability zone
via IMDS, confirms the Elastic SAN subscription/region match the VM's, maps
the VM's logical zone to a physical zone via the ARM Locations API, and
appends a provisional `:az-<physical-zone>` suffix to the target IQN
(`[a-z0-9.-]+`, and the resulting IQN must stay within 223 UTF-8 bytes per
RFC 3720). **This is provisional**: the Elastic SAN front end must parse and
strip this suffix before zonal-affinity routing has any effect in
production. Without `--enable-zonal-affinity` (the default), the original,
undecorated IQN is used.

## Known limitations / external validation gates

These are things this PoC does not verify and that you must validate
yourself before depending on it in production:

1. **Azure CLI on FreeBSD is community-maintained, not Microsoft-supported.**
   The script only confirms `az` is present and the `elastic-san` extension
   is installed; it cannot verify that the FreeBSD build of Azure CLI behaves
   identically to the officially supported Linux/Windows/macOS builds.
2. **iSCSI login-redirect (status-class 1) compatibility is unverified.**
   Upstream FreeBSD `iscsid` implements RFC 3720/7143 login redirects
   (a target returning a status-class-1 response with a new `TargetAddress`,
   commonly used for portal-group load balancing / failover). Whether Elastic
   SAN's backend issues such redirects, and whether FreeBSD's `iscsid`
   handles them correctly against that specific backend/firmware version
   (e.g., a NetApp-derived target stack or fork), is **not validated by this
   script**. Confirm this against the exact Elastic SAN backend version/fork
   you are pointed at -- this is a hard gate before production use, since a
   redirect FreeBSD's `iscsid` mishandles would surface as session churn or
   silent connectivity loss, not a clean error from this script. The
   readiness check accounts for a successful redirect by correlating by target
   IQN instead of requiring the original portal.
3. **`gmultipath` assembly is not automated** (see "What this script
   intentionally does not do" above) -- session creation is not multipath
   device assembly.
4. **Multi-session is not enabled.** Stock FreeBSD's duplicate restriction is
   enforced as one session per volume. Any NetApp-specific exception needs
   dedicated interoperability and failure-mode validation.

## Testing

```sh
python3 -m py_compile connect_for_documentation.py test_connect_for_documentation.py
python3 -W error::ResourceWarning -m unittest test_connect_for_documentation -v
```

All Azure CLI, IMDS, filesystem, and `iscsictl`/`service`/`sysrc` calls are
mocked in the test suite -- it does not require FreeBSD, root, or network
access to run.
