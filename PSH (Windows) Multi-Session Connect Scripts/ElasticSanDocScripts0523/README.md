# Windows standalone zonal mapping

`connect.ps1` remains an independently downloadable Azure PowerShell script.
Without `-EnableZonalAffinity`, its original execution block, FQDN behavior,
session-count handling and commands are unchanged.

## Opt-in mapping

Use an existing authenticated Azure PowerShell context for the Elastic SAN
subscription. The script does not log in, change the active subscription,
install modules, or introduce an Azure CLI dependency.

```powershell
.\connect.ps1 -ResourceGroupName rg -ElasticSanName san -VolumeGroupName vg `
    -VolumeName volume1,volume2 -NumSession 2 -EnableZonalAffinity
```

Both modes support 1-32 sessions per volume; omitting the count selects 32.
Mapping-only mode retains the target FQDN and port. It does not resolve VIPs,
require exactly 32 sessions, or implement 11/11/10 distribution.

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

Every selected volume's mapping, IQN, FQDN and port is validated before any
native mutation. Duplicate names or shared raw IQNs fail. A later volume's
lookup or validation failure therefore cannot connect an earlier volume.
The iSCSI initiator must already be running; multi-session use requires
installed Multipath I/O. No service, feature or registry setup is performed.

## Existing state and failures

Mapping-only mode retains an existing-target skip, not VIP inventory or
session-layout validation. An exact decorated live IQN is skipped, with an
explicit warning that session count and persistence were not verified.
An undecorated or differently decorated live target for any selected volume
is refused before mutation; no migration, rebalance or disconnect is attempted.
This layer cannot prove the absence of stale persistent-only entries.
Operators must inspect and reconcile the selected targets' live and persistent
state before enabling mapping or retrying. Do not run concurrent connection
tools against the same targets.

New commands use the decorated IQN for both `AddTarget` and
`PersistentLoginTarget`, retaining the existing initiator selection, multipath
login flag and digest arguments. Native process failures, API failures and
unrecognized/localized terminal output stop execution. Successful English
`iscsicli` status means the request was accepted, not that session readiness
or persistence after reboot was independently established.

Whole-batch preflight is **not a transaction**. After mutation begins, a failure
can leave earlier sessions or persistent entries. Inspect `Get-IscsiSession`,
`Get-IscsiConnection` and `iscsicli ListPersistentTargets`, then use an
operator-approved target-specific recovery procedure. Do not bulk logout on a
shared host. The existing disconnect script is unchanged and does not reliably
handle decorated IQNs; disabling the opt-in is not cleanup.

## Tests and qualification

From the repository root:

```powershell
powershell.exe -NoProfile -NonInteractive -Command "Invoke-Pester -Script '.\PSH (Windows) Multi-Session Connect Scripts\ElasticSanDocScripts0523\connect.Tests.ps1' -EnableExit"
```

The Pester suite uses inline Windows-owned data, mocks Azure/native boundaries,
and exercises the actual entrypoint, cooperative and non-cooperative local
provider-timeout pipelines, and deferred cleanup. It needs no Azure access or
native iSCSI mutation. The original
execution block is pinned by an LF-normalized SHA-256 fingerprint from upstream
`c0e39eedc46456e0f68b92d9f52f5177b5e42a60`, with literal golden native argv cases.

**Production enablement remains blocked** on Elastic SAN front-end suffix
parsing/stripping and same-zone routing, service identity compatibility, and
authorized native/end-to-end qualification. Unit tests do not qualify login
redirects, Windows reboot persistence, zone failover/RTO, IPv6 or NetApp.
This connect-only layer does not enable those capabilities.

## Reconstruction provenance

Windows mapping layer ADO 39689651 reconstructs selected mapping helpers,
connection arguments and mapping/legacy tests from combined task 39679867,
source commit `d6e8d4822a3d0aa7dac8b7794ebb9c74862ac108`
(`feature/hrithikraj/esan-zrs-vip-distribution-39679867`).
The source `connect.ps1`, `connect.Tests.ps1` and Windows-relevant documentation
were split by concern, not cherry-picked unchanged. Complete-IQN validation,
duplicate-region/physical-zone rejection and bounded provider execution are
strengthened here. VIP DNS/allocation, persistent-login interop and exact-layout
orchestration are deliberately deferred to Windows VIP layer 39689652.
