# Linux standalone zonal-affinity mapping

ADO **39557586** adds opt-in logical-to-physical zone mapping to the standalone
Linux connect script. This layer still connects through the volume's **FQDN**
and retains the existing configurable session count (default 32, capped at 32).
It does not resolve or allocate VIPs, require exactly 32 sessions, change
disconnect scripts, or modify portal-generated scripts.

**This is a mapping proof of concept, not a production-ready native connector.**
The Elastic SAN front end must understand and strip the provisional IQN suffix
before canonical IQN parsing and implement same-zone routing. The script cannot
enable or verify that service capability.

## Use

Use an explicitly authorized Linux test VM in the **same subscription and
region** as the SAN. The VM must be availability-zone pinned. The script uses
the existing iSCSI initiator and multipath prerequisites and Azure CLI with the
`elastic-san` extension. Authenticate Azure CLI with access to the SAN, its
volumes, and the subscription Locations API before running. No extra Python
packages or shared runtime module are needed; use Python 3.5 or later.

```sh
python3 connect_for_documentation.py \
    --elastic-san-subscription '<SAN-subscription-name-or-ID>' \
    -g rg -e san -v vg -n volume1 volume2 -s 4 \
    --enable-zonal-affinity
```

`--subscription` remains an alias for `--elastic-san-subscription`. If omitted,
the enabled path resolves the active Azure CLI subscription once and pins all
subsequent discovery to that canonical subscription ID. Cross-subscription
connections are not supported. Omitting `--enable-zonal-affinity` preserves the
original discovery order, undecorated IQN, FQDN, skip behavior, and count
handling. The command above is not a dry run: native mutation follows preflight.

## Enabled discovery and preflight

1. Read VM subscription, region, and logical zone from IMDS, with `Metadata: true`,
   no HTTP proxy, and a five-second HTTP timeout.
2. Resolve the SAN subscription to a GUID and get its region using
   `az elastic-san show --elastic-san-name`. Reject VM/SAN subscription or region
   mismatches.
3. Fetch `availabilityZoneMappings` with `az rest` against the explicit
   `/subscriptions/<ID>/locations?api-version=2022-12-01` URL and `--output json`
   so the user's CLI output preference cannot change the parsed format. Do not use
   `az account list-locations --subscription`, which is unsupported.
4. Reject missing or malformed mappings, duplicate normalized region entries,
   duplicate trimmed logical zones, or an
   unmatched logical zone. Trim/lowercase the physical zone and require
   `[a-z0-9.-]+`.
5. Discover and snapshot **every selected volume's** IQN, FQDN, and port through
   the bounded Azure CLI path. Append the exact `:az-<physicalZone>` suffix:
   `iqn.example:volume` becomes `iqn.example:volume:az-eastus-az3`.
6. Validate the **complete** decorated IQN: only lowercase ASCII letters,
   digits, dots, colons, and hyphens, and at most **223 UTF-8 bytes**. Invalid or
   uppercase service identities fail; they are never silently rewritten.
   Require a nonempty safe hostname and an integer port from 1 through 65535.
7. Only after the complete selection passes, hand the snapshotted targets to
   the unchanged native connection helpers. A later discovery or IQN error
   causes zero calls to native inventory or connection helpers.

Every enabled Azure CLI invocation has a 30-second process wait. Output uses
private temporary files rather than pipes, so inherited output handles cannot
strand a background reader or delay collection until a descendant exits. On
timeout or interruption the Linux CLI process group is killed and reaping gets
at most another 30 seconds. A termination or cleanup failure is reported
explicitly, with no undecorated-IQN or alternate-subscription fallback.
These bounds do not change the legacy opt-out or native helper timeouts.
The read-only package-manager prerequisite checks keep their original
entrypoint ordering and run before mapping preflight.

## Native limitations and recovery

The legacy helper bodies and signatures are deliberately preserved in this
mapping-only layer. Their existing-state check is a simple textual match, not
proof of complete, healthy, persistent sessions. They do not establish original
portal identity across redirects or safely adopt/migrate an existing target.
Do not use this opt-in to migrate an already connected volume.

Known inherited connector defects also remain: the digest command construction
uses an empty string separator and can fail after login, the persisted session
count uses the decremented value, and some native command failures are not
checked. This layer does **not** claim to fix CRC32C setup, persistent session
counts, or native readiness. Those require separately scoped connector work
and authorized Linux qualification before rollout.

Preflight is not a transaction across volumes. A native failure can leave
earlier volumes connected and the failing volume partially configured. There
is no automatic rollback, disconnect, repair, or rebalance. Inspect the exact
target's live and persistent state using read-only, target-scoped procedures
before retrying; never bulk logout or delete on a shared host. Avoid concurrent
connection/configuration tools for the same target.

Existing disconnect scripts do not reliably handle decorated IQNs. Turning
off the opt-in does not clean up sessions. Cleanup and migration need a
separately approved operator procedure or follow-up implementation.

The dependent Linux VIP layer (ADO **39689656**) will own DNS normalization,
three-endpoint allocation, exact-32 sessions, and supported native inventory.
None of that behavior is included here. FE suffix support, real Linux
multi-session behavior, persistent reconnection after reboot, redirects,
dual-stack/backend compatibility, zone failure, and recovery-time qualification
remain rollout gates. Mocked unit tests cannot establish those properties.

## Offline tests

Run only this OS suite from the repository root:

```powershell
python -B -W error::ResourceWarning -m unittest discover -s '.\CLI (Linux) Multi-Session Connect Scripts' -p test_connect_for_documentation.py -v
```

The suite keeps the 23 historical mapping cases and adds focused full-IQN,
whole-selection preflight, subscription snapshot, malformed-input, timeout,
and opt-out ordering/count regressions. Azure discovery and all native mutation
boundaries are mocked; subprocess lifecycle cases use harmless local Python
processes as well as mocks. No live Azure resources, root privileges, DNS/VIP
fixtures, or another OS suite are required.

## Provenance

The four coherent Linux mapping commits were carried with `cherry-pick -x`:

- `fd1f513f05a4c1995fcda8db4c32d15dac076ccb`
- `ea36a31901238618ba3b9e6ad57d1e53c0f3417a`
- `5e265679beaa8b2864cd297400485a77f5405f59`
- `3430ed96bc1ae8d401fe29e574acfe379f58d1dc`

The subsequent adaptation adds full-selection preflight, pinned and bounded
volume discovery, full-IQN validation, duplicate-map checks, and focused
regressions; it is not an unchanged cherry-pick. Applicable mapping, recovery,
and qualification guidance was adapted from `docs/standalone-zonal-affinity.md`
in combined source `d6e8d4822a3d0aa7dac8b7794ebb9c74862ac108`, without bringing
its VIP implementation or cross-OS dependencies into this directory.
