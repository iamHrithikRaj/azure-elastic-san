# Azure Elastic SAN Connect Script -- FreeBSD (PoC)

Standalone **proof of concept** script that connects one
or more Azure Elastic SAN volumes to a FreeBSD 13+ host with **one iSCSI
session per volume**, using FreeBSD's native iSCSI initiator
(`iscsid`/`iscsictl`/`iscsi.conf`, `sysrc`/`service`).

Stock FreeBSD rejects attempts to create duplicate sessions for the same target
name and portal. This PoC therefore requires `--num-of-sessions 1` and does not
silently reduce larger values. NetApp-specific initiator/target behavior that
may permit multiple sessions needs separate validation and is outside this PoC.
Both default and opted-in connections retain the service-provided FQDN; this
script has no VIP discovery, allocation, or dependency on a Linux script or an
adjacent runtime module. The Python file can be downloaded independently.

This is a **separate implementation** from
[`CLI (Linux) Multi-Session Connect Scripts`](../CLI%20(Linux)%20Multi-Session%20Connect%20Scripts/connect_for_documentation.py).
FreeBSD's iSCSI stack (`iscsid`/`iscsictl`, `/etc/iscsi.conf`, `sysrc`/`service`)
is fundamentally different from Linux's (`open-iscsi`/`iscsiadm`, distro
package managers), so the two scripts intentionally do not share a code path.
The zonal-affinity resolution logic (IMDS lookup, subscription/region checks,
ARM Locations mapping, IQN decoration) is kept semantically identical between
the two scripts.

## Prerequisites

* **FreeBSD 13 or later** with the native iSCSI initiator (`iscsi(4)`,
  `iscsid(8)`, `iscsictl(8)`). A mutating run checks `uname -s` and
  `freebsd-version -u` and stops on anything else. The stock `GENERIC` kernel
  does **not** include `device iscsi`; the driver ships as the loadable
  `iscsi.ko` module, which the script loads and persists for you (see
  "Host prerequisites" below). A custom kernel with `device iscsi` compiled in
  is also accepted.
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
* **Root** for any mutating action (writing `/etc/iscsi.conf`, loading and
  persisting the `iscsi` kernel module, enabling/starting `iscsid`, enabling
  boot-time session re-login, adding iSCSI sessions). `--dry-run` does not
  require root and performs no mutation.

This script intentionally does **not**:
* call `iscsiadm`, `systemd`, or any Linux package manager;
* call `iscsictl -R -a` (or otherwise disconnect/remove sessions);
* restart the `iscsid` service, start the `iscsictl` rc service, or touch
  sessions it did not create;
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
| `--skip-recommended-settings` | | Accepted for CLI parity with the Linux/Windows scripts; no effect on FreeBSD (see "Recommended settings") |
| `--dry-run` | | Read-only discovery/planning only; no mutation (see below) |

## Execution order

A mutating run always follows the same order as the Linux and Windows
scripts. Any stop prints one `ERROR: <message>` line on stderr and exits
with status 1 (no Python traceback):

1. **Privilege**: must be root.
2. **Platform**: `uname -s` must be `FreeBSD`, the required tools
   (`iscsictl`, `service`, `sysrc`, `kldstat`, `kldload`, `freebsd-version`)
   must be on `PATH`, and `freebsd-version -u` must report 13 or later.
3. **Read-only lookup**: Azure CLI check and per-volume storage-target lookup
   (plus zonal mapping when opted in).
4. **Host prerequisites**: iSCSI kernel driver, `iscsid`, and boot-time
   re-login (see "Host prerequisites").
5. **Recommended settings**: prints the single informational line described
   under "Recommended settings"; nothing is changed.
6. **Pre-mutation plan**: one `iscsictl -L -v` inventory, then every selected
   volume is classified (see "Existing connections and reruns") before any
   config or session is changed.
7. **Connect**: one volume at a time, its managed entry is written and its
   session is added (see "Atomic write, backup, and rollback").
8. **Validation**: read-only `[PASS]`/`[WARN]`/`[FAIL]` checks and a summary
   (see "Validation and exit codes"). Any `[FAIL]` ends the run in error.

