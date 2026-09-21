# Windows standalone zonal mapping and VIP distribution

`connect.ps1` remains an independently downloadable Azure PowerShell script.
Without `-EnableZonalAffinity`, its original execution block, FQDN behavior,
session-count handling and commands are unchanged.

## Opt-in mapping and distribution

Use an existing authenticated Azure PowerShell context for the Elastic SAN
subscription. The script does not log in, change the active subscription,
install modules, or introduce an Azure CLI dependency.

```powershell
.\connect.ps1 -ResourceGroupName rg -ElasticSanName san -VolumeGroupName vg `
    -VolumeName volume1,volume2 -EnableZonalAffinity
```

The existing opt-in now requires **exactly 32 sessions per volume**; omitting
`-NumSession` selects 32. Any other explicit enabled count fails before discovery
or mutation. There is no second VIP switch. Disabled mode still supports 1-32
sessions and retains its FQDN commands.

Enabled mode reads the VM subscription, region and logical availability zone
from IMDS with `Metadata: true`, proxy bypass, no redirects and a five-second
complete-response timeout. The existing context supplies the SAN subscription;
the VM and SAN must be in the same subscription and region.

All Azure resource reads use an explicit subscription and the existing
`DefaultProfile`. `Get-AzElasticSan` checks the SAN region;
`Invoke-AzRestMethod` reads the subscription-scoped ARM Locations API
(`2022-12-01`). Missing, malformed or duplicate locations/mappings fail closed,
including repeated logical or physical zones. Each provider read runs in an
in-process PowerShell pipeline with a 30-second result deadline and asynchronous
cancellation on timeout. This preserves real context objects rather than
serializing credentials to a child process or file. No unsupported `-AsJob`
or timeout parameter is passed to `Get-AzElasticSan*`. The caller fails at the
deadline without waiting for synchronous stop/disposal. An outstanding read
that ignores cancellation can finish in the background; its result is
discarded and a .NET callback disposes the pipeline when it terminates.
This bounds caller waiting, not the lifetime of an unresponsive read. The
worker has no connection/mutation commands and cannot resume the connection
path after timeout.

The script appends the exact `:az-<physicalZone>` suffix, lowercasing only
the physical-zone value. The **complete decorated IQN** must start with
`iqn.`, contain only lowercase ASCII letters, digits, `.`, `-` and `:`,
and fit within 223 UTF-8 bytes. An already decorated or invalid IQN is rejected.
The opaque service identity is never silently lowercased, trimmed or rewritten.
If the service returns a mixed-case/otherwise incompatible identity, the
front-end identity contract must be resolved before enabling this mode.

Every selected volume's mapping, IQN, FQDN, port, DNS answer and session plan is
validated before any native mutation. Duplicate names or shared raw IQNs fail.
A later volume's lookup or validation failure cannot connect an earlier volume.
The iSCSI initiator must already be running; multi-session use requires
installed Multipath I/O. No service, feature or registry setup is performed.

## DNS and allocation

The host-local asynchronous .NET resolver gets at most three independent
attempts, each with a five-second result-wait deadline, separated by one- and
two-second delays. A late resolver task can finish in the background; its
answer is discarded. Answers are never unioned across attempts, and there
is no FQDN fallback. A successful answer is reused only within this invocation
for volumes sharing the same hostname.

Each answer must contain exactly three unique usable endpoints after IPv4-mapped
IPv6 normalization and deduplication. IPv4 is dotted decimal; IPv6 is compressed
lowercase hexadecimal. Invalid extras, scoped addresses, unspecified, loopback,
multicast, link-local and broadcast addresses reject the entire answer. Private
and public unicast addresses are allowed. IPv4 sorts before IPv6, then by
unsigned packed network-order bytes, not address text.

The sorted endpoints receive deterministic round-robin **11/11/10** session
counts. The numeric original portal is supplied to every persistent login.
IPv6 is unbracketed because the Windows command passes host and port separately.
The service-provided port is preserved. DNS validation does not prove endpoint
ownership, reachability, or backend support for the decorated IQN.

## Existing state and failures

The whole batch's read-only existing-state preflight also finishes before
`AddTarget` or the first login. The inline Windows
[`ReportIScsiPersistentLoginsW`](https://learn.microsoft.com/windows/win32/api/iscsidsc/nf-iscsidsc-reportiscsipersistentloginsw)
inventory reads original persistent portals and optional native session mappings.
The script skips only a complete healthy 32-session layout with matching
decorated IQN, persistent port, initiator, multipath/digest options, 11/11/10
allocation and unique persistent-to-live session correlation. Empty state may
be connected. Partial, stale, extra, incompatible, undecorated, differently
decorated or ambiguous state refuses explicitly. There is no automatic repair,
migration, disconnect or rebalance.

A redirected **current endpoint is not proof of the original portal**. If the
optional native SessionId mapping is absent or cannot be correlated uniquely,
a later invocation refuses even when current endpoint counts appear correct.
During a new connection only, each accepted request can instead be associated
with the one newly observed live session. These original-portal observations
exist only for that invocation; they are not persisted in another state file.
Do not run concurrent connection tools against the same targets.

New commands use the decorated IQN for both `AddTarget` and
`PersistentLoginTarget`, retaining `Root\ISCSIPRT\0000_0`, the multipath
login flag and digest arguments. Native process failures, API failures and
unrecognized/localized terminal output stop execution. Successful English
`iscsicli` status means the request was accepted, not that a session is ready.
Each login is followed by at most five readiness observations with four
one-second sleeps; the next login is not submitted until the new live session,
persistent state and all previously observed sessions satisfy the plan.
Visible conflicting state fails instead of being treated as ordinary delay.
This bounds polling, **not the duration of an individual Windows inventory or
native CLI call**, which can block in the platform. Reboot persistence is not
established by this check.

Whole-batch preflight is **not a transaction**. After mutation begins, a failure
can leave earlier sessions or persistent entries. Inspect `Get-IscsiSession`,
`Get-IscsiConnection` and `iscsicli ListPersistentTargets`, then use an
operator-approved target-specific recovery procedure. Do not bulk logout on a
shared host. Disabling the opt-in is not cleanup.

## Disconnect

`disconnect.ps1` needs no zonal flag. After confirmation, it logs out every
live session matching a selected volume's plain IQN or `<plain IQN>:az-`
prefix (case-insensitive), removes each matching persistent login using its
recorded portal address and port, then removes each matching target name.
FQDN, IPv4 and IPv6 portals are supported, including persistent-only state
before reboot. The existing `ROOT\ISCSIPRT\0000_0` initiator and any-port
selection are retained. Similar-prefix unrelated targets are left alone.

Persistent inventory is parsed from English `iscsicli ListPersistentTargets`
output (`Total of ... persistent targets`, `Target Name`, `Address and Socket`).
Missing/malformed inventory or native command failure stops cleanup explicitly.
Tests cover the assumed text format only; real Windows output and native cleanup
still require authorized qualification. Cleanup is not transactional, so a
failure can leave some sessions or saved logins in place.

## Tests and qualification

From the repository root:

```powershell
powershell.exe -NoProfile -NonInteractive -Command "Invoke-Pester -Script '.\PSH (Windows) Multi-Session Connect Scripts\ElasticSanDocScripts0523' -EnableExit"
```

The Pester suites use inline Windows-owned data and mock IMDS, DNS, Azure and
native boundaries. They exercise the actual entrypoint, canonicalization and
retry/deadline behavior, batch refusal, 11/11/10 commands, persistent/session
proof, pending readiness, native errors, cooperative and non-cooperative local
provider-timeout pipelines, and deferred cleanup. The inline C# ABI is compiled
and decoded from synthetic memory without invoking the DLL. Tests need no
Azure access or native iSCSI mutation. The original
execution block is pinned by an LF-normalized SHA-256 fingerprint from upstream
`c0e39eedc46456e0f68b92d9f52f5177b5e42a60`, with literal golden native argv cases.

**Production enablement remains blocked** on Elastic SAN front-end suffix
parsing/stripping and same-zone routing, service identity compatibility, and
authorized native/end-to-end qualification. Unit tests do not qualify login
redirects, Windows reboot persistence, zone failover/RTO, IPv6 or NetApp.
These scripts do not enable those capabilities.

## Reconstruction provenance

Windows mapping layer ADO 39689651 reconstructs selected mapping helpers,
connection arguments and mapping/legacy tests from combined task 39679867,
source commit `d6e8d4822a3d0aa7dac8b7794ebb9c74862ac108`
(`feature/hrithikraj/esan-zrs-vip-distribution-39679867`).
The source `connect.ps1`, `connect.Tests.ps1` and Windows-relevant documentation
were split by concern, not cherry-picked unchanged. Complete-IQN validation,
duplicate-region/physical-zone rejection and bounded provider execution are
strengthened in that parent layer.

Windows VIP layer **ADO 39689652** reconstructs the same source's Windows-only
`ConvertTo-ZonalAddress` through `Test-ZonalLayout` helpers, numeric-portal
orchestration and corresponding tests/documentation. It preserves mapping
parent `a17ed6779163396298b87851edbaa202682c6697` and its provider bounds.
The mixed-OS source history is not imported; this is a concern-specific
adaptation, not an unchanged cherry-pick. Whole-batch input checks and the
legacy execution block remain intact.
