# Azure Elastic SAN Connect Script -- FreeBSD (PoC)

Standalone **proof of concept** script that connects one
or more Azure Elastic SAN volumes to a FreeBSD 13+ host with **1-32 iSCSI
sessions per volume (default 32)**, using FreeBSD's native iSCSI initiator
(`iscsid`/`iscsictl`/`iscsi.conf`, `sysrc`/`service`). Session count, the
`--enable-zonal-affinity` and `--enable-vip-distribution` opt-ins, and the
companion `disconnect_for_documentation.py` match the Linux and Windows
scripts.

Each session is its own `iscsi.conf` stanza. Stock FreeBSD rejects a second
session whose target name and portal both match an existing one, so on a stock
initiator use `-s 1`, or `--enable-vip-distribution` with at most one session
per resolved VIP (see "Session count"). Initiators that permit several sessions
per target portal can use any count up to 32. The script has no dependency on a
Linux script; `disconnect_for_documentation.py` imports helpers from this file,
so keep both files in the same directory.

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
* call `iscsictl -R -a` (or otherwise disconnect/remove sessions) -- use
  `disconnect_for_documentation.py` for the selected volumes (see "Disconnect");
* restart the `iscsid` service, or touch sessions it did not create;
* assemble `gmultipath` devices. Each session exposes its own `/dev/da*`
  device; combining them is the job of the host's multipath layer
  (`gmultipath(8)` or the initiator's own multipath support), which must claim
  the devices before the volume is used. With more than one session the script
  prints this as a one-line `Multipath:` notice.

## Usage

```sh
# Preview only -- no root required, no mutating commands are run.
python3 connect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group \
  -n volume1 volume2 --dry-run

# Connect volume1 and volume2 with 32 sessions each (run as root).
python3 connect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group \
  -n volume1 volume2

# Spread the sessions across the FQDN's IPv4 VIPs (32 over three VIPs: 11/11/10).
python3 connect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group \
  -n volume1 --enable-vip-distribution

# Match an initiator that allows fewer sessions per target portal (8 per VIP).
python3 connect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group \
  -n volume1 --enable-vip-distribution -s 24

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
| `-s`, `--num-of-sessions` | | Sessions per volume, 1-32 (default: 32 -- see "Session count" below) |
| `--enable-zonal-affinity` | | Opt in to provisional logical→physical AZ IQN suffix (see below) |
| `--enable-vip-distribution` | | Spread the sessions' portals across the FQDN's IPv4 VIPs (see "VIP distribution") |
| `--dry-run` | | Read-only discovery/planning only; no mutation (see below) |

| Options | IQN | Original portal of each session |
|---|---|---|
| Neither opt-in | Plain | Volume FQDN |
| `--enable-zonal-affinity` | `:az-<physicalZone>` suffix | Volume FQDN |
| `--enable-vip-distribution` | Plain | IPv4 VIP selection below |
| Both | Decorated | IPv4 VIP selection below |

## `--dry-run`

`--dry-run` performs every read-only step -- Azure CLI/extension check, IMDS
and Azure Resource Manager zonal lookups (if `--enable-zonal-affinity` is
set), the per-volume storage-target lookup, the IPv4 DNS lookup (if
`--enable-vip-distribution` is set), and nickname/config-block computation --
and prints the plan (resolved targets, each portal's session count, nicknames,
and the selected entries that would be merged into the managed block). It does **not**
run `sysrc`, `service`, or `iscsictl`, and does **not** write
`/etc/iscsi.conf`. It does not require root.

Subprocess output is captured in temporary files, including during dry-run,
so inherited output handles cannot make timeout cleanup wait indefinitely.
Each command has an execution timeout plus at most five seconds for post-kill
reaping. Only the direct child is terminated on timeout; deliberately started
services and other descendants are not killed as a process group.

Because inspecting existing sessions requires opening `/dev/iscsi` (which
generally requires root), `--dry-run` does not inspect current session state
and plans every requested session. The real run checks existing sessions before
writing anything -- see "Idempotency and reruns" below.
Native inspection is not inherently read-only:
[`iscsictl` startup](https://github.com/freebsd/freebsd-src/blob/25985322095d073354d31431da34da1e6871cca5/usr.bin/iscsictl/iscsictl.c#L881-L891)
can load the kernel module when `/dev/iscsi` is missing. Dry-run avoids all
`iscsictl` calls, not just session-add commands. A mutating run requires the
device node to exist, but does not provide a globally read-only native-state
preflight guarantee.
Because the existing config is intentionally not opened in unprivileged
dry-run mode, retained managed entries for unselected volumes are not included
in the preview.

## What gets written to `/etc/iscsi.conf`

For every volume the script generates a deterministic nickname with this exact
cross-tool contract (the Azure portal generator must use the same algorithm):

1. Build the identity as a compact JSON array with no spaces:
   `[rawVolumeGroupName,rawVolumeName,rawTargetName,sessionIndex]`, where
   `rawTargetName` is the resolved, unsanitized IQN written as `TargetName`
   (including the zonal-affinity suffix when enabled). Python uses
   `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`; the portal
   equivalent is `JSON.stringify([...])`. SHA-256 hashes the UTF-8 bytes of this
   JSON. The first 16 lowercase hexadecimal characters are the digest.
2. Trim and lowercase the group and volume independently, replace each run of
   characters outside `[a-z0-9]` with `-`, and trim leading/trailing `-`.
   Form the readable base `esan-<sanitized-group>-<sanitized-volume>`.
3. Form the suffix `-<16-hex-digest>-s<sessionIndex>` (`-s1` through `-s32`).
   Truncate the readable base to the first `128 - len(suffix)` characters,
   trim trailing `-` from that prefix, and append the suffix.

Every nickname includes the digest, even when the readable base is short. This
keeps names stable and distinguishes raw identities that sanitize to the same
text, different group/volume boundaries, and different target IQNs while
remaining within 128 characters and `[a-z0-9][a-z0-9_-]*`. Each session of a
volume gets its own stanza with the same `TargetName`; `TargetAddress` is that
session's portal (the FQDN, or a VIP with `--enable-vip-distribution`):

```
esan-my-volume-group-volume1-<16-hex-digest>-s1 {
	TargetName    = "iqn.2005-03.com.microsoft:<...>"
	TargetAddress = "portal.example:3260"
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
in the current run that already exist with the same nickname and identical
target/address are idempotent. A nickname collision with a different target or
address is rejected rather than overwritten. Managed entries for unselected
volumes are retained. The merged block is sorted by nickname, so selecting A
then B or B then A converges to the same bytes. If the file has only one marker,
duplicate markers, or non-standalone marker text, the script refuses to touch it
rather than guessing.

`TargetName`/`TargetAddress` values are validated before they are written:
anything containing a quote, brace, `#`, `;`, or a control character
(including newlines) is rejected outright rather than escaped, since
`iscsi.conf(5)`'s grammar has no documented escape sequence for those
characters -- allowing them through could let a malformed API response inject
a second statement or stanza into the file.
Before formatting `TargetAddress`, the raw portal hostname must be a nonempty
ASCII hostname with valid DNS labels, and the raw port must be an integer from
1 through 65535 (not a Boolean or string). No hostname rewriting is performed,
and DNS is queried only for `--enable-vip-distribution`.

We deliberately do not set `LoginTimeout`/`PingTimeout` in the generated
stanzas. `iscsi.conf(5)` exposes both, but this PoC has no evidence-based
"safe default" for either across Elastic SAN's network paths and simply
inherits FreeBSD's built-in defaults (`kern.iscsi.login_timeout` = 60s,
`kern.iscsi.ping_timeout` = 5s). Tune them explicitly in your own
`/etc/iscsi.conf` if your environment needs different values -- outside the
managed block, so they survive reruns.

### Atomic write, backup, and rollback

Mutating runs are serialized with an exclusive `fcntl.flock` on the separate
`/etc/.azure-elastic-san.lock` file. The separate inode is required because
`/etc/iscsi.conf` itself is atomically replaced. The lock is securely opened
without following symlinks where the platform supports `O_NOFOLLOW`, must be a
regular non-symlink file, and is created with mode `0600`. It is acquired before
reading or merging the config and held through backup, atomic write, `iscsid`
setup, session setup, and any rollback. This prevents concurrent standalone or
portal-generated runs that honor the same lock contract from losing each
other's updates. `--dry-run` neither creates nor acquires the lock.

While holding that lock, every write to `/etc/iscsi.conf` goes through: write a
temp file in the same directory → `fsync` → preserve the original file's
permissions and ownership (where the platform supports it) → `os.replace`
(atomic rename). Before any of that, if `/etc/iscsi.conf` already existed, its
exact current bytes are copied to a backup file
(`/etc/iscsi.conf.bak.pre-esan-connect.<pid>`, created with restrictive `0600`
permissions regardless of the original file's mode, since preserved unrelated
stanzas may contain CHAP secrets). The backup is **not** deleted automatically;
it's left behind for manual recovery.

If starting/configuring the requested sessions then fails partway through,
the script restores `/etc/iscsi.conf` from that backup (or deletes the file
entirely if it did not exist before this run) before re-raising the error.
This only ever rolls back the **config file** -- it never disconnects or
removes any iSCSI session. After `iscsictl -A` has been submitted, a session
may already be live or may become live after a timeout even though the config
file was restored. Always inspect `iscsictl -L -v` before retrying a failed run.
Service enablement/start is not rolled back either. Input preflight is not an
all-volume transaction: if connecting a later volume fails, earlier volumes
may remain connected.

## Idempotency and reruns

Re-running the script with the same arguments regenerates an identical
managed block (nicknames are deterministic), so the config file converges
rather than accumulating duplicate stanzas.

After setting up `iscsid` and before writing anything, the script takes one
`iscsictl -L -v` inventory and checks every selected volume. Like the Linux
and Windows scripts, a session belongs to a volume when its target name is the
volume's IQN or that IQN with one `:az-<zone>` suffix (exact and
case-insensitive, on any portal), so a volume connected in either zonal mode,
through the FQDN or through VIPs, is recognized. The original portal is not
compared: a successful iSCSI `TargetAddress` login redirect legitimately
changes the portal shown by FreeBSD.

