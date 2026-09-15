# Standalone zonal affinity and ZRS VIP distribution

ADO **39679867** extends the standalone connect scripts. It depends on the
FreeBSD implementation in **39568949** and carries forward the Linux mapping
prerequisite from **39557586**. This is not a change to portal-generated
scripts, the VM extension, deployment tooling, or the disconnect scripts.

## Behavior

| Mode | Windows | Linux | Stock FreeBSD |
| --- | --- | --- | --- |
| Opt-in absent | Existing FQDN behavior; default 32 sessions | Existing FQDN behavior; default 32 sessions | Existing FQDN behavior; exactly one session |
| Opt-in enabled | Map IQN and connect 32 sessions over three VIPs | Map IQN and connect 32 sessions over three VIPs | Complete read-only preflight, then explicitly refuse the unsupported 32-session operation |

Use `-EnableZonalAffinity` on Windows or `--enable-zonal-affinity` on Python.
There is no separate VIP-distribution switch. Enabled mode requires exactly
32 sessions: omitting the count selects 32, while explicitly requesting any
other count fails rather than being ignored or clamped. Disabled-mode count
handling and target portal FQDN arguments remain unchanged.

The scripts remain standalone downloads with no shared runtime module.
Python zonal mode requires Python 3.5 or later and only the standard library.
Windows uses Azure PowerShell, not a new Azure CLI dependency.

```powershell
.\connect.ps1 -ResourceGroupName rg -ElasticSanName san -VolumeGroupName vg `
    -VolumeName volume1,volume2 -EnableZonalAffinity
```

```sh
# Linux: establish the opted-in connections.
python3 connect_for_documentation.py -g rg -e san -v vg \
    -n volume1 volume2 --enable-zonal-affinity

# FreeBSD: read-only preflight, followed by an unsupported-operation error.
# Adding --dry-run does not turn this into a successful connection operation.
python3 connect_for_documentation.py -g rg -e san -v vg \
    -n volume1 --enable-zonal-affinity --dry-run