Nothing on FreeBSD needs a reboot to take effect, so the script never prints
a reboot notice.

## `--dry-run`

`--dry-run` performs every read-only step -- Azure CLI/extension check, IMDS
and Azure Resource Manager zonal lookups (if `--enable-zonal-affinity` is
set), the per-volume storage-target lookup, and nickname/config-block
computation -- and prints the plan (resolved targets, nicknames, and the
selected entries that would be merged into the managed block). It does **not**
run `sysrc`, `service`, `iscsictl`, `kldstat`, `kldload`, or
`freebsd-version`, does **not** write `/etc/iscsi.conf`, and skips the
privilege/platform checks and validation. It does not require root.

Subprocess output is captured in temporary files, including during dry-run,
so inherited output handles cannot make timeout cleanup wait indefinitely.
Each command has an execution timeout plus at most five seconds for post-kill
reaping. Only the direct child is terminated on timeout; deliberately started
services and other descendants are not killed as a process group.

Because inspecting existing sessions requires opening `/dev/iscsi` (which
generally requires root), `--dry-run` does not inspect current session state
and plans one session per volume. The real run inventories existing sessions
before changing anything -- see "Existing connections and reruns" below.
Native inspection is not inherently read-only:
[`iscsictl` startup](https://github.com/freebsd/freebsd-src/blob/25985322095d073354d31431da34da1e6871cca5/usr.bin/iscsictl/iscsictl.c#L881-L891)
can load the kernel module when `/dev/iscsi` is missing. Dry-run avoids all
`iscsictl` calls, not just session-add commands. A mutating run loads the
module explicitly (see "Host prerequisites") before inspecting sessions.
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
3. Form the suffix `-<16-hex-digest>-s1`. Truncate the readable base to the
   first `128 - len(suffix)` characters, trim trailing `-` from that prefix,
   and append the suffix.

Every nickname includes the digest, even when the readable base is short. This
keeps names stable and distinguishes raw identities that sanitize to the same
text, different group/volume boundaries, and different target IQNs while
remaining within 128 characters and `[a-z0-9][a-z0-9_-]*`:

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
1 through 65535 (not a Boolean or string). No hostname rewriting or DNS lookup
is performed.

We deliberately do not set `LoginTimeout`/`PingTimeout` in the generated
stanzas. `iscsi.conf(5)` only has them on FreeBSD 14.0 and later (13.x
`iscsictl` rejects the whole file if they appear), the managed-block format is
shared with the Azure portal script, and this PoC has no evidence-based "safe
default" for either across Elastic SAN's network paths. The script inherits
FreeBSD's built-in defaults (`kern.iscsi.login_timeout` = 60s,
`kern.iscsi.ping_timeout` = 5s). Tune them explicitly in your own
`/etc/iscsi.conf` if your environment needs different values -- outside the
managed block, so they survive reruns. See "Recommended settings" below.

### Atomic write, backup, and rollback

Mutating runs are serialized with an exclusive `fcntl.flock` on the separate
`/etc/.azure-elastic-san.lock` file. The separate inode is required because
`/etc/iscsi.conf` itself is atomically replaced. The lock is securely opened
without following symlinks where the platform supports `O_NOFOLLOW`, must be a
regular non-symlink file, and is created with mode `0600`. It is acquired before
reading or merging the config and held through backup, atomic write, host
prerequisites, session setup, and any rollback. This prevents concurrent
standalone or portal-generated runs that honor the same lock contract from
losing each other's updates. `--dry-run` neither creates nor acquires the lock.

While holding that lock, every write to `/etc/iscsi.conf` goes through: write a
temp file in the same directory → `fsync` → preserve the original file's
permissions and ownership (where the platform supports it) → `os.replace`
(atomic rename).

Each volume that the pre-mutation plan selects for connection is its own
config transaction: back up the current file, add that volume's managed entry,
then establish its session. The backup is the exact current bytes of
`/etc/iscsi.conf`, including entries added for volumes connected earlier in
this run. It is written to `/etc/iscsi.conf.bak.pre-esan-connect.<pid>` with
restrictive `0600` permissions regardless of the original file's mode, since
preserved unrelated stanzas may contain CHAP secrets. Each volume's backup
replaces the previous one, so the file always holds the state before the
volume in progress; content outside the managed block is identical in every
copy. The backup is **not** deleted automatically; it's left behind for manual
recovery. No backup is taken while the file doesn't exist yet. When every
selected volume is skipped, the file is neither written nor backed up.