* `Connected` sessions (any number) skip the volume, and none of its entries
  are written. Run `disconnect_for_documentation.py` first to change its layout.
* A disconnected, disabled, or otherwise non-connected session stops the run
  before any config or session change, with recovery guidance. The script
  neither adds a duplicate nor removes the stale session.
* With no session, the volume's stanzas are merged into the managed block. A
  nickname that already exists with a different target or address (for example
  a different session layout) stops the run instead of overwriting it.
* The script then submits `iscsictl -A -n <nickname> -c /etc/iscsi.conf`
  (without `-w`) for each session in order, and polls `iscsictl -L -v` for up
  to 30 seconds until that target IQN has that many `Connected` sessions.
  Unrelated disconnected sessions are ignored. A timeout is explicit and does
  not remove or roll back sessions that may already have been created.

## `iscsid` service management

The script enables `iscsid` persistently with `sysrc iscsid_enable=YES`, but
only writes that value if `sysrc -n iscsid_enable` doesn't already report
`YES`; an unset variable (reported by `sysrc` as a non-zero exit) is treated
as disabled and set to `YES` (idempotent, avoids noisy reruns). It checks
whether `iscsid` is already running with `service iscsid onestatus` and only
runs `service iscsid start` if it isn't. It never restarts `iscsid` and never
touches sessions/targets it did not create itself.

