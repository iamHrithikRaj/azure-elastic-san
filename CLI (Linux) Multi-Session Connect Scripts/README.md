# Linux standalone connection options

Use Python 3.5 or later with the existing iSCSI/multipath prerequisites and an
authenticated Azure CLI with the `elastic-san` extension. Session count defaults
to 32; `-s` accepts smaller positive counts and caps larger values at 32.

| Options | IQN | Original portal selection |
|---|---|---|
| Neither opt-in | Plain | Volume FQDN |
| `--enable-zonal-affinity` | `:az-<physicalZone>` suffix | Volume FQDN |
| `--enable-vip-distribution` | Plain | IPv4 DNS selection below |
| Both | Decorated | IPv4 DNS selection below |

```sh
python3 connect_for_documentation.py \
  --elastic-san-subscription '<subscription>' -g rg -e san -v vg \
  -n volume1 volume2 -s 32 --enable-vip-distribution
```

Add `--enable-zonal-affinity` independently when supported by the service.
That option retains the mapping layer's all-selected-volume preflight using
IMDS and subscription/region availability-zone mappings; the VM must be zonal
and in the same subscription and region as the SAN. VIP distribution alone
does not query IMDS or zone mappings. `--subscription` remains an alias.

## VIP selection and redirects

VIP distribution resolves the FQDN with IPv4 `socket.getaddrinfo`, deduplicates
addresses, and sorts them numerically. No IPv4 answers produces a DNS error
before node creation or login. **One address keeps the original FQDN**, including
for LRS and private endpoints, so saved configuration remains migration-safe.
With **N addresses**, session shares are `count // N`, with one extra assigned
to each of the first `count % N` addresses: 32 over three gives 11/11/10; over two,
16/16. Zero-share addresses are only tried as single-session fallbacks if every
positive-share portal fails to log in.

Client pinning spreads **original saved portals**, not final session placement.
It complements the front end's ISID-hash redirect: the front end may redirect
any login, and this script neither controls nor verifies the resulting placement.
Spreading saved portals avoids depending on one zone for all initial connections;
it is not a guarantee of successful failover.

Each portal uses one seed login, SID-specific clones, CRC32C digest settings, and
the full persistent session count. The seed temporarily uses `nr_sessions=1`
because native login already honors that setting. Login failures warn and allow
other portals to proceed; the volume fails only if none logs in. Clone or
persistence failures also warn, so a usable connection may have fewer sessions
or incomplete saved settings. Native commands use ordinary `sudo`.

## Existing connections and limits

Any session or node record for the plain IQN or its `:az-*` variants skips the
volume: **already configured; run `disconnect_for_documentation.py` first to
change the layout**. No layout proof, repair, rebalance, or automatic cleanup is
performed. A failed attempt can leave node records or sessions behind. Review
warnings and use an approved disconnect/reconnect procedure; do not run competing
connection tools for the same volume.

Mocks do not establish native initiator, service redirect, reboot, or zone-failure
behavior. Zonal IQN routing requires front-end support. No cloud resources are
created by this change.

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

## Offline tests

```powershell
python -B -W error::ResourceWarning -m unittest discover -s '.\CLI (Linux) Multi-Session Connect Scripts' -p 'test*.py'
```
