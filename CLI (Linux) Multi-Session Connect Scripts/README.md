# Linux standalone zonal VIP distribution

ADO **39689656** extends the Linux mapping layer (**39557586**) through the
existing `--enable-zonal-affinity` opt-in. Enabled connections require **32
sessions** through exactly **three numeric VIPs**, allocated **11/11/10** to the
decorated IQN. There is no second flag and no FQDN fallback. Omitting the opt-in
preserves the original discovery order, undecorated IQN, FQDN, configurable
session count (default and maximum 32), skip behavior, and legacy helper bodies.
No other OS, Portal generator, shared runtime module, or disconnect script is
changed by this layer.

**This is a provisional connector, not production qualification.** The Elastic
SAN front end must understand and strip the `:az-<physicalZone>` suffix before
canonical IQN parsing and implement same-zone routing. The script cannot enable
or verify that service capability.

## Use and supported scope

Use Python **3.5 or later** on an explicitly authorized Linux test VM, with the
existing iSCSI initiator/multipath prerequisites and Azure CLI `elastic-san`
extension. Authenticate Azure CLI before running. The VM must be zonal and in
the **same subscription and region** as the SAN.

```sh
python3 connect_for_documentation.py \
    --elastic-san-subscription '<SAN-subscription-name-or-ID>' \
    -g rg -e san -v vg -n volume1 volume2 -s 32 \
    --enable-zonal-affinity
```

This is **not a dry run**: connection changes follow preflight. Omitting `-s`
uses 32. Enabled values other than 32 fail before the legacy count clamp.
`--subscription` remains an alias for `--elastic-san-subscription`. If omitted,
the enabled path resolves the active CLI subscription once and pins subsequent
discovery to its canonical GUID. Cross-subscription connections are unsupported.

The source-audited native command paths are **upstream open-iscsi 2.1.11**.
The supported selected-session topology is software TCP (`iscsi_tcp`), explicit
`default` interface, and one connection `:0` per SID. Required sysfs evidence was
checked against Linux v6.12, with principal attributes also present in v5.15.
Missing evidence or unsupported topology refuses, rather than inferring defaults.
These source checks and mocks do **not** qualify arbitrary distribution patches,
sudo policies, kernels, or installed binaries. Native validation on each intended
platform remains necessary before rollout. No package, service, MPIO, global
configuration, credential, or database-root setup is added.

## Whole-selection preflight

1. Read VM subscription, region, and logical zone from IMDS with `Metadata: true`,
   proxy bypass, and a five-second HTTP timeout.
2. Resolve the SAN subscription to a GUID and its location with
   `az elastic-san show --elastic-san-name`. Reject subscription/region mismatch.
   Obtain `availabilityZoneMappings` with explicit-subscription
   `az rest .../subscriptions/<ID>/locations?api-version=2022-12-01`.
   Do not use the unsupported `az account list-locations --subscription`.
3. Snapshot every selected volume's IQN, hostname, and port. Reject missing or
   malformed mappings, duplicate normalized region entries or logical zones,
   unsafe hostnames/ports, conflicting aliases, already-decorated service IQNs,
   and invalid complete decorated IQNs. The IQN permits only lowercase ASCII
   letters, digits, dots, colons, and hyphens and at most **223 UTF-8 bytes**.
   Never rewrite an invalid service identity to make it pass.
4. Finish every input/mapping/IQN check before DNS. Resolve each hostname locally
   with at most **three independent five-second attempts**, with 1/2-second
   retry delays. A successful answer may be cached only within this invocation.
   Never union partial answers across attempts or discard invalid extra answers.
   Require exactly three distinct usable addresses after normalization:
   IPv4 dotted decimal, IPv6 compressed lowercase, mapped IPv6 normalized to IPv4.
   Reject scoped, unspecified, loopback, multicast, link-local, broadcast, malformed,
   nondecimal, or whitespace-padded answers. Private and public unicast are allowed.
   Sort IPv4 before IPv6, then unsigned network-order bytes; allocate 11/11/10.
