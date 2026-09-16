# Azure Elastic SAN Multi-Session Connect Script -- FreeBSD (PoC)

Standalone, production-quality **proof of concept** script that connects one
or more Azure Elastic SAN volumes to a FreeBSD 13+ host with multiple iSCSI
sessions per volume, using FreeBSD's native iSCSI initiator
(`iscsid`/`iscsictl`/`iscsi.conf`, `sysrc`/`service`).

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
* assemble `gmultipath` devices. **Creating N iSCSI sessions to a volume is
  not the same as assembling a multipath device.** This script only brings up
  sessions; correlating the resulting `/dev/da*` device nodes into a single
  multipath device requires knowing which `daN` node belongs to which
  session/LUN, which this PoC does not attempt (there is no generically safe
  way to do that without also knowing your specific `gmultipath` layout). Do
  that step yourself, e.g. with `iscsictl -L -v` (see "Device nodes:" per
  session) and `gmultipath create`.

## Usage

```sh
# Preview only -- no root required, no mutating commands are run.
python3 connect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group \
  -n volume1 volume2 -s 4 --dry-run

# Connect volume1 and volume2 with 4 sessions each (run as root).
python3 connect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group \
  -n volume1 volume2 -s 4

# Opt in to provisional zonal-affinity IQN routing.
python3 connect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group \
  -n volume1 -s 4 --enable-zonal-affinity

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
| `-s`, `--num-of-sessions` | | Sessions per volume (default/max 32 -- see "Session count" below) |
| `--enable-zonal-affinity` | | Opt in to provisional logical→physical AZ IQN suffix (see below) |
| `--dry-run` | | Read-only discovery/planning only; no mutation (see below) |

## `--dry-run`

`--dry-run` performs every read-only step -- Azure CLI/extension check, IMDS
and Azure Resource Manager zonal lookups (if `--enable-zonal-affinity` is
set), the per-volume storage-target lookup, and nickname/config-block
computation -- and prints the plan (resolved targets, nicknames, and the
exact `/etc/iscsi.conf` managed block that would be written). It does **not**
run `sysrc`, `service`, or `iscsictl`, and does **not** write
`/etc/iscsi.conf`. It does not require root.

Because inspecting existing sessions requires opening `/dev/iscsi` (which
generally requires root), `--dry-run` does not inspect current session state
and always plans for the full requested session count per volume. The real
(non-dry-run) run inspects existing sessions and only adds the shortfall --
see "Idempotency and reruns" below.

## What gets written to `/etc/iscsi.conf`

For every `(volume, session index)` pair the script generates a deterministic
nickname (`esan-<volume-group>-<volume>-<session-index>`, sanitized to
`[a-z0-9_-]`) and a matching stanza:

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
so whichever one ran most recently must recognize and fully replace the
other's managed block rather than seeing it as a stray/corrupted marker.

Anything outside that block -- your own stanzas, comments, CHAP secrets for
other targets, etc. -- is preserved **byte-for-byte**. If `/etc/iscsi.conf`
already has a managed block from a previous run (from this script or the
portal's), it is fully replaced (not appended to); if it has only one of the
`BEGIN`/`END` markers (hand-edited or corrupted), the script refuses to touch
the file and tells you to fix it manually rather than guessing.

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
removes any iSCSI session, including ones this run itself just established
successfully for an earlier volume in the same invocation.

## Idempotency and reruns

Re-running the script with the same arguments regenerates an identical
managed block (nicknames are deterministic), so the config file converges
rather than accumulating duplicate stanzas.

Before adding sessions for a volume, the script inspects `iscsictl -L -v` and
counts sessions whose `Target name`/`Target portal` match that volume's
target. FreeBSD's kernel session state does not retain the nickname a
session was added under, so this match is necessarily by target identity,
not by nickname:

* **Existing count == requested count:** skipped, nothing is added.
* **Existing count < requested count:** only the shortfall is added, via
  `iscsictl -A -n <nickname> -c /etc/iscsi.conf -w 30` for each missing
  nickname (sessions are added **sequentially**, one `iscsictl -A` per
  session -- FreeBSD has no equivalent of Linux's "reuse this session ID for
  N more sessions" operation).
* **Existing count > requested count:** the script **fails with guidance**
  instead of removing anything. It never calls `iscsictl -R` (targeted) or
  `iscsictl -R -a` (all sessions) to reconcile a surplus -- if you see this,
  either raise `-s`/`--num-of-sessions` to match, or disconnect the extra
  sessions yourself after confirming which ones are safe to drop.

After adding sessions, the script re-inspects `iscsictl -L -v` to confirm the
expected count is actually observed (`iscsictl -A -w` waits for
establishment and blocking is best-effort per `iscsictl(8)` -- a non-zero
exit does not guarantee failure and a zero exit does not guarantee success,
so this is a second, independent check). If the count still falls short, the
script fails (triggering the config rollback described above) rather than
silently reporting success.

## `iscsid` service management

The script enables `iscsid` persistently with `sysrc iscsid_enable=YES`, but
only writes that value if `sysrc -n iscsid_enable` doesn't already report
`YES`; an unset variable (reported by `sysrc` as a non-zero exit) is treated
as disabled and set to `YES` (idempotent, avoids noisy reruns). It checks
whether `iscsid` is already running with `service iscsid onestatus` and only
runs `service iscsid start` if it isn't. It never restarts `iscsid` and never
touches sessions/targets it did not create itself.

## Session count

The default and maximum is 32 sessions per volume (same cap as the Linux
script). This is a client-side cap in the script, not a value derived from
your specific environment -- validate it against your own constraints (link
bandwidth/IOPS budget, per-target session limits on the Elastic SAN backend,
and FreeBSD's `kern.iscsi.max_sessions`/initiator worker-thread sizing)
before relying on the default in production. The script does **not** modify
`iscsid`'s global `maxproc`/worker-thread configuration; if your session
count needs exceed the daemon's defaults, that is a separate, deliberate
tuning decision for the operator to make.

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
   silent connectivity loss, not a clean error from this script.
3. **`gmultipath` assembly is not automated** (see "What this script
   intentionally does not do" above) -- session creation is not multipath
   device assembly.
4. **Nickname-to-session correlation on reruns is best-effort.** Because
   FreeBSD's kernel session state does not track the nickname a session was
   added under, "add the shortfall using the next unused nicknames" is a
   reasonable but not perfectly authoritative reconciliation strategy if
   sessions to the same target were established outside this script.
5. **iscsictl `-w` wait semantics are best-effort**, per `iscsictl(8)`: a
   non-zero exit status does not guarantee the session failed, so the
   post-add `iscsictl -L -v` recount is the authoritative check this script
   relies on, not the `iscsictl -A` exit code alone.

## Testing

```sh
python3 -m py_compile connect_for_documentation.py test_connect_for_documentation.py
python3 -m unittest test_connect_for_documentation -v
```

All Azure CLI, IMDS, filesystem, and `iscsictl`/`service`/`sysrc` calls are
mocked in the test suite -- it does not require FreeBSD, root, or network
access to run.