If establishing a volume's session fails, the script restores
`/etc/iscsi.conf` from that volume's backup (or deletes the file if it did not
exist before this run and this was the first volume), which removes only the
failed volume's entry. It then stops with an error that names the failed
volume and any volumes connected earlier in this run; those keep their
sessions and managed entries, so they stay persistent. Validation is not run
in that case.
This only ever rolls back the **config file** -- it never disconnects or
removes any iSCSI session. After `iscsictl -A` has been submitted, a session
may already be live or may become live after a timeout even though the config
file was restored. Always inspect `iscsictl -L -v` before retrying a failed run.
Host prerequisite changes (kernel module, `iscsid`, `iscsictl_enable`) are not
rolled back either. A rerun after fixing the cause skips the volumes that are
already connected and connects the rest.

## Existing connections and reruns

Re-running the script with the same arguments regenerates an identical
managed block (nicknames are deterministic), so the config file converges
rather than accumulating duplicate stanzas.

Before changing any config or session, the script takes one `iscsictl -L -v`
inventory and classifies **every** selected volume. It correlates by **target
IQN only** (exact, case-insensitive), not by the original portal: a
successful iSCSI `TargetAddress` login redirect legitimately changes the
portal shown by FreeBSD. "Persistent" means an entry for that IQN in the
managed block of `/etc/iscsi.conf`.

| Live sessions for the IQN | Managed entries for the IQN | Action |
| --- | --- | --- |
| none | none | Connect: write the entry and add the session |
| none | exactly this volume's entry | Re-establish that single configured session; no new entry |
| none | a different entry (for example, written with differently cased resource names) or several | Skip: `persistent configuration exists but no live sessions`, with the `iscsictl -A -n <nickname>` command to establish it. A second entry is never added |
| one `Connected` | any | Skip: `already connected (<live> live / <persistent> persistent)`. With no managed entry the session is reported as not persistent; the script does not add one (disconnect it and re-run to make it persistent) |
| more than one, or any not `Connected`/disabled | any | Skip as a pre-existing anomaly, with manual-recovery guidance. The script never adds, removes, or modifies those sessions |

A skipped volume's state is reported by validation as `[WARN]`, because this
script never disconnects or rewrites existing sessions. Stanzas outside the
managed block are not inspected: a hand-written stanza for the same IQN
elsewhere in `/etc/iscsi.conf` is not detected.

A volume selected more than once in one run (for example `-n vol1 VOL1`,
which resolve to the same IQN in any case) is planned and connected once: the
first occurrence is kept, and each later one prints
`<volume> [<iqn>]: Ignored duplicate selection; the same target is already planned as '<first>'`.
This also applies to `--dry-run`.

For a volume being connected, the script submits exactly
`iscsictl -A -n <nickname> -c /etc/iscsi.conf` (without `-w`), then polls
`iscsictl -L -v` for up to 30 seconds until that target IQN is `Connected`.
Just before submitting, it re-checks that volume's sessions; if one appeared
since the inventory, it is skipped (`Connected`) or the run stops (anomaly).
Unrelated disconnected sessions are ignored. A timeout is explicit and does
not remove or roll back a session that may already have been created.

## Host prerequisites

* **iSCSI kernel driver.** `kldstat -q -n iscsi` detects the loadable
  `iscsi.ko` module by file name. (`kldstat -m iscsi` would also match a
  driver compiled into the kernel.)
  * Module loaded: `iscsi_load="YES"` is persisted with
    `sysrc -f /boot/loader.conf iscsi_load=YES`, only if it is not already set.
  * No module but `/dev/iscsi` exists: the driver is compiled into the kernel;
    nothing is loaded or persisted.
  * Neither: `kldload iscsi`, then persist as above. If loading fails, or
    `/dev/iscsi` still does not exist, the run stops before touching the
    config or any session.