5. Only after **all pure plans** pass, inspect session sysfs and the native node
   inventory. The narrowly allowed effects of node inspection are **local DB
   locking/lock artifacts and creation of missing database/lock directories**.
   No selected node-record edits, configuration edits, service/daemon startup,
   logins, or session changes are allowed before **every volume** passes its
   existing-state checks.
6. Re-read the full selected session proof and node configuration at the global
   barrier, then recheck the specific volume immediately before its first change.
   Visible identity/configuration changes refuse. These observations are not an
   atomic transaction or a guarantee against concurrent tools; do not configure
   the same targets concurrently.

Azure CLI discovery retains the mapping parent's file-backed output and bounded
30-second process waits. Its existing CLI-group termination and bounded reaping
behavior remain unchanged. Read-only package prerequisite checks retain their
original entrypoint ordering.

## Existing-state proof

Native observation uses **unfiltered `iscsiadm -m node`** and target/portal-scoped
node `--op show` **without an interface filter**, so extra interface records
cannot be hidden. It uses the installed command's compiled database location,
never guesses `/etc/iscsi` versus `/var/lib/iscsi`, and does not need a new root
flag. Exit code 21 means empty only with the exact expected diagnostic.
Unexpected stderr invalidates even exit-zero inventory: enumeration can warn
and skip unreadable entries. Malformed, duplicate, incomplete, or conflicting
selected records refuse.

Flat output supports flexible whitespace. Only the flat-node representation
`4294967295` is normalized to unknown TPGT `-1`; the full record must independently
confirm signed `node.tpgt = -1`. Live session tags never use that conversion.
Known node/session tags must agree. Structurally valid unrelated node records,
including loopback/link-local portals, remain visible without being adopted.
Native scoped IPv6 identity is preserved, and flat inventory accepts upstream's
unbracketed dotted-tail IPv6 format. Neither case relaxes DNS suitability or lets
a scoped address match a selected unscoped VIP.

Sessions are inspected **directly through per-SID sysfs**, never native flat
session listing or `-P 3` during preflight. Required evidence includes:

| Evidence | Required proof |
|---|---|
| Target and interface | Exact decorated IQN, explicit `default`, signed session TPGT |
| Original portal | Independent connection `persistent_address` and `persistent_port` matching its allocated VIP |
| Current portal | Separately read `address` and `port`; redirect convergence is allowed, never an original-portal fallback |
| Topology | Connection -> session -> host ancestry, class device links, one `:0` connection, actual session-descendant target/LUN/block associations |
| Kernel readiness | `LOGGED_IN`, connection `up`, `iscsi_tcp` host `running`, at least one attached block disk and all disk states `running` |
| Digests | Negotiated `header_digest = 1` and `data_digest = 1` (CRC32C) |
| Persistence | Exactly three default-TCP nodes, full 11/11/10 counts, `node.startup = automatic`, `node.conn[0].startup = manual`, both requested digests CRC32C |

Membership, identity, and repeated snapshots must agree. Pending new-session
health/disk scans can settle within the bounded readiness poll, but a changing
sample cannot report ready. Kernel readiness does **not** establish daemon
health, internal recovery state, or successful end-to-end I/O.

Skip only a complete matching **32-session / three-node / 11/11/10** layout.
Undecorated targets, other zones, FQDN records, partial connections, wrong
counts/ports/interfaces, unhealthy sessions, or ambiguous persistence refuse.
There is no automatic adoption, repair, disconnect, migration, or rebalance.

## Apply and recovery

After the global barrier, create one numeric-portal node per VIP, using brackets
only for IPv6 host:port syntax. Set **both startup fields to manual**, seed count
to one, and both digest requests to CRC32C. Re-read these settings before login.
Establish exactly one seed per VIP, wait for repeated kernel-ready proof, then
clone that VIP's **specific SID**, never an arbitrary existing session.

After all 32 sessions are ready, persist the **full** 11/11/10 allocations, not
allocation-minus-one, and enable node automatic startup. Verify the complete live
and persistent layout before reporting success.

Native commands use noninteractive sudo, a fixed locale, a 30-second client wait,
file-backed output, and a 1 MiB supported output limit per stream. Timeout or
interruption terminates **only the direct native client**, with a one-second
cleanup wait; it never kills the client's process group. A daemon-side operation
may continue after the client exits. Such a timeout is a failure, not proof of
cancellation or rollback.

