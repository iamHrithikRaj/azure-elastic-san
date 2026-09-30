# Windows standalone connection scripts

`connect.ps1` uses the existing authenticated Azure PowerShell context; it does
not log in, change subscriptions, install modules or require Azure CLI.
`-NumSession` accepts 1-32 in every mode and defaults to 32.

| Switches | Target IQN | Original login portal |
| --- | --- | --- |
| Neither | Plain | FQDN; unchanged legacy behavior |
| `-EnableZonalAffinity` | `:az-<physicalZone>` suffix | FQDN |
| `-EnableVipDistribution` | Plain; no IMDS or zone mapping | IPv4 VIPs, or FQDN for one address |
| Both | `:az-<physicalZone>` suffix | IPv4 VIPs, or FQDN for one address |

```powershell
.\connect.ps1 -ResourceGroupName rg -ElasticSanName san -VolumeGroupName vg `
    -VolumeName volume1,volume2 -NumSession 32 -EnableVipDistribution
```

Add `-EnableZonalAffinity` independently when zonal IQN routing is wanted and
supported by the front end. Mapping retains subscription/region checks, bounded
IMDS/provider reads and whole-batch decorated-IQN validation from ADO 39689651.

## VIP distribution and reboot

VIP mode resolves the volume FQDN with `System.Net.Dns.GetHostAddresses`, keeps
IPv4 only, removes duplicates and sorts numerically. No IPv4 addresses is a
clear DNS error before any iSCSI change. One distinct IPv4 keeps the **FQDN**
for LRS, private endpoints and migrations. With two or more addresses, session
`i` uses `VIP[i % N]`: 32 requests over three VIPs give 11/11/10 original
portals, and over two give 16/16. Only portals used by the requested session
count are registered.

The script calls `AddTarget` once per used portal and saves each login with
`PersistentLoginTarget`, preserving the multipath and digest options.
**Reboot is required to establish the saved logins.** It does not poll for
live sessions after saving them.

Client pinning spreads saved original portals across the returned VIPs so they
do not all depend on a single zone's portal. It complements the front end's
ISID-hash redirect; the front end may redirect after login. Original portal
distribution is not a guarantee or verification of final session placement.

## Existing configuration and failures

With either opt-in, any live session or persistent login for the volume's plain
IQN or `<plain IQN>:az-*` causes a skip:
`already configured; run disconnect.ps1 first to change the layout`.
No layout proof, automatic repair, rebalance or disconnect is attempted.
All selected-volume discovery and DNS checks finish before configuration
changes. The initiator must already be running and multi-session use requires
MPIO; the script does not install or tune them.

Preflight is not a transaction. A native failure can leave saved entries for
this or earlier volumes. Inspect the selected targets before retrying, and do
not run concurrent connection tools against them.

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

## Tests and limits

Run from this directory:

```powershell
Invoke-Pester -Script .\connect.Tests.ps1, .\connect.Vip.Tests.ps1, .\disconnect.Tests.ps1 -EnableExit
```

Tests mock Azure, IMDS, DNS and native commands, including a
`PersistentLoginTarget` that never creates a live session. Persistent inventory
parsing is tested against an **assumed English `iscsicli ListPersistentTargets`
format**, not captured host output; unreadable or incomplete listings fail
explicitly. No native/reboot, backend redirect or zone-failure qualification is
claimed. Zonal routing still requires front-end suffix support.