* **`iscsid`.** The script enables `iscsid` persistently with
  `sysrc iscsid_enable=YES`, but only writes that value if
  `sysrc -n iscsid_enable` doesn't already report `YES`; an unset variable
  (reported by `sysrc` as a non-zero exit) is treated as disabled and set to
  `YES`. It checks whether `iscsid` is already running with
  `service iscsid onestatus` and only runs `service iscsid start` if it isn't.
  It never restarts `iscsid` and never touches sessions/targets it did not
  create itself.
* **Boot persistence (`iscsictl_enable`).** FreeBSD keeps iSCSI sessions only
  in kernel memory. After a reboot, nothing re-adds them unless the
  `iscsictl` rc service is enabled; the earlier versions of this script and of
  the Azure portal FreeBSD script did not enable it, so managed sessions were
  not re-established after a reboot. This is the FreeBSD equivalent of
  Windows persistent logins and Linux `node.startup = automatic`.
  * If `iscsictl_flags` is the stock `-Aa` (the `/etc/defaults/rc.conf`
    value) or undefined, the script sets `iscsictl_enable=YES` (only if it
    isn't already `YES`).
  * If `iscsictl_flags` is customized, neither variable is changed and
    validation reports a `[WARN]`. An explicitly empty value counts as
    customized: `rc.d/iscsictl` would then run a bare `iscsictl`, which only
    lists sessions.
  * The service is only enabled, not started, so this run doesn't connect
    anything else. **Note:** at boot, the stock `iscsictl -Aa` adds **every**
    stanza in `/etc/iscsi.conf` as a session, not only the managed ones.
    Stanzas with `Enable = Off` are added disabled and don't log in; use that
    for your own stanzas that should stay manual.

## Recommended settings (`--skip-recommended-settings`)

The Windows and Linux scripts apply the client-side values from
[Elastic SAN configuration best practices](https://learn.microsoft.com/azure/storage/elastic-san/elastic-san-best-practices)
unless `-SkipRecommendedSettings` / `--skip-recommended-settings` is passed.
On FreeBSD, none of those values has a safe, documented equivalent, so this
script changes nothing and prints exactly one line in the settings step:

```
Recommended settings: not applicable on FreeBSD; no Elastic SAN tuning value has a documented FreeBSD equivalent.
```

`--skip-recommended-settings` is accepted so the same command line works for
every platform, and has no effect. The Azure portal's generated FreeBSD script
prints the same line.

| Windows / Linux setting | Value | FreeBSD |
| --- | --- | --- |
| `MaxTransferLength` / `MaxXmitDataSegmentLength`, `MaxRecvDataSegmentLength` | 262144 | No `iscsi.conf(5)` or `iscsi(4)` knob. The initiator limit comes from the undocumented, host-wide sysctl `kern.icl.soft.max_data_segment_length`, which already defaults to 256 KiB (`sys/dev/iscsi/icl_soft.c`, FreeBSD 13.0 and 14.1). |
| `MaxBurstLength`, `FirstBurstLength` | 262144 | No documented knob; only the undocumented, host-wide `kern.icl.soft.max_burst_length` / `first_burst_length` sysctls (1 MiB, negotiated down to the target's value). |
| `InitialR2T` | 0 / `No` | Not configurable: `iscsid` always proposes `InitialR2T=Yes` (`usr.sbin/iscsid/login.c`). |
| `ImmediateData` | 1 / `Yes` | Not configurable: `iscsid` always proposes `ImmediateData=Yes`, which already matches. |
| `WMIRequestTimeout` / `node.conn[0].timeo.login_timeout` | 30 | `LoginTimeout` exists in `iscsi.conf(5)` only on FreeBSD 14.0+; 13.x `iscsictl` rejects the file, and it would change the shared managed-block format. `kern.iscsi.login_timeout` (`iscsi(4)`) is host-wide and defaults to 60 s, already more tolerant than 30 s. |
| `node.conn[0].timeo.logout_timeout` | 15 | No equivalent. |
| `LinkDownTime` | 30 | No equivalent. `kern.iscsi.ping_timeout` and `kern.iscsi.fail_on_disconnection` have different semantics and are host-wide. |
| MPIO (MSDSM claim, round robin, disk timeout) / Linux `multipath.conf` | -- | Not applicable: this PoC uses one session per volume and does not automate `gmultipath(8)`. |
| Header/data digest `CRC32C` | always | Always written in every managed stanza (not opt-out) and checked by validation. |

## Validation and exit codes

After the connect phase completes, the script runs read-only checks and prints
one line per check, followed by a summary:

```
[PASS] iscsid_enable: YES
[PASS] iscsictl_enable: YES (sessions are re-added at boot)
[PASS] iSCSI kernel driver: iscsi.ko loaded; iscsi_load="YES" in /boot/loader.conf
[PASS] volume1 [iqn...] session: 1 Connected
[PASS] volume1 [iqn...] digests: header CRC32C, data CRC32C
[PASS] volume1 [iqn...] managed entry: esan-...-s1
Validation: 6 passed, 0 warnings, 0 failed
```

| Check | PASS | WARN | FAIL |
| --- | --- | --- | --- |
| `iscsid_enable` | `YES` | | anything else |
| `iscsictl_enable` | `YES` with stock `iscsictl_flags` | `iscsictl_flags` customized (left unchanged) | not `YES` with stock flags |
| iSCSI kernel driver | `iscsi.ko` loaded and `iscsi_load="YES"`, or compiled into the kernel | | otherwise |
| `<volume> [<iqn>] session` | exactly one `Connected` session | more than one session; or a skipped volume with no `Connected` session | no `Connected` session on a volume connected in this run |
| `<volume> [<iqn>] digests` | header and data `CRC32C` | not `CRC32C` on a skipped volume | not `CRC32C` on a volume connected in this run |
| `<volume> [<iqn>] managed entry` | exactly one managed entry | none on a skipped volume (not persistent), or more than one | none on a volume connected in this run |

If the session inventory or the config cannot be read during validation, that
is reported as one `[FAIL]` line and the dependent per-volume checks are
omitted. Any `[FAIL]` makes the script exit with status 1 and an
`ERROR: Validation reported N failed check(s)` line on stderr.

## Session count

The default and only accepted value is **1 session per volume**. Stock FreeBSD
rejects duplicate target-name/portal sessions, so values such as `-s 2` or
`-s 32` fail before Azure discovery or local mutation; they are never silently
capped to 1. NetApp-specific multi-session behavior is a separate validation
track and must not be inferred from this PoC.

**VIP/32-session support remains blocked and is not part of this script.**
Stock FreeBSD's add/modify checks reject duplicate target IQN plus configured
portal text with `EBUSY`; three canonical portals permit at most three normal
configured sessions, not 32. Different textual spellings of a portal, ISIDs,
or nicknames are not a supported workaround. This limitation does not remove
the useful one-session FQDN path, including its zonal-affinity opt-in.

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

This is connect-only support, not a migration or disconnect tool. Disabling
the opt-in does not clean up decorated sessions or managed entries; changing
the target IQN changes its nickname, and old managed entries are preserved.
Inspect and reconcile the affected sessions/config explicitly before switching
modes. Existing Linux/Windows disconnect scripts are not FreeBSD cleanup tools.

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
   dedicated interoperability and failure-mode validation against the exact
   initiator fork, scripts, and target/backend version before production use.
5. **Native and end-to-end qualification is outstanding.** Unit tests do not
   establish suffix parsing/routing support, native login or redirect behavior,
   kernel-module loading, boot-time re-login through `iscsictl_enable`,
   dual-stack operation, zone-failure recovery, or an approximately 30-second
   recovery-time objective. Validate on FreeBSD 13.x and 14.x before relying
   on it.

## Testing

```sh
python3 -m py_compile connect_for_documentation.py test_connect_for_documentation.py
python3 -W error::ResourceWarning -m unittest test_connect_for_documentation -v
```

Azure CLI, IMDS, and `iscsictl`/`service`/`sysrc` calls are mocked in the test
suite; filesystem tests use temporary files. It does not require FreeBSD,
root, or network access to run. Keep `ResourceWarning` treated as an error
to retain coverage for unclosed file handles.