A later failure can leave earlier volumes connected and the current volume
partially configured. Errors identify the failing volume and earlier verified
volumes. Inspect and reconcile only the selected target under an approved
maintenance procedure before retrying; never bulk logout or delete on a shared
host. The script performs no automatic cleanup.

Legacy helpers are deliberately unchanged, including their known digest-command,
decremented-persistence-count, and native-error-handling defects. The enabled VIP
path does not use those helpers. Turning off the opt-in is not cleanup; use the
explicit disconnect procedure below.

## Disconnect

Quiesce I/O and unmount the selected volumes before explicitly running
`disconnect_for_documentation.py` with its existing subscription/resource/volume
arguments. No zonal parameter is needed: it discovers both persistent node
records and live sessions, selecting the exact volume IQN and every target
starting with `<volume-IQN>:az-`. Each matching name is logged out and its node
records deleted **across all portals**, including FQDNs, VIP IPs, and old zones.
This removes automatic-startup records even when no sessions are active; other
target names are left alone. An empty inventory is harmless, while native
errors stop cleanup and can leave partial state. Inspect the selected targets
before retrying, and do not run concurrent connection tools. Turning off the
connect opt-in alone still does not clean up anything. Native disconnect and
reboot behavior require authorized Linux qualification.

## Offline validation and limits

From the repository root:

```powershell
python -B -W error::ResourceWarning -m unittest discover -s '.\CLI (Linux) Multi-Session Connect Scripts' -p 'test*.py'
```

The suite exercises the actual standalone entrypoints and helpers with mocked
IMDS/CLI/DNS/native boundaries, an argv-level initiator model, file-backed sysfs
attributes with portable class-link emulation, the local address corpus, and
harmless child-process lifecycle probes. It covers exact allocation, original
versus redirected portals, full persistence, TPGT/whitespace, all-volume
preflight, races, pending readiness, partial failures, and opt-out behavior.
It never runs a real initiator or connects a cloud resource.

FE suffix support, real initiator/daemon behavior, recovery after reboot,
redirects, backend dual stack, zone failure, RTO, and end-to-end I/O remain
unqualified. Mocked results are not native or production qualification.

## Provenance

This Linux-only adaptation reuses selected planning, allocation, state-proof,
connection, and fixture behavior from the preserved combined source:

- `fd03a7e2610f6e020ef57d81418f26a872f793f0` - inline VIP planning and address cases.
- `b084085baf18e8cb7b7b9acbd9fcacfa2574ed7a` - Linux VIP/node/SID allocation intent.
- `39e593bbbaab2d59eac9fda117709ed132a1a4ee` - flat-only unsigned node TPGT handling.
- Combined reference `d6e8d4822a3d0aa7dac8b7794ebb9c74862ac108` on
  [`iamHrithikRaj/azure-elastic-san`](https://github.com/iamHrithikRaj/azure-elastic-san/tree/feature/hrithikraj/esan-zrs-vip-distribution-39679867).

These are selective adaptations, not unchanged cherry-picks. Native session
probes were replaced by sysfs, unsafe inventory assumptions corrected, and
parent mapping/authentication/CLI/IQN behavior preserved. The small fixture is
owned by this Linux directory; no other OS's runtime or tests are imported.
The mapping parent retains its four original Linux mapping cherry-picks and
subsequent pinned-discovery/full-selection validation fixes.

Source anchors for the native observation contract:
[node list/show dispatch](https://github.com/open-iscsi/open-iscsi/blob/2.1.11/usr/iscsiadm.c#L750-L841),
[DB enumeration and partial-stat warnings](https://github.com/open-iscsi/open-iscsi/blob/2.1.11/libopeniscsiusr/node.c#L78-L210),
[DB initialization](https://github.com/open-iscsi/open-iscsi/blob/2.1.11/usr/idbm.c#L3136),
[persistent portal publication](https://github.com/open-iscsi/open-iscsi/blob/2.1.11/usr/initiator_common.c#L490-L506),
and [kernel iSCSI attributes](https://github.com/torvalds/linux/blob/v6.12/drivers/scsi/scsi_transport_iscsi.c#L4150-L4400).