```

## Mapping and preflight

The enabled path reads the VM's logical zone, subscription, and location from
IMDS. It bypasses HTTP proxies for IMDS and supplies the required metadata
header. The SAN subscription and region must match the VM. The
subscription-scoped ARM Locations API maps the logical zone through
`availabilityZoneMappings`; missing, malformed, or duplicate mapping entries
fail explicitly.

The scripts append the same provisional `:az-<physical-zone>` IQN suffix,
normalized to lowercase and constrained to `[a-z0-9.-]+`. The resulting IQN
must fit within 223 UTF-8 bytes. **The Elastic SAN front end must support
parsing and stripping this suffix.** These client scripts cannot enable or
verify that server capability.

Every selected volume is discovered and validated before any persistent
configuration or session mutation. A DNS or mapping failure for a later
volume must not leave earlier volumes connected.

## DNS and allocation contract

The input is the volume's target portal hostname and port, not an ARM VIP
list. The script uses the host's configured local resolver; it does not
select a public DNS service or fall back to a hardcoded address.

Resolution permits **three attempts**, each with a **five-second lookup
deadline**, separated by **one-second and two-second delays**. The lookup
itself is bounded, not just the retry loop: Python isolates `getaddrinfo` in
a subprocess that is killed and reaped on timeout; Windows bounds its wait
on the asynchronous .NET resolver.

Each attempt must yield exactly three unique usable unicast IP addresses.
IPv4 and IPv6, including mixed-family sets and private addresses, are
accepted. Unspecified, loopback, multicast, link-local, scoped IPv6, broadcast,
and malformed values are rejected. An invalid extra value is not silently
discarded to make a set pass. An incomplete set is retried, but addresses
from different attempts are never accumulated.

Normalize IPv4-mapped IPv6 to IPv4 before deduplication. Output uses
dotted-decimal IPv4 and compressed lowercase IPv6. Sort by family (IPv4
first), then unsigned lexicographic network-byte order, not textual order.
The first two sorted addresses receive 11 sessions each and the third
receives 10. Session slot `i`, starting at zero, uses VIP `i % 3`.

For example, the answer:

```text
fd00::1, 10.0.0.10, ::ffff:10.0.0.2, 10.0.0.2
```

normalizes to:

```text
10.0.0.2   -> 11 sessions
10.0.0.10  -> 11 sessions
fd00::1    -> 10 sessions
```

The volume's port is preserved. Linux uses `[ipv6]:port` where combined
portal syntax requires brackets; Windows passes the literal host and port
separately. A successful resolution is reused for the same hostname within
one invocation, but is not persisted or reused across invocations.

DNS validation does not prove reachability, server ownership, or that an
address is genuinely an SLB. Native login and readiness results still matter.
The allocation describes the original requested portals; a backend login
redirect can legitimately change the current endpoint.

## Existing connections and failures

An enabled Windows/Linux run skips a volume only when it can prove the
complete healthy, persistent 32-session layout matches the planned IQN,
port, and 11/11/10 allocation. Existing undecorated or differently decorated
targets must not be mistaken for an empty volume. Partial, extra, unhealthy,
conflicting, or ambiguous state causes an explicit failure before mutation.
An uncorrelatable redirected endpoint is not proof of the original allocation.

Linux enabled mode supports the default TCP interface. It verifies the
original portal for each SID separately, along with negotiated CRC32C
digests, running SCSI devices, automatic node startup, and the full persistent
11/11/10 counts. It does not adopt custom-interface or incomplete node records.

Windows uses the existing `Root\ISCSIPRT\0000_0` initiator selection and
requires a running initiator plus installed Multipath I/O; it does not
enable services or install features. A read-only
[`ReportIScsiPersistentLoginsW`](https://learn.microsoft.com/windows/win32/api/iscsidsc/nf-iscsidsc-reportiscsipersistentloginsw)
interop helper reads original portals and optional native session mappings.
When Windows does not expose a unique persistent-to-live mapping, a later
run refuses to infer it from current endpoints, even if their counts look
correct. During a new connection, the script can instead correlate each
successful login request with the one newly observed session and validate
its persistence. Those observations are invocation-local, not saved as
another state database.

Windows checks the native process status and `iscsicli`'s terminal English
success message. Localized or unrecognized status output fails explicitly;
it is not treated as success.

No automatic disconnect, node deletion, rebalance, or partial-session repair
is performed. Inspect the selected target's live and persistent state before
retrying. On Linux use read-only `iscsiadm -m session -P 3` and a target-scoped
node listing; on Windows inspect `Get-IscsiSession`, its connections, and the
persistent login inventory.

A failure after native mutation begins can leave newly created sessions or
persistent entries. The scripts must report that failure rather than claim
success or promise transactional session rollback. Reconcile only the
specific target's state with an operator-approved recovery procedure; never
use a bulk logout or delete against a shared host. Do not run concurrent
connection/configuration tools against the same selected target.

## Why FreeBSD refuses

Stock FreeBSD's
[`iscsi_ioctl_session_add()`](https://github.com/freebsd/freebsd-src/blob/25985322095d073354d31431da34da1e6871cca5/sys/dev/iscsi/iscsi.c#L1978-L1998)
and
[`iscsi_ioctl_session_modify()`](https://github.com/freebsd/freebsd-src/blob/25985322095d073354d31431da34da1e6871cca5/sys/dev/iscsi/iscsi.c#L2198-L2230)
reject duplicate normal sessions with the same configured target-address
text and target IQN using `EBUSY`. Three canonical VIP:port endpoints permit
at most three such configured sessions, not 32.

The FreeBSD script still performs logical-to-physical mapping, IQN
decoration, exact-three-VIP resolution, and 11/11/10 allocation. If they
succeed, it reports that read-only allocation and exits nonzero with the
unsupported-operation explanation. The same applies to `--dry-run`. It
does not generate a misleading 32-session managed block, acquire the
mutation lock, write config/backups, change services, or mutate sessions.
Earlier mapping/DNS failures are reported at their stage.

Nicknames, initiator names, ISIDs, worker counts, MaxConnections, TPGTs,
aliases, alternate spellings, fake ports/IQNs, and redirects are not supported
workarounds. Future multi-session FreeBSD support needs a separately
qualified initiator/backend mechanism. Source review is not live NetApp
qualification, and session creation is not `gmultipath` assembly.

## Focused tests and qualification

From the repository root, run the Python suites separately because the two
OS test modules have the same filename:

```powershell
python -B -W error::ResourceWarning -m unittest discover -s '.\tests' -p test_standalone_zonal_contract.py -v
python -B -W error::ResourceWarning -m unittest discover -s '.\CLI (Linux) Multi-Session Connect Scripts' -p test_connect_for_documentation.py -v
python -B -W error::ResourceWarning -m unittest discover -s '.\CLI (FreeBSD) Multi-Session Connect Scripts' -p test_connect_for_documentation.py -v
powershell.exe -NoProfile -NonInteractive -Command "Invoke-Pester -Script '.\PSH (Windows) Multi-Session Connect Scripts\ElasticSanDocScripts0523\connect.Tests.ps1' -EnableExit"
```

The fixtures in `tests/standalone_zonal_affinity_cases.json` define shared
mapping, dual-stack normalization, ordering, and allocation cases. Tests
mock DNS, Azure discovery, and native connection boundaries; they do not
require root, live Azure resources, or production mutation.

Mocked tests cannot qualify real initiator multi-session behavior, persistent
reconnection after reboot, IPv6 connectivity, IQN suffix support, or backend
login redirects. Validate those on explicitly authorized Windows/Linux lab
hosts and the intended backend before production use. Azure CLI support on
FreeBSD and NetApp interoperability remain separate qualification gates.
