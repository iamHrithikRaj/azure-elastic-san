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
initiator use `-s 1`, with or without VIP distribution (see "Session count").
An initiator that allows K sessions to one target portal must use at most K
total sessions per volume, because redirects can concentrate them on one portal.
The script has no dependency on a
Linux script; `disconnect_for_documentation.py` imports helpers from this file,
so keep both files in the same directory.

The FreeBSD implementation is independent of the Linux and Windows scripts.
The tools share the zonal suffix and VIP allocation contracts, not host tooling.

## Prerequisites

* **FreeBSD 13 or later** with the native iSCSI initiator available:
  real execution verifies the release and loads the driver if `/dev/iscsi`
  is absent. A custom kernel needs `device iscsi` / `device iscsi_initiator`
  or a loadable `iscsi` module.
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
  loading the driver, enabling `iscsid` and boot re-login, adding sessions). `--dry-run` does not
  require root and performs no mutation.

This script intentionally does **not**:
* call `iscsiadm`, `systemd`, or any Linux package manager;
* call `iscsictl -R -a`; it never removes sessions that existed before the run.
  Use `disconnect_for_documentation.py` for selected volumes (see "Disconnect");
* restart `iscsid`, start the boot `iscsictl` service, or touch pre-existing sessions;
* assemble `gmultipath` devices. Each session exposes its own `/dev/da*`
  device; combining them is the job of the host's multipath layer
  (`gmultipath(8)` or the initiator's own multipath support), which must claim
  the devices before the volume is used. With more than one session the script
  prints this as a one-line `Multipath:` notice.

## Usage

```sh
# Stock FreeBSD: request one session per volume (run as root).
python3 connect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group \
  -n volume1 -s 1

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

# For an initiator that allows 8 sessions to one target portal, request 8 total.
python3 connect_for_documentation.py \
  -g my-resource-group -e my-elastic-san -v my-volume-group \
  -n volume1 --enable-vip-distribution -s 8

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
| `--skip-recommended-settings` | | Accepted for CLI parity; FreeBSD tuning is not changed |

| Options | IQN | Original portal of each session |
|---|---|---|
| Neither opt-in | Plain | Volume FQDN |
| `--enable-zonal-affinity` | `:az-<physicalZone>` suffix | Volume FQDN |
| `--enable-vip-distribution` | Plain | IPv4 VIP selection below |
| Both | Decorated | IPv4 VIP selection below |

## `--dry-run`

`--dry-run` checks Azure CLI/extension availability, reads volume targets, and
performs the enabled zonal and IPv4 DNS lookups. It prints resolved targets,
portal counts, nicknames and the selected managed entries. It requires no root
and never runs native tools, acquires a config lock or writes `/etc/iscsi.conf`.
It also prints shell-quoted native add commands and skips validation.

Subprocess output is captured in temporary files, including during dry-run,
so inherited output handles cannot make timeout cleanup wait indefinitely.
Each command has an execution timeout plus at most five seconds for post-kill
reaping. Only the direct child is terminated on timeout; deliberately started
services and other descendants are not killed as a process group.

The preview assumes no existing sessions and does not read the current config;
retained entries for unselected volumes are therefore absent. Real execution
checks existing state before writing. Native inspection is not zero-effect:
`iscsictl` can load the kernel module when `/dev/iscsi` is missing, so dry-run
avoids every `iscsictl` call, including inventory.

## What gets written to `/etc/iscsi.conf`

For every volume the script generates a deterministic nickname with this exact
cross-tool contract (the Azure portal generator must use the same algorithm):

1. Serialize a compact JSON array with no spaces:
   `[rawVolumeGroupName,rawVolumeName,rawTargetName,sessionIndex]`, where
   `rawTargetName` is the resolved, unsanitized IQN written as `TargetName`
   (including a zonal suffix). SHA-256 hashes its UTF-8 bytes; the first
   16 lowercase hexadecimal characters are the digest.
2. Trim and lowercase the group and volume independently, replace each run of
   characters outside `[a-z0-9]` with `-`, and trim leading/trailing `-`.
   Form the readable base `esan-<sanitized-group>-<sanitized-volume>`.
3. Form the suffix `-<16-hex-digest>-s<sessionIndex>` (`-s1` through `-s32`).
   Truncate the readable base to the first `128 - len(suffix)` characters,
   trim trailing `-` from that prefix, and append the suffix.

Every nickname includes the digest to disambiguate raw names, group/volume
boundaries and IQNs. It fits within 128 characters and `[a-z0-9][a-z0-9_-]*`.
Each session has a stanza with its target IQN and FQDN or selected VIP:

```
esan-my-volume-group-volume1-<16-hex-digest>-s1 {
	TargetName    = "iqn.2005-03.com.microsoft:<...>"
	TargetAddress = "portal.example"
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

The neutral markers and nickname contract are identical in the standalone
and Portal scripts, so each recognizes and preserves the other's entries.

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

`TargetName`/`TargetAddress` values containing a quote, brace, `#`, `;`, or a
control character are rejected, not escaped: the config grammar has no
documented escape for these statement-breaking characters.
Before formatting `TargetAddress`, the raw portal hostname must be a nonempty
ASCII hostname with valid DNS labels, and the raw port must be an integer from
1 through 65535 (not a Boolean or string). Port 3260 is omitted because it is
iscsid's default; other ports are retained. The final address must fit within
46 ASCII bytes or planning stops before any host change. DNS is queried only
for `--enable-vip-distribution`; hostnames are not otherwise rewritten.

We deliberately do not set `LoginTimeout`/`PingTimeout` in the generated
stanzas. `iscsi.conf(5)` exposes both, but this PoC has no evidence-based
"safe default" for either across Elastic SAN's network paths and simply
inherits FreeBSD's built-in defaults (`kern.iscsi.login_timeout` = 60s,
`kern.iscsi.ping_timeout` = 5s). Tune them explicitly in your own
`/etc/iscsi.conf` if your environment needs different values -- outside the
managed block, so they survive reruns.

### Atomic write, backup, and rollback

Mutating runs hold an exclusive `fcntl.flock` on `/etc/.azure-elastic-san.lock`
from the config read through host/session work and rollback. This separate inode
survives config replacement. The lock must be a regular non-symlink file, is
created `0600`, and uses `O_NOFOLLOW` where available. Both tools honor it;
external writers must coordinate. Dry-run neither creates nor acquires it.

While holding that lock, every write to `/etc/iscsi.conf` goes through: write a
temp file in the same directory → `fsync` → preserve the original file's
permissions and ownership (where the platform supports it) → `os.replace`
(atomic rename). Before any of that, if `/etc/iscsi.conf` already existed, its
exact current bytes are copied to a backup file
(`/etc/iscsi.conf.bak.pre-esan-connect.<pid>`, created with restrictive `0600`
permissions regardless of the original file's mode, since preserved unrelated
stanzas may contain CHAP secrets). The backup is **not** deleted automatically;
it's left behind for manual recovery.
New config files use mode `0600`; an existing file retains its mode and ownership.

Each volume is a config transaction. Connected sessions and their entries stay
when later adds fail; safely identifiable unconnected sessions added this run
are removed (see "Session count"). If none connect, that volume's pre-run config
is restored, preserving earlier volumes. The backup remains the exact pre-run
file. Ambiguous cleanup retains entries and stops rather than risk removing a
healthy session. Always inspect `iscsictl -L -v` before retrying a failed run.
Service enablement/start is not rolled back.

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
* A disconnected, disabled, or otherwise non-connected session skips the volume
  before any config or session change, with recovery guidance. The script
  neither adds a duplicate nor removes the stale session.
* With no session, the volume's stanzas are merged into the managed block. A
  nickname that already exists with a different target or address (for example
  a different session layout) skips that volume instead of adding a second layout.
  Only the exact planned entries can be re-established when no sessions exist.
* The script then submits `iscsictl -A -n <nickname> -c /etc/iscsi.conf`
  (without `-w`) for each session in order, and polls `iscsictl -L -v` for up
  to 30 seconds until that target IQN has that many `Connected` sessions.
  Unrelated disconnected sessions are ignored. On timeout, the partial-connection
  cleanup rule in "Session count" applies.

## Host prerequisites and boot login

The config is parsed before driver or service changes. Privilege, platform,
tools and FreeBSD userland release are checked before discovery.
Driver setup detects loadable modules with `kldstat -q -n iscsi`, persists
`iscsi_load=YES` in `/boot/loader.conf` for them, and loads a missing driver.
A compiled-in driver needs no loader entry.
`iscsid_enable=YES` is written only if needed; `service iscsid start` runs only
when `service iscsid onestatus` says it is stopped. The service is never restarted.

With stock `iscsictl_flags=-Aa` (or undefined), `iscsictl_enable=YES` enables
boot re-login. Customized flags, including an explicit empty value, remain
unchanged and generate WARNs. An empty value only lists sessions.
The boot service is enabled, never started by this run.
It processes **every** configured stanza at boot, not only managed entries;
use `Enable = Off` for unrelated stanzas that must remain manual.

## Recommended settings and validation

No Elastic SAN tuning value has a documented, safe FreeBSD equivalent.
The settings step prints one notice and changes no tuning. The
`--skip-recommended-settings` flag is accepted for CLI parity and has no effect.
Header and data digests remain CRC32C. Nothing requires an immediate reboot.

After connecting, read-only validation prints PASS/WARN/FAIL checks and totals.
Host checks require `/dev/iscsi`, enabled/running-service configuration and
stock boot-login configuration; customized boot flags WARN rather than change policy.
For volumes connected this run, fewer live sessions or managed entries than
requested and non-CRC32C digests FAIL. These problems only WARN for skipped
volumes, which the script never repairs. Extra sessions or entries WARN.
Inventory/config read failures FAIL once; dependent checks are omitted.
Exit 0 means no FAILs (WARNs may remain); any FAIL or operation error exits 1.

## Hosts with their own session manager

Use `--dry-run` to obtain the exact managed block and shell-quoted add commands.
Apply them through your own component, which must own configuration, login,
recovery and multipath assembly. Coordinate one writer per volume.
Do not run a host-owned manager alongside the stock boot session manager:
a normal run enables `iscsictl_enable=YES`, whose `iscsictl -Aa` processes all stanzas.

## Session count

`-s`/`--num-of-sessions` accepts 1 through 32 and defaults to 32, like the
Linux and Windows scripts. Other values stop before Azure discovery or local
mutation; they are never silently capped.

The FreeBSD kernel rejects a new session whose configured target name **and**
target address text both match an existing session (`EBUSY`, reported by
`iscsictl -A` as `Device busy`). Stock FreeBSD therefore supports one session
per target portal; initiators that lift that restriction support more. **Total
sessions per volume must not exceed what the initiator allows to one target
portal**, with or without VIP distribution: the service can redirect sessions
onto the same portal. On FreeBSD a redirect rewrites the session's address and
the kernel rejects a duplicate target+address, including during that rewrite.

| Initiator | Total sessions per volume, with or without VIP distribution |
| --- | --- |
| Stock FreeBSD | `-s 1` |
| Allows K sessions to one target portal | `-s K` |

VIP distribution with `-s 1` adds nothing over FQDN mode. It saves a literal IP
instead of the service name when DNS returns multiple VIPs.

After a synchronous add rejection, no further sessions are added for that volume.
The bounded readiness wait still runs. Connected sessions and their entries stay;
unconnected sessions are removed only when their configured target+portal can be
removed without touching a Connected session. Native `iscsictl -R -n` selects
every session with that target+portal, not an individual nickname. Ambiguous
cleanup fails closed, retains the entries and requires quiescing I/O and using
the disconnect script. A partial connection is reported as fewer than requested;
When at least one session connected, re-run with that count (or the initiator's
supported limit). When none connected, inspect `iscsictl -L -v`, reachability,
and initiator support before retrying; `-s 0` is not supported.

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

If the SAN's VIPs change (account migration), saved IP portals stop working.
Run disconnect, then connect again.

## Multipath

Each session exposes its own disk device; a runtime notice reminds you that
the script does not assemble multipath. Use the host's multipath layer so one volume's
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

Validate these limits against your exact initiator and backend before production:

1. **Azure CLI on FreeBSD is community-maintained.** Presence of `az` and the
   extension does not establish API/packaging compatibility or first-class support.
2. **Login redirects are not qualified.** Upstream `iscsid` implements
   RFC 3720/7143 status-class-1 redirects, but target-specific interoperability
   must be verified. Failures can cause session churn or silent connectivity loss.
   Readiness follows target IQNs, not original portals, to permit valid redirects.
3. **Multipath assembly is not automated.** Multiple sessions are separate disks,
   not proof of working MPIO or final zone placement.
4. **Multi-session limits depend on the initiator.** Stock FreeBSD allows one;
   K-session initiators need their own interoperability/failure testing.
5. **Native and E2E qualification remains required.** Unit tests do not establish
   suffix parsing/routing, native login/redirect behavior, module loading, boot
   re-login, dual-stack operation, zone-failure recovery or a 30-second objective.
   Qualify both FreeBSD 13.x and 14.x before relying on them.

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