## Session count

`-s`/`--num-of-sessions` accepts 1 through 32 and defaults to 32, like the
Linux and Windows scripts. Other values stop before Azure discovery or local
mutation; they are never silently capped.

The FreeBSD kernel rejects a new session whose configured target name **and**
target address text both match an existing session (`EBUSY`, reported by
`iscsictl -A` as `Device busy`). Stock FreeBSD therefore supports one session
per target portal; initiators that lift that restriction support more. Pick a
count the initiator supports:

| Initiator | Without VIP distribution | With `--enable-vip-distribution` (N VIPs) |
| --- | --- | --- |
| Stock FreeBSD | `-s 1` | `-s` up to N (one session per VIP) |
| Allows K sessions per target portal | `-s` up to K | `-s` up to K x N (for example `-s 24` for 8/8/8) |

When the initiator rejects another session to a portal this run already used,
the error says so and suggests a smaller `--num-of-sessions` or
`--enable-vip-distribution`. The config is then rolled back (see "Atomic write,
backup, and rollback"); sessions already added stay up until you remove them
with `disconnect_for_documentation.py`.

## VIP distribution (`--enable-vip-distribution`)

Identical selection to the Linux and Windows scripts, independent of zonal
affinity: the volume FQDN is resolved with IPv4 `socket.getaddrinfo`, addresses
are deduplicated and sorted numerically. No IPv4 answer stops the run before any
change. **One address keeps the FQDN**, including for LRS and private
endpoints. With **N addresses**, each gets `count // N` sessions and the first
`count % N` get one more: 32 over three gives 11/11/10, over two 16/16. Addresses
with no share (fewer sessions than addresses) are not configured. Sessions are
assigned to VIPs in contiguous blocks (`-s1` through `-s11` on the first VIP,
and so on), and each FQDN is resolved once per run so every volume behind it
gets the same layout.

Spreading **original saved portals** keeps initial connections from all
depending on one zone. It complements the front end's ISID-hash redirect: the
front end may redirect any login, and this script neither controls nor verifies
the resulting placement.

## Multipath

Each session exposes its own disk device. The script does not assemble
multipath devices; with more than one session it prints:

```
Multipath: each iSCSI session exposes its own disk device; this script does not assemble multipath devices. Use the host's multipath layer (gmultipath(8) or the initiator's multipath support) before using the volumes.
```

Use `gmultipath(8)` or the initiator's own multipath support so one volume's
devices are used as a single device. Do not put a file system on an individual
`/dev/da*` path of a multi-session volume.

## Zonal affinity (`--enable-zonal-affinity`)

Identical semantics to the Linux script: resolves the VM's availability zone
via IMDS, confirms the Elastic SAN subscription/region match the VM's, maps
the VM's logical zone to a physical zone via the ARM Locations API, and
appends a provisional `:az-<physical-zone>` suffix to the target IQN.
The canonical subscription ID resolved for that mapping is pinned on every
selected-volume query, even when the caller initially used a subscription name
or the current Azure CLI context. Duplicate matching region entries or logical
zone mappings are rejected rather than selected by response order.
The physical zone is trimmed and lowercased and must match `[a-z0-9.-]+`.
The **complete decorated IQN**, including the original service identity, must
contain only lowercase ASCII letters, digits, `.`, `:`, and `-`, and fit within
223 UTF-8 bytes. Invalid, uppercase, or non-ASCII service identities are
rejected, never trimmed, lowercased, or otherwise rewritten.

All selected volumes' mapping, complete IQNs, config scalar values, and
nicknames are resolved and validated before acquiring the config lock or
changing config, service, or session state. Native session inspection happens
only in the mutating execution path, not during this input preflight.

**This is provisional**: the Elastic SAN front end must parse and
strip this suffix before zonal-affinity routing has any effect in
production. Without `--enable-zonal-affinity` (the default), the original,
undecorated IQN is used.

This is connect-only behavior, not a migration tool. Disabling
the opt-in does not clean up decorated sessions or managed entries; changing
the target IQN changes its nickname, and old managed entries are preserved.
Run `disconnect_for_documentation.py` for the volume before switching modes;
it removes plain and zonal sessions and entries alike.

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
   (or a customized initiator or target stack), is **not validated by this
   script**. Confirm this against the exact Elastic SAN backend version/fork
   you are pointed at -- this is a hard gate before production use, since a
   redirect FreeBSD's `iscsid` mishandles would surface as session churn or
   silent connectivity loss, not a clean error from this script. The
   readiness check accounts for a successful redirect by correlating by target
   IQN instead of requiring the original portal.
3. **`gmultipath` assembly is not automated** (see "Multipath") -- session
   creation is not multipath device assembly.
4. **Multi-session support depends on the initiator.** Stock FreeBSD allows
   one session per target portal (see "Session count"). Initiators that permit
   more need their own interoperability and failure-mode validation against the
   target/backend version before production use.
5. **Native and end-to-end qualification is outstanding.** Unit tests do not
   establish suffix parsing/routing support, native login or redirect behavior,
   reboot recovery, dual-stack operation, zone-failure recovery, or an
   approximately 30-second recovery-time objective.

## Disconnect

`disconnect_for_documentation.py` takes the same subscription, resource group,
Elastic SAN, volume group and volume arguments (no zonal or VIP flag is needed)
and must be run as root from the same directory as this script:

```sh
python3 disconnect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group -n volume1 volume2
```

Quiesce I/O and unmount the volumes first. After every volume lookup succeeds,
it asks for confirmation (`Y/Yes/N/No`; anything else asks again, end of input
cancels). Then, under the same config lock as the connect script, for each
volume it:

1. removes every session whose target is the volume's IQN or that IQN with one
   `:az-<zone>` suffix, on any portal (FQDN or VIP), with
   `iscsictl -R -t <target>`; the kernel removes all sessions for that exact
   target name;
2. removes the volume's entries from the managed block, so nothing re-adds them
   at boot. A pre-run copy is kept as `/etc/iscsi.conf.bak.pre-esan-disconnect.<pid>`
   (mode `0600`), and an emptied managed block is removed with its markers.

A volume with neither sessions nor entries prints `Skipped as this volume is
not connected`. Stanzas outside the managed block, other targets, and host
settings are never changed. The session inventory is skipped when `/dev/iscsi`
does not exist, because `iscsictl` could load the kernel module. A malformed
managed block stops before any change; a native failure stops the run and can
leave earlier volumes disconnected and the failing volume's entries in place.

## Testing

```sh
python3 -m py_compile connect_for_documentation.py disconnect_for_documentation.py
python3 -W error::ResourceWarning -m unittest discover -s . -p "test*.py" -v
```

Azure CLI, IMDS, DNS, and `iscsictl`/`service`/`sysrc` calls are mocked in the test
suite; filesystem tests use temporary files. It does not require FreeBSD,
root, or network access to run. Keep `ResourceWarning` treated as an error
to retain coverage for unclosed file handles.
