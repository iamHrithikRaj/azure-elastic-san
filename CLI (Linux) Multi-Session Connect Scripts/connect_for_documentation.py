import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time

try:
    from urllib.request import ProxyHandler, Request, build_opener
    from urllib.error import HTTPError, URLError
except ImportError:
    from urllib2 import HTTPError, ProxyHandler, Request, URLError, build_opener

# for compatibility between python2 and python3 
if hasattr(__builtins__, 'raw_input'):
      input = raw_input

try:
    string_types = (basestring,)
except NameError:
    string_types = (str,)

IMDS_COMPUTE_URL = "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01"
IMDS_TIMEOUT_SECONDS = 5
AZ_CLI_TIMEOUT_SECONDS = 30
MAX_IQN_UTF8_BYTES = 223
PHYSICAL_ZONE_PATTERN = re.compile(r"^[a-z0-9.-]+\Z")
IQN_PATTERN = re.compile(r"^[a-z0-9.:-]+\Z")
SUBSCRIPTION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class ZonalAffinityError(Exception):
    pass


class VipResolutionError(ZonalAffinityError, ValueError):
    """The local resolver could not establish the exact-three-VIP contract."""


DNS_ATTEMPT_TIMEOUT_SECONDS = 5
DNS_RETRY_DELAYS_SECONDS = (1, 2)
ZONAL_SESSION_COUNT = 32


def _parse_ip_address(text):
    import ipaddress
    if not isinstance(text, str) or "%" in text:
        raise VipResolutionError("Invalid or scoped IP address")
    # Older ipaddress releases accepted leading zeros, including IPv6 tails.
    if "." in text and re.fullmatch(
        r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}",
        text.rsplit(":", 1)[-1],
    ) is None:
        raise VipResolutionError("Invalid IP address: {!r}".format(text))
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        raise VipResolutionError("Invalid IP address: {!r}".format(text))
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address


def _canonical_vip(text):
    address = _parse_ip_address(text)
    if (
        address.is_unspecified or address.is_loopback or address.is_multicast
        or address.is_link_local or str(address) == "255.255.255.255"
    ):
        raise VipResolutionError("Unusable unicast VIP: {}".format(address))
    return address


def canonicalize_vips(addresses):
    if sys.version_info < (3, 5):
        raise VipResolutionError("Zonal VIP resolution requires Python 3.5 or later")
    if not isinstance(addresses, (list, tuple)):
        raise VipResolutionError("DNS must return an address list")
    unique = {}
    for text in addresses:
        address = _canonical_vip(text)
        unique[(address.version, address.packed)] = str(address)
    if len(unique) != 3:
        raise VipResolutionError(
            "Expected exactly three unique usable SLB VIPs; DNS returned {}".format(len(unique))
        )
    return [unique[key] for key in sorted(unique)]


def _lookup_target_addresses(hostname):
    # A dedicated resolver child bounds getaddrinfo, which has no timeout API.
    worker = (
        "import json,socket,sys;"
        "print(json.dumps([r[4][0] for r in socket.getaddrinfo("
        "sys.argv[1],None,socket.AF_UNSPEC,socket.SOCK_STREAM)]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", worker, hostname],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=DNS_ATTEMPT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 or result.stderr.strip():
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise OSError("Local DNS lookup failed: {}".format(detail or result.returncode))
    return json.loads(result.stdout.decode("utf-8"))


def resolve_target_vips(hostname, cache=None):
    if (
        not isinstance(hostname, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", hostname) is None
    ):
        raise VipResolutionError("Invalid target portal hostname: {!r}".format(hostname))
    key = hostname.lower()
    if cache is not None and key in cache:
        return list(cache[key])
    for attempt in range(len(DNS_RETRY_DELAYS_SECONDS) + 1):
        try:
            vips = canonicalize_vips(_lookup_target_addresses(hostname))
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            if attempt == len(DNS_RETRY_DELAYS_SECONDS):
                raise VipResolutionError(
                    "Cannot resolve '{}' to exactly three unique usable SLB VIPs after "
                    "{} attempts: {}".format(hostname, attempt + 1, error)
                )
            time.sleep(DNS_RETRY_DELAYS_SECONDS[attempt])
        else:
            if cache is not None:
                cache[key] = tuple(vips)
            return vips


def allocate_vip_sessions(vips, number_of_sessions=ZONAL_SESSION_COUNT):
    if type(number_of_sessions) is not int or number_of_sessions != ZONAL_SESSION_COUNT:
        raise VipResolutionError("Zonal affinity requires exactly 32 sessions per volume")
    ordered = canonicalize_vips(vips)
    return [ordered[index % 3] for index in range(ZONAL_SESSION_COUNT)]


def validate_target_port(port):
    if isinstance(port, bool) or re.fullmatch(r"[0-9]+", str(port)) is None or not 1 <= int(port) <= 65535:
        raise VipResolutionError("Invalid target portal port: {!r}".format(port))
    return int(port)


def format_target_portal(host, port):
    return "{}:{}".format("[{}]".format(host) if ":" in host else host, validate_target_port(port))

# determine os and package manager type
package_manager = ''
if os.path.exists('/usr/bin/apt-get'):
    package_manager = 'apt'
elif os.path.exists('/usr/bin/yum'):
    package_manager = 'yum'
elif os.path.exists('/usr/bin/zypper'):
    package_manager = 'zypper'
else:
    raise OSError("cannot find a usable package manager")

# check if iSCSI initiator is installed
def check_iscsi():
    # check for each type of package manager
    if package_manager == 'apt':
        command = "dpkg -l open-iscsi".split(' ')
    elif package_manager == 'yum':
        command = "rpm -q iscsi-initiator-utils".split(' ')
    elif package_manager == 'zypper':
        command = "zypper search -i open-iscsi".split(' ')
    else:
        raise OSError("cannot find a usable package manager")
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _ = p.communicate()
    out = out.decode("utf-8")
    
    # if not found/installed, select to exit or continue anyway
    if (package_manager == "apt" and "ii  open-iscsi" not in out) or (package_manager == 'yum' and "iscsi-initiator-utils is not installed" in out) or (package_manager == 'zypper' and "open-iscsi" not in out):
        value = input("\033[93mWarning: iSCSI initiator is not installed or enabled. It is required for successful execution of this connect script. \nDo you wish to terminate the script to install it? \n[Y/Yes to terminate; N/No to proceed with rest of the steps]:\033[00m")
        while True:
            if value.lower() == 'yes' or value.lower() == 'y':
                sys.exit(1)
            elif value.lower() == 'no' or value.lower() == 'n':
                break
            else:
                value = input('\033[93m[Y/Yes to terminate; N/No to proceed with rest of the steps]:\033[00m')
           
# check if multipath-tools is installed
def check_mpio():
    # check for each type of package manager
    if package_manager == 'apt':
        command = "dpkg -l multipath-tools".split(' ')
    elif package_manager == 'yum':
        command = "rpm -q device-mapper-multipath".split(' ')
    elif package_manager == 'zypper':
        command = "zypper search -i multipath-tools".split(' ')
    else:
        raise OSError("cannot find a usable package manager")
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _ = p.communicate()
    out = out.decode("utf-8")
    
    # if not found/installed, select to exit or continue anyway
    if (package_manager == "apt" and "ii  multipath-tools" not in out) or (package_manager == 'yum' and "device-mapper-multipath is not installed" in out) or (package_manager == 'zypper' and "multipath-tools" not in out):
        value = input("\033[93mWarning: Multipath I/O is not installed or enabled. It is recommended for multi-session setup. \nDo you wish to terminate the script to install it? \n[Y/Yes to terminate; N/No to proceed with rest of the steps]:\033[00m")
        while True:
            if value.lower() == 'yes' or value.lower() == 'y':
                sys.exit(1)
            elif value.lower() == 'no' or value.lower() == 'n':
                break
            else:
                value = input('\033[93m[Y/Yes to terminate; N/No to proceed with rest of the steps]:\033[00m')

# check if azure cli is installed
def check_azcli():
    # check for each type of package manager
    if package_manager == 'apt':
        command = "dpkg -l azure-cli".split(' ')
    elif package_manager == 'yum':
        command = "rpm -q azure-cli".split(' ')
    elif package_manager == 'zypper':
        command = "zypper search -i azure-cli".split(' ')
    else:
        raise OSError("cannot find a usable package manager")
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _ = p.communicate()
    out = out.decode("utf-8")
    
    # if not found/installed, exit
    if (package_manager == "apt" and "ii  azure-cli" not in out) or (package_manager == 'yum' and "azure-cli is not installed" in out) or (package_manager == 'zypper' and "azure-cli" not in out):
        print("\033[93mWarning: Azure CLI is not installed or enabled. It is required for successful execution of this connect script. \n You need to install by following `https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux` and run:\n `az extension add -n elastic-san`\n `az login`\033[00m")
        sys.exit(1)
    
# get iqn info from the ElasticSAN
def get_iqns(elastic_san_subscription, resource_group_name, elastic_san_name, volume_group_name, volume_name):
    check_azcli()
    subscription_argument = " --subscription "+elastic_san_subscription if elastic_san_subscription is not None else ""
    command = "az elastic-san volume show -g {} -e {} -v {} -n {} --query storageTarget{}".format(resource_group_name, elastic_san_name, volume_group_name, volume_name, subscription_argument).split(' ')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # timeout in case the extension is not installed and prompts the user to install
    timeout = 10
    while p.poll() is None and timeout > 0:
     time.sleep(1)
     timeout -= 1
    if timeout<=0:
        raise Exception('Command took longer than 10s')
    out, err = p.communicate()
    if "error" in err.decode("utf-8").lower():
        raise Exception(err)
    out = out.decode("utf-8")
    storage_target = json.loads(out)
    target_iqn = storage_target["targetIqn"]
    target_portal_hostname = storage_target["targetPortalHostname"]
    target_portal_port = storage_target["targetPortalPort"]
    return target_iqn, target_portal_hostname, target_portal_port


def _decode_output(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _run_az_command(command, description):
    # File-backed output cannot deadlock on a full pipe or a descendant retaining
    # a pipe handle. No background reader survives a timeout.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                command, stdout=stdout, stderr=stderr, start_new_session=(os.name == "posix")
            )
        except OSError as error:
            raise ZonalAffinityError("{} failed to start: {}".format(description, error))
        try:
            process.wait(timeout=AZ_CLI_TIMEOUT_SECONDS)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            try:
                if os.name == "posix":
                    # az may be a shell wrapper; terminate its own group, not
                    # just the wrapper, without affecting the caller's group.
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass  # The process exited between the deadline and termination.
            except OSError as cleanup_error:
                raise ZonalAffinityError(
                    "{} interrupted; Azure CLI termination failed: {}".format(
                        description, cleanup_error
                    )
                )
            try:
                process.wait(timeout=AZ_CLI_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                raise ZonalAffinityError(
                    "{} timed out; Azure CLI did not exit after termination".format(description)
                )
            if isinstance(error, KeyboardInterrupt):
                raise
            raise ZonalAffinityError(
                "{} timed out after {} seconds".format(description, AZ_CLI_TIMEOUT_SECONDS)
            )
        try:
            # Snapshot each length so a surviving descendant cannot extend a read
            # indefinitely by continuing to append output after the CLI exits.
            stdout.seek(0)
            stderr.seek(0)
            out = _decode_output(stdout.read(os.fstat(stdout.fileno()).st_size))
            err = _decode_output(stderr.read(os.fstat(stderr.fileno()).st_size)).strip()
        except (OSError, UnicodeError) as error:
            raise ZonalAffinityError(
                "{} failed while collecting output: {}".format(
                    description, error
                )
            )
    if process.returncode != 0:
        detail = err if err else "exit code {}".format(process.returncode)
        raise ZonalAffinityError("{} failed: {}".format(description, detail))
    return out


def get_vm_compute_metadata():
    request = Request(IMDS_COMPUTE_URL, headers={"Metadata": "true"})
    opener = build_opener(ProxyHandler({}))
    try:
        response = opener.open(request, timeout=IMDS_TIMEOUT_SECONDS)
        try:
            payload = _decode_output(response.read())
        finally:
            response.close()
    except HTTPError as error:
        raise ZonalAffinityError("IMDS request failed with HTTP status {}".format(error.code))
    except URLError as error:
        raise ZonalAffinityError("IMDS request failed: {}".format(error.reason))
    except socket.timeout:
        raise ZonalAffinityError(
            "IMDS request timed out after {} seconds".format(IMDS_TIMEOUT_SECONDS)
        )

    try:
        compute = json.loads(payload)
    except ValueError as error:
        raise ZonalAffinityError("IMDS returned invalid JSON: {}".format(error))
    if not isinstance(compute, dict):
        raise ZonalAffinityError("IMDS compute metadata must be a JSON object")
    return compute


def _required_metadata_value(compute, field_name):
    value = compute.get(field_name)
    if not isinstance(value, string_types) or not value.strip():
        raise ZonalAffinityError(
            "IMDS compute metadata is missing a valid '{}' value".format(field_name)
        )
    return value.strip()


def canonicalize_subscription_id(subscription_id, source):
    if not isinstance(subscription_id, string_types):
        raise ZonalAffinityError("{} subscription ID must be a GUID".format(source))
    canonical_subscription_id = subscription_id.strip().lower()
    if SUBSCRIPTION_ID_PATTERN.match(canonical_subscription_id) is None:
        raise ZonalAffinityError("{} subscription ID must be a GUID".format(source))
    return canonical_subscription_id


def resolve_elastic_san_subscription_id(elastic_san_subscription):
    command = ["az", "account", "show"]
    if elastic_san_subscription is not None:
        command.extend(["--subscription", elastic_san_subscription])
    command.extend(["--query", "id", "--output", "tsv"])
    subscription_id = _run_az_command(
        command, "Elastic SAN subscription resolution"
    ).strip()
    if not subscription_id:
        raise ZonalAffinityError("Elastic SAN subscription resolution returned an empty ID")
    return canonicalize_subscription_id(subscription_id, "Elastic SAN")


def get_elastic_san_location(
    elastic_san_subscription_id, resource_group_name, elastic_san_name
):
    command = [
        "az",
        "elastic-san",
        "show",
        "-g",
        resource_group_name,
        "--elastic-san-name",
        elastic_san_name,
        "--subscription",
        elastic_san_subscription_id,
        "--query",
        "location",
        "--output",
        "tsv",
    ]
    location = _run_az_command(
        command, "Elastic SAN location lookup"
    ).strip()
    if not location:
        raise ZonalAffinityError("Elastic SAN location lookup returned an empty location")
    return location


def get_azure_locations(elastic_san_subscription_id):
    elastic_san_subscription_id = canonicalize_subscription_id(
        elastic_san_subscription_id, "Elastic SAN"
    )
    url = (
        "https://management.azure.com/subscriptions/{}/locations"
        "?api-version=2022-12-01"
    ).format(elastic_san_subscription_id)
    command = [
        "az",
        "rest",
        "--method",
        "get",
        "--url",
        url,
        "--output",
        "json",
    ]
    payload = _run_az_command(command, "Azure location REST query")
    try:
        response = json.loads(payload)
    except ValueError as error:
        raise ZonalAffinityError("Azure location REST query returned invalid JSON: {}".format(error))
    if not isinstance(response, dict):
        raise ZonalAffinityError("Azure location REST response must be a JSON object")
    locations = response.get("value")
    if not isinstance(locations, list):
        raise ZonalAffinityError(
            "Azure location REST response must contain a top-level 'value' array"
        )
    return locations


def map_logical_to_physical_zone(locations, location_name, logical_zone):
    normalized_location = location_name.strip().lower()
    region = None
    for candidate in locations:
        if not isinstance(candidate, dict):
            raise ZonalAffinityError("Azure location REST response contains a malformed region entry")
        candidate_name = candidate.get("name")
        if not isinstance(candidate_name, string_types) or not candidate_name.strip():
            raise ZonalAffinityError("Azure location REST response contains a malformed region entry")
        if candidate_name.strip().lower() == normalized_location:
            if region is not None:
                raise ZonalAffinityError(
                    "Azure location REST response contains duplicate region '{}'".format(
                        normalized_location
                    )
                )
            region = candidate

    if region is None:
        raise ZonalAffinityError(
            "Azure location REST response has no region matching Elastic SAN location '{}'".format(
                location_name
            )
        )

    if "availabilityZoneMappings" not in region:
        raise ZonalAffinityError(
            "Region '{}' is missing availabilityZoneMappings".format(region.get("name"))
        )

    mappings = region["availabilityZoneMappings"]
    if not isinstance(mappings, list) or not mappings:
        raise ZonalAffinityError(
            "Region '{}' has malformed availabilityZoneMappings".format(region.get("name"))
        )

    normalized_logical_zone = logical_zone.strip()
    zone_map = {}
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ZonalAffinityError("availabilityZoneMappings contains a malformed entry")
        mapped_logical_zone = mapping.get("logicalZone")
        mapped_physical_zone = mapping.get("physicalZone")
        if (
            not isinstance(mapped_logical_zone, string_types)
            or not mapped_logical_zone.strip()
            or not isinstance(mapped_physical_zone, string_types)
            or not mapped_physical_zone.strip()
        ):
            raise ZonalAffinityError("availabilityZoneMappings contains a malformed entry")
        mapped_logical_zone = mapped_logical_zone.strip()
        mapped_physical_zone = normalize_physical_zone(mapped_physical_zone)
        if mapped_logical_zone in zone_map:
            raise ZonalAffinityError(
                "availabilityZoneMappings contains duplicate logical zone '{}'".format(
                    mapped_logical_zone
                )
            )
        zone_map[mapped_logical_zone] = mapped_physical_zone

    if normalized_logical_zone not in zone_map:
        raise ZonalAffinityError(
            "Region '{}' has no availability-zone mapping for logical zone '{}'".format(
                region.get("name"), logical_zone
            )
        )
    return zone_map[normalized_logical_zone]


def resolve_zonal_affinity_context(
    elastic_san_subscription, resource_group_name, elastic_san_name
):
    compute = get_vm_compute_metadata()
    logical_zone = compute.get("zone")
    if not isinstance(logical_zone, string_types) or not logical_zone.strip():
        raise ZonalAffinityError(
            "VM is not availability-zone pinned; IMDS compute.zone is empty or missing"
        )
    logical_zone = logical_zone.strip()
    imds_subscription_id = canonicalize_subscription_id(
        _required_metadata_value(compute, "subscriptionId"), "IMDS"
    )
    location_name = _required_metadata_value(compute, "location")

    elastic_san_subscription_id = resolve_elastic_san_subscription_id(
        elastic_san_subscription
    )
    if elastic_san_subscription_id != imds_subscription_id:
        raise ZonalAffinityError(
            "Elastic SAN subscription '{}' does not match VM subscription '{}'".format(
                elastic_san_subscription_id, imds_subscription_id
            )
        )

    elastic_san_location = get_elastic_san_location(
        elastic_san_subscription_id, resource_group_name, elastic_san_name
    )
    if elastic_san_location.strip().lower() != location_name.strip().lower():
        raise ZonalAffinityError(
            "The VM and Elastic SAN must be in the same region."
        )

    locations = get_azure_locations(elastic_san_subscription_id)
    physical_zone = map_logical_to_physical_zone(
        locations, elastic_san_location, logical_zone
    )
    return elastic_san_subscription_id, physical_zone


def resolve_physical_zone(
    elastic_san_subscription, resource_group_name, elastic_san_name
):
    return resolve_zonal_affinity_context(
        elastic_san_subscription, resource_group_name, elastic_san_name
    )[1]


def normalize_physical_zone(physical_zone):
    if not isinstance(physical_zone, string_types):
        raise ZonalAffinityError("Physical zone must be a nonempty string")
    normalized_physical_zone = physical_zone.strip().lower()
    if (
        not normalized_physical_zone
        or PHYSICAL_ZONE_PATTERN.match(normalized_physical_zone) is None
    ):
        raise ZonalAffinityError(
            "Physical zone '{}' contains characters that are unsafe for an IQN suffix".format(
                physical_zone
            )
        )
    return normalized_physical_zone


def decorate_target_iqn(target_iqn, physical_zone):
    normalized_physical_zone = normalize_physical_zone(physical_zone)
    if not isinstance(target_iqn, string_types) or not target_iqn:
        raise ZonalAffinityError("Target IQN must be a nonempty string")
    # Provisional contract: the Elastic SAN front end must parse and strip this suffix
    # before zonal-affinity routing can be used in production.
    decorated_iqn = "{}:az-{}".format(target_iqn, normalized_physical_zone)
    try:
        iqn_byte_length = len(decorated_iqn.encode("utf-8"))
    except UnicodeEncodeError:
        raise ZonalAffinityError("Decorated target IQN is not valid UTF-8")
    if iqn_byte_length > MAX_IQN_UTF8_BYTES:
        raise ZonalAffinityError(
            "Decorated target IQN exceeds the {}-byte UTF-8 limit".format(
                MAX_IQN_UTF8_BYTES
            )
        )
    if IQN_PATTERN.match(decorated_iqn) is None:
        raise ZonalAffinityError(
            "Decorated target IQN contains unsafe or non-lowercase characters; "
            "the service IQN must not be rewritten"
        )
    return decorated_iqn


def get_mapped_volume_target(
    elastic_san_subscription_id, resource_group_name, elastic_san_name,
    volume_group_name, volume_name,
):
    command = [
        "az", "elastic-san", "volume", "show",
        "-g", resource_group_name, "-e", elastic_san_name,
        "-v", volume_group_name, "-n", volume_name,
        "--subscription", elastic_san_subscription_id,
        "--query", "storageTarget", "--output", "json",
    ]
    payload = _run_az_command(command, "Volume '{}' target lookup".format(volume_name))
    try:
        target = json.loads(payload)
    except ValueError as error:
        raise ZonalAffinityError(
            "Volume '{}' target lookup returned invalid JSON: {}".format(volume_name, error)
        )
    if not isinstance(target, dict):
        raise ZonalAffinityError("Volume '{}' storageTarget must be a JSON object".format(volume_name))
    for field in ("targetIqn", "targetPortalHostname"):
        if not isinstance(target.get(field), string_types) or not target[field]:
            raise ZonalAffinityError(
                "Volume '{}' storageTarget is missing a valid '{}'".format(volume_name, field)
            )
    hostname = target["targetPortalHostname"]
    # Reject unsafe discovery data before DNS or native inventory.
    if re.match(r"^[a-zA-Z0-9][a-zA-Z0-9.-]*\Z", hostname) is None:
        raise ZonalAffinityError("Volume '{}' has an unsafe target portal hostname".format(volume_name))
    port = target.get("targetPortalPort")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ZonalAffinityError("Volume '{}' has an invalid target portal port".format(volume_name))
    return target["targetIqn"], hostname, port


def preflight_zonal_affinity(
    elastic_san_subscription, resource_group_name, elastic_san_name,
    volume_group_name, volume_names,
):
    if sys.version_info < (3, 5):
        raise ZonalAffinityError("Zonal affinity requires Python 3.5 or later")
    selected_volumes = tuple(volume_names)
    subscription_id, physical_zone = resolve_zonal_affinity_context(
        elastic_san_subscription, resource_group_name, elastic_san_name
    )
    targets = []
    for volume_name in selected_volumes:
        target_iqn, hostname, port = get_mapped_volume_target(
            subscription_id, resource_group_name, elastic_san_name,
            volume_group_name, volume_name,
        )
        targets.append((
            volume_name,
            (decorate_target_iqn(target_iqn, physical_zone), hostname, port),
        ))
    return targets


def build_zonal_plan(subscription, resource_group, san, group, volumes, count):
    if type(count) is not int or count != ZONAL_SESSION_COUNT:
        raise ZonalAffinityError("Zonal affinity requires exactly 32 sessions per volume")
    if not volumes:
        raise ZonalAffinityError("At least one volume must be selected")
    selected = []
    for volume in volumes:
        if not isinstance(volume, string_types) or not volume.strip():
            raise ZonalAffinityError("Volume names must be nonempty strings")
        if volume not in selected:
            selected.append(volume)
    targets = preflight_zonal_affinity(subscription, resource_group, san, group, selected)
    plans, seen_iqns = [], {}
    for volume, (iqn, hostname, port) in targets:
        raw_iqn = iqn.rsplit(":az-", 1)[0]
        if ":az-" in raw_iqn:
            raise ZonalAffinityError("Already decorated service target IQN")
        identity = (hostname.lower(), port)
        if raw_iqn in seen_iqns:
            if seen_iqns[raw_iqn] != identity:
                raise ZonalAffinityError("Conflicting portal selections for " + raw_iqn)
            continue
        seen_iqns[raw_iqn] = identity
        plans.append(dict(
            volume=volume, raw_iqn=raw_iqn, iqn=iqn, hostname=hostname, port=port,
        ))
    # Finish all input/mapping/IQN checks before DNS and every DNS plan before
    # observing native state. The successful-answer cache lives for this call.
    cache = {}
    for plan in plans:
        plan["vips"] = resolve_target_vips(plan["hostname"], cache)
        plan["slots"] = allocate_vip_sessions(plan["vips"], count)
    return plans


SYSFS_CLASS_ROOT = "/sys/class"
ZONAL_RECOVERY_GUIDANCE = (
    " No automatic rollback or cleanup was attempted; node records or sessions may remain. "
    "Have the storage administrator inspect and reconcile only the selected target during "
    "an approved maintenance window before retrying; do not disconnect active workloads. "
    "A timed-out client does not prove that daemon-side work stopped."
)


def _zonal_state_error(message):
    return ZonalAffinityError(message + ZONAL_RECOVERY_GUIDANCE)


def _selected_zonal_entries(plan, entries):
    return [
        entry for entry in entries
        if entry["iqn"] == plan["raw_iqn"]
        or entry["iqn"].startswith(plan["raw_iqn"] + ":az-")
    ]


def _state_host(host):
    if ":" in host or re.fullmatch(r"[0-9.]+", host):
        address_text, separator, scope = host.partition("%")
        address = _parse_ip_address(address_text)
        if separator:
            # Native inventory may include unrelated scoped IPv6 nodes. Keep
            # the scope as identity; never strip it into a selected usable VIP.
            if address.version != 6 or re.fullmatch(r"[A-Za-z0-9_.@-]+", scope) is None:
                raise _zonal_state_error("Invalid scoped portal in iSCSI inventory")
            return str(address) + "%" + scope
        return str(address)
    # Keep legacy FQDN records visible so they cannot look like empty state.
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host) is None:
        raise _zonal_state_error("Invalid portal in iSCSI inventory")
    return host.lower()


def _parse_state_portal(text, flat_node=False):
    match = re.fullmatch(r"(?:\[([^\]]+)\]|([^\[\]\s]+)):([0-9]+),(-?[0-9]+)", text)
    if match is None:
        raise _zonal_state_error("Cannot establish persistent portal: " + text)
    unbracketed = match.group(2)
    # Upstream 2.1.11 omits brackets whenever the stored address contains a
    # dot, including IPv6 dotted tails. Scope this format to flat node output.
    if unbracketed and ":" in unbracketed and (not flat_node or "." not in unbracketed):
        raise _zonal_state_error("Unbracketed IPv6 portal in iSCSI inventory")
    tpgt = int(match.group(4))
    # This is only a flat-node printing artifact, never a live/session value.
    # Acceptance still requires signed node.tpgt=-1 in the full node record.
    if flat_node and tpgt == 4294967295:
        tpgt = -1
    if not -1 <= tpgt <= 65535:
        raise _zonal_state_error("Invalid target portal group tag")
    return (
        _state_host(match.group(1) or match.group(2)),
        validate_target_port(match.group(3)), tpgt,
    )


def _parse_zonal_nodes(output):
    nodes = []
    for line in output.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"(\S+)\s+(\S+)", line.strip())
        if match is None:
            raise _zonal_state_error("Cannot parse iSCSI node inventory")
        portal, iqn = match.groups()
        nodes.append(dict(iqn=iqn, portal=_parse_state_portal(portal, flat_node=True)))
    return nodes


def _parse_zonal_node_config(output):
    fields = {}
    for line in output.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key or key in fields:
            raise _zonal_state_error("Ambiguous selected node configuration")
        fields[key] = value
    return fields


def check_zonal_layout(plan, sessions, nodes):
    """Accept only an empty target or its complete kernel-ready persistent layout."""
    sessions = _selected_zonal_entries(plan, sessions)
    nodes = _selected_zonal_entries(plan, nodes)
    if not sessions and not nodes:
        return False
    expected = {vip: plan["slots"].count(vip) for vip in plan["vips"]}
    if (
        len(sessions) != ZONAL_SESSION_COUNT or len(nodes) != 3
        or len({s["sid"] for s in sessions}) != len(sessions)
        or any(e["iqn"] != plan["iqn"] or e["portal"][:2] not in
               [(vip, plan["port"]) for vip in plan["vips"]] for e in sessions + nodes)
        or any(sum(e["portal"][0] == vip for e in sessions) != count
               for vip, count in expected.items())
        or len({e["portal"][0] for e in nodes}) != 3
    ):
        raise _zonal_state_error("Partial, extra, or conflicting state for " + plan["raw_iqn"])
    for node in nodes:
        _validate_zonal_node(plan, node, expected[node["portal"][0]], "automatic")
    by_host = {node["portal"][0]: node for node in nodes}
    for session in sessions:
        node = by_host[session["portal"][0]]
        if (
            node["portal"][2] not in (-1, session["portal"][2])
            or not session["healthy"]
        ):
            raise _zonal_state_error("Unhealthy or mismatched session for " + plan["iqn"])
    return True


def _validate_zonal_node(plan, node, count, startup):
    fields = node.get("fields", {})
    required = {
        "node.name": plan["iqn"], "node.tpgt": str(node["portal"][2]),
        "iface.iscsi_ifacename": "default", "iface.transport_name": "tcp",
        "node.conn[0].port": str(plan["port"]),
        "node.startup": startup, "node.conn[0].startup": "manual",
        "node.session.nr_sessions": str(count),
        "node.conn[0].iscsi.HeaderDigest": "CRC32C",
        "node.conn[0].iscsi.DataDigest": "CRC32C",
    }
    if (
        any(fields.get(key) != value for key, value in required.items())
        or "node.conn[0].address" not in fields
        or any(key.startswith("node.conn[") and not key.startswith("node.conn[0].") for key in fields)
        or _state_host(fields.get("node.conn[0].address", "")) != node["portal"][0]
    ):
        raise _zonal_state_error("Conflicting or incomplete persistent node configuration")


def _sysfs_value(path):
    with open(path, encoding="ascii") as handle:
        value = handle.read(4097)
    if len(value) > 4096 or not value.strip() or "\n" in value.strip():
        raise _zonal_state_error("Missing or ambiguous sysfs attribute: " + path)
    return value.strip()


def _sysfs_identity(path):
    resolved = os.path.realpath(path)
    info = os.stat(resolved)
    return resolved, info.st_dev, info.st_ino


def _sysfs_names(path, pattern):
    names = sorted(os.listdir(path))
    if any(re.fullmatch(pattern, name) is None for name in names):
        raise _zonal_state_error("Unexpected sysfs inventory member in " + path)
    return names


def _snapshot_zonal_sessions(plans, allow_pending=False):
    """Read selected per-SID kernel evidence without daemon IPC or native tools."""
    root = SYSFS_CLASS_ROOT
    classes = set(os.listdir(root))
    required = {"iscsi_session", "iscsi_connection"}
    if not required.intersection(classes):
        return []
    if not required.issubset(classes):
        raise _zonal_state_error("Incomplete iSCSI sysfs class inventory")
    sessions_path = os.path.join(root, "iscsi_session")
    connections_path = os.path.join(root, "iscsi_connection")
    session_names = _sysfs_names(sessions_path, r"session[0-9]+")
    connection_names = _sysfs_names(connections_path, r"connection[0-9]+:[0-9]+")
    session_ids = {name[len("session"):] for name in session_names}
    if any(name[len("connection"):].split(":")[0] not in session_ids for name in connection_names):
        raise _zonal_state_error("Orphan connection in sysfs inventory")
    entries, observed = [], {}
    for name in session_names:
        path = os.path.join(sessions_path, name)
        iqn = _sysfs_value(os.path.join(path, "targetname"))
        observed[name] = (_sysfs_identity(path), iqn)
        entry = dict(sid=name[len("session"):], iqn=iqn)
        if not any(_selected_zonal_entries(plan, [entry]) for plan in plans):
            continue
        device_identity = _sysfs_identity(os.path.join(path, "device"))
        device = device_identity[0]
        host_device = os.path.dirname(device)
        host_name = os.path.basename(host_device)
        if os.path.basename(device) != name or re.fullmatch(r"host[0-9]+", host_name) is None:
            raise _zonal_state_error("Invalid session-to-host ancestry")
        host_path = os.path.join(root, "scsi_host", host_name)
        host_identity = _sysfs_identity(os.path.join(host_path, "device"))
        if host_identity[0] != host_device:
            raise _zonal_state_error("Mismatched session host device link")
        if _sysfs_value(os.path.join(host_path, "proc_name")) != "iscsi_tcp":
            raise _zonal_state_error("Only software iSCSI TCP is supported")
        if _sysfs_value(os.path.join(path, "ifacename")) != "default":
            raise _zonal_state_error("Selected session has a nondefault or unknown interface")
        tpgt_text = _sysfs_value(os.path.join(path, "tpgt"))
        if re.fullmatch(r"-?[0-9]+", tpgt_text) is None or not -1 <= int(tpgt_text) <= 65535:
            raise _zonal_state_error("Invalid session target portal group tag")
        names = [n for n in connection_names if n.startswith("connection" + entry["sid"] + ":")]
        expected_connection = "connection" + entry["sid"] + ":0"
        if names != [expected_connection]:
            raise _zonal_state_error("Selected session must have exactly one connection :0")
        connection = os.path.join(connections_path, expected_connection)
        connection_identity = _sysfs_identity(os.path.join(connection, "device"))
        if (
            os.path.dirname(connection_identity[0]) != device
            or os.path.basename(connection_identity[0]) != expected_connection
        ):
            raise _zonal_state_error("Mismatched connection-to-session device link")
        persistent = _sysfs_value(os.path.join(connection, "persistent_address"))
        persistent_port = validate_target_port(_sysfs_value(os.path.join(connection, "persistent_port")))
        current = _sysfs_value(os.path.join(connection, "address"))
        current_port = validate_target_port(_sysfs_value(os.path.join(connection, "port")))
        # Neither current address nor current port may replace original values.
        portal = (_state_host(persistent), persistent_port, int(tpgt_text))
        current_portal = (str(_canonical_vip(current)), current_port)
        states = (
            _sysfs_value(os.path.join(path, "state")),
            _sysfs_value(os.path.join(connection, "state")),
            _sysfs_value(os.path.join(host_path, "state")),
            _sysfs_value(os.path.join(connection, "header_digest")),
            _sysfs_value(os.path.join(connection, "data_digest")),
        )
        disks = []
        host_number = host_name[len("host"):]
        for target_name in sorted(os.listdir(device)):
            if not target_name.startswith("target"):
                continue
            if re.fullmatch(r"target" + host_number + r":[0-9]+:[0-9]+", target_name) is None:
                raise _zonal_state_error("Invalid session SCSI target ancestry")
            target_path = os.path.join(device, target_name)
            if os.path.dirname(os.path.realpath(target_path)) != device:
                raise _zonal_state_error("SCSI target is not a session descendant")
            prefix = target_name[len("target"):] + ":"
            for lun_name in sorted(os.listdir(target_path)):
                if re.fullmatch(r"[0-9]+:[0-9]+:[0-9]+:[0-9]+", lun_name) is None:
                    continue
                if not lun_name.startswith(prefix):
                    raise _zonal_state_error("Mismatched SCSI LUN ancestry")
                lun_path = os.path.join(target_path, lun_name)
                lun_identity = _sysfs_identity(lun_path)
                class_lun = os.path.join(root, "scsi_device", lun_name, "device")
                if (
                    os.path.dirname(lun_identity[0]) != os.path.realpath(target_path)
                    or _sysfs_identity(class_lun) != lun_identity
                ):
                    raise _zonal_state_error("Mismatched SCSI device link")
                disk_state = _sysfs_value(os.path.join(class_lun, "state"))
                block_path = os.path.join(lun_path, "block")
                try:
                    block_names = sorted(os.listdir(block_path))
                except FileNotFoundError:
                    if not allow_pending:
                        raise
                    block_names = []
                for disk in block_names:
                    disk_identity = _sysfs_identity(os.path.join(block_path, disk))
                    class_block = os.path.join(root, "block", disk)
                    if (
                        os.path.dirname(disk_identity[0]) != os.path.realpath(block_path)
                        or _sysfs_identity(class_block) != disk_identity
                        or _sysfs_identity(os.path.join(class_block, "device")) != lun_identity
                    ):
                        raise _zonal_state_error("Mismatched attached block device")
                    disks.append((lun_name, disk, disk_state, lun_identity, disk_identity))
        healthy = states == ("LOGGED_IN", "up", "running", "1", "1") and bool(disks)
        healthy = healthy and all(disk[2] == "running" for disk in disks)
        entry.update(
            portal=portal, current_portal=current_portal, healthy=healthy,
            identity=(device_identity, host_identity, connection_identity),
            states=states, disks=tuple(disks),
        )
        entries.append(entry)
    # Detect visible removal/replacement/membership races, including unselected
    # sessions changing identity into a selected target while it is inspected.
    if (
        session_names != _sysfs_names(sessions_path, r"session[0-9]+")
        or connection_names != _sysfs_names(connections_path, r"connection[0-9]+:[0-9]+")
        or any(observed[name] != (
            _sysfs_identity(os.path.join(sessions_path, name)),
            _sysfs_value(os.path.join(sessions_path, name, "targetname")),
        ) for name in session_names)
    ):
        raise _zonal_state_error("Session inventory changed during sysfs inspection")
    return entries


def _read_zonal_sessions(plans, allow_pending=False):
    try:
        entries = _snapshot_zonal_sessions(plans, allow_pending)
        again = _snapshot_zonal_sessions(plans, allow_pending)
        if entries != again:
            identity = lambda values: [
                (e["sid"], e["iqn"], e["portal"], e["current_portal"], e["identity"])
                for e in values
            ]
            if not allow_pending or identity(entries) != identity(again):
                raise _zonal_state_error("Session state changed during sysfs inspection")
            # A disk scan/state transition can settle on a later poll, but may
            # never turn a single, visibly changing sample into readiness proof.
            before = {entry["sid"]: entry for entry in entries}
            for entry in again:
                if before[entry["sid"]] != entry:
                    entry["healthy"] = False
        return again
    except (OSError, UnicodeError, ValueError) as error:
        raise _zonal_state_error("Cannot establish selected sysfs session state: {}".format(error))


ISCSI_COMMAND_TIMEOUT_SECONDS = 30
ISCSI_CLEANUP_TIMEOUT_SECONDS = 1
ISCSI_READY_ATTEMPTS = 5
ISCSI_READY_DELAY_SECONDS = 1
ISCSI_OUTPUT_LIMIT_BYTES = 1024 * 1024


def _run_zonal_iscsiadm(arguments, empty_message=None):
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "LANG": "C"})
    command = ["sudo", "-n", "iscsiadm"] + arguments
    # File-backed output avoids inherited pipe handles extending our deadline.
    # Kill only the direct client: its descendants may include a started daemon.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                env=environment, start_new_session=(os.name == "posix"),
            )
        except OSError as error:
            raise _zonal_state_error("iSCSI command failed to start: {}".format(error))
        try:
            process.wait(timeout=ISCSI_COMMAND_TIMEOUT_SECONDS)
            lengths = [os.fstat(handle.fileno()).st_size for handle in (stdout, stderr)]
            if any(length > ISCSI_OUTPUT_LIMIT_BYTES for length in lengths):
                raise _zonal_state_error("iSCSI command output exceeded its supported size")
            stdout.seek(0)
            stderr.seek(0)
            out = stdout.read(lengths[0]).decode("utf-8")
            err = stderr.read(lengths[1]).decode("utf-8").strip()
        except subprocess.TimeoutExpired:
            raise _zonal_state_error("iSCSI command timed out after {} seconds".format(
                ISCSI_COMMAND_TIMEOUT_SECONDS
            ))
        except (OSError, UnicodeError) as error:
            raise _zonal_state_error("iSCSI output collection failed: {}".format(error))
        finally:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass  # Direct child exited between poll and kill.
                except OSError as error:
                    raise _zonal_state_error("iSCSI client termination failed: {}".format(error))
                try:
                    process.wait(timeout=ISCSI_CLEANUP_TIMEOUT_SECONDS)
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise _zonal_state_error("iSCSI client could not be reaped: {}".format(error))
    if process.returncode == 21 and empty_message:
        if (out + "\n" + err).strip() in (empty_message, "iscsiadm: " + empty_message):
            return ""
    # Enumeration can warn and skip a failed stat. Even exit 0 + records is
    # incomplete proof when stderr is nonempty.
    if process.returncode != 0 or err:
        raise _zonal_state_error("iSCSI command {} failed (exit {}): {}".format(
            arguments, process.returncode, err or "no diagnostic"
        ))
    if empty_message and not out.strip():
        raise _zonal_state_error("iSCSI inventory returned no evidence of its state")
    return out


def _read_zonal_nodes(plans):
    # Source-audited open-iscsi 2.1.11 paths: unfiltered node list and node show
    # without an iface filter read/lock the active compiled DB, without daemon
    # IPC. These local lock/directory effects are allowed only after pure plans.
    nodes = _parse_zonal_nodes(_run_zonal_iscsiadm(["-m", "node"], "No records found"))
    for node in nodes:
        if any(_selected_zonal_entries(plan, [node]) for plan in plans):
            output = _run_zonal_iscsiadm([
                "-m", "node", "--targetname", node["iqn"],
                "--portal", format_target_portal(*node["portal"][:2]) + "," + str(node["portal"][2]),
                "--op", "show",
            ])
            node["fields"] = _parse_zonal_node_config(output)
    return nodes


def _preflight_zonal_state(plans):
    sessions = _read_zonal_sessions(plans)
    nodes = _read_zonal_nodes(plans)
    connected = [check_zonal_layout(plan, sessions, nodes) for plan in plans]
    if sessions != _read_zonal_sessions(plans) or nodes != _read_zonal_nodes(plans):
        raise _zonal_state_error("State changed during whole-selection preflight")
    return connected


def _node_arguments(plan, host):
    return [
        "-m", "node", "--targetname", plan["iqn"],
        "--portal", format_target_portal(host, plan["port"]), "--interface", "default",
    ]


def _wait_for_zonal_session(plan, previous, host):
    for attempt in range(ISCSI_READY_ATTEMPTS):
        entries = _read_zonal_sessions([plan], allow_pending=True)
        current = {entry["sid"]: entry for entry in entries}
        added = set(current) - set(previous)
        if (
            any(current.get(sid) != entry for sid, entry in previous.items())
            or len(added) > 1 or any(entry["iqn"] != plan["iqn"] for entry in entries)
        ):
            raise _zonal_state_error("Session layout changed unexpectedly during login")
        if added:
            sid = next(iter(added))
            entry = current[sid]
            if entry["portal"][:2] != (host, plan["port"]):
                raise _zonal_state_error("New session did not retain its allocated VIP")
            if entry["healthy"]:
                return sid, current
        if attempt + 1 < ISCSI_READY_ATTEMPTS:
            time.sleep(ISCSI_READY_DELAY_SECONDS)
    raise _zonal_state_error("Timed out waiting for a kernel-ready session through " + host)


def connect_zonal_volume(plan):
    print("{} [{}]: Connecting with 32 zonal sessions".format(plan["volume"], plan["iqn"]))
    for host in plan["vips"]:
        arguments = _node_arguments(plan, host)
        _run_zonal_iscsiadm(arguments + ["--op", "new"])
        for key, value in (
            ("node.startup", "manual"), ("node.conn[0].startup", "manual"),
            ("node.session.nr_sessions", "1"),
            ("node.conn[0].iscsi.HeaderDigest", "CRC32C"),
            ("node.conn[0].iscsi.DataDigest", "CRC32C"),
        ):
            _run_zonal_iscsiadm(arguments + ["--op", "update", "-n", key, "-v", value])
    nodes = _selected_zonal_entries(plan, _read_zonal_nodes([plan]))
    if (
        len(nodes) != 3
        or {n["portal"][:2] for n in nodes} != {(host, plan["port"]) for host in plan["vips"]}
        or any(n["iqn"] != plan["iqn"] for n in nodes)
        or _read_zonal_sessions([plan])
    ):
        raise _zonal_state_error("State changed while preparing seed nodes")
    for node in nodes:
        _validate_zonal_node(plan, node, 1, "manual")
    seeds, sessions = {}, {}
    for host in plan["slots"]:
        arguments = (["-m", "session", "-r", seeds[host], "--op", "new"]
                     if host in seeds else _node_arguments(plan, host) + ["--login"])
        _run_zonal_iscsiadm(arguments)
        sid, sessions = _wait_for_zonal_session(plan, sessions, host)
        if host not in seeds:
            seeds[host] = sid
    for host in plan["vips"]:
        for key, value in (
            ("node.session.nr_sessions", str(plan["slots"].count(host))),
            ("node.startup", "automatic"),
        ):
            _run_zonal_iscsiadm(_node_arguments(plan, host) + ["--op", "update", "-n", key, "-v", value])
    if _preflight_zonal_state([plan]) != [True]:
        raise _zonal_state_error("New zonal connection disappeared before verification")
    print("{} [{}]: Verified 32 kernel-ready persistent sessions (11/11/10)".format(
        plan["volume"], plan["iqn"]
    ))

# check if there are existing connections, if so exit and not connect again
def check_connection(target_iqn, target_portal_hostname, target_portal_port):
    command = "sudo iscsiadm -m session".split(' ')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    if "error" in err.decode("utf-8").lower():
        raise Exception(err)
    out = out.decode("utf-8")
    return "No active sessions." not in out and "{}:{},-1 {}".format(target_portal_hostname, target_portal_port, target_iqn) in out
        
# connect to volume with the specified number of sessions
def connect_volume(volume_name, target_iqn, target_portal_hostname, target_portal_port, number_of_sessions):    
    print("{} [{}]: Connecting to this volume".format(volume_name, target_iqn))
    # add target and attempt to register a session
    command = "sudo iscsiadm -m node --targetname {} --portal {}:{} -o new".format(target_iqn, target_portal_hostname, target_portal_port).split(' ')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    if err:
        raise Exception(err)
    command = "sudo iscsiadm -m node --targetname {} -p {}:{} -l".format(target_iqn, target_portal_hostname, target_portal_port).split(' ')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    if err:
        raise Exception(err)
    number_of_sessions-=1

    # get session id
    command = "sudo iscsiadm -m session".split(' ')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sessions, _ = p.communicate()
    sessions = sessions.decode("utf-8")
    session_id = ""
    for l in sessions.splitlines()[::-1]:
        s = l.split(' ')
        if s[2] == "{}:{},-1".format(target_portal_hostname, target_portal_port) and s[3] == target_iqn:
            session_id = s[1][1:-1]
            break

    # register remaining sessions
    command = "sudo iscsiadm -m session -r {} --op new".format(session_id).split(' ')
    for i in range(number_of_sessions):
        p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.communicate()
            
    # maintain persistent connection
    command = "sudo iscsiadm -m node --targetname {} --portal {}:{} --op update -n node.session.nr_sessions " \
              "-v {}".format(target_iqn, target_portal_hostname, target_portal_port, number_of_sessions).split(' ')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.communicate()

    # automatic startup
    command = "sudo iscsiadm -m node --targetname {} --portal {}:{} --op update -n node.startup -v automatic".format(
        target_iqn, target_portal_hostname, target_portal_port).split(' ')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.communicate()

    # enable data protection
    command = "sudo iscsiadm -m node --targetname () --portal {}:{} --op update -n node.conn[0].iscsi.HeaderDigest"\
              "-v CRC32C".format(target_iqn, target_portal_hostname, target_portal_port).split('')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.communicate()
    command = "sudo iscsiadm -m node --targetname () --portal {}:{} --op update -n node.conn[0].iscsi.DataDigest"\
              "-v CRC32C".format(target_iqn, target_portal_hostname, target_portal_port).split('')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.communicate()


def connect_volumes(
    elastic_san_subscription,
    resource_group_name,
    elastic_san_name,
    volume_group_name,
    volume_names,
    number_of_sessions,
    enable_zonal_affinity=False,
):
    if enable_zonal_affinity:
        plans = build_zonal_plan(
            elastic_san_subscription, resource_group_name, elastic_san_name,
            volume_group_name, volume_names, number_of_sessions,
        )
        connected = _preflight_zonal_state(plans)
        completed = []
        for plan, already_connected in zip(plans, connected):
            try:
                # Recheck immediately before this target's first change; this
                # detects visible races but is not an all-volume transaction.
                if _preflight_zonal_state([plan]) != [already_connected]:
                    raise _zonal_state_error("State changed after whole-selection preflight")
                if already_connected:
                    print("{} [{}]: Skipped; verified kernel-ready persistent 11/11/10 layout".format(
                        plan["volume"], plan["iqn"]
                    ))
                else:
                    connect_zonal_volume(plan)
                completed.append(plan["volume"])
            except ZonalAffinityError as error:
                raise ZonalAffinityError(
                    "Stopped at volume '{}'; earlier verified volumes: {}. {}".format(
                        plan["volume"], ", ".join(completed) or "none", error
                    )
                )
            except KeyboardInterrupt:
                print("Interrupted at volume '{}'; earlier verified volumes: {}.{}".format(
                    plan["volume"], ", ".join(completed) or "none", ZONAL_RECOVERY_GUIDANCE
                ), file=sys.stderr)
                raise
        return

    # Keep opt-out discovery interleaved with connection, as before.
    targets = (
        (volume_name, get_iqns(
            elastic_san_subscription, resource_group_name, elastic_san_name,
            volume_group_name, volume_name,
        ))
        for volume_name in volume_names
    )

    for volume_name, (target_iqn, target_hostname, target_port) in targets:
        connected = check_connection(target_iqn, target_hostname, target_port)
        if connected:
            print('{} [{}]: Skipped as this volume is already connected'.format(volume_name, target_iqn))
            continue
        connect_volume(volume_name, target_iqn, target_hostname, target_port, number_of_sessions)


def create_argument_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--elastic-san-subscription",
        "--subscription",
        dest="elastic_san_subscription",
        help=(
            "Elastic SAN resource subscription name or ID. "
            "--subscription is retained as a compatibility alias."
        ),
    )
    parser.add_argument("-g", "--resource-group")
    parser.add_argument("-e", "--elastic-san")
    parser.add_argument("-v", "--volume-group")
    parser.add_argument("-n", "--volumes", nargs='+')
    parser.add_argument("-s", "--num-of-sessions")
    parser.add_argument(
        "--enable-zonal-affinity",
        action="store_true",
        help=(
            "Opt in to provisional zonal routing through exactly three VIPs and 32 sessions. "
            "Requires Elastic SAN front-end support for parsing the IQN suffix."
        ),
    )
    return parser


def main(argv=None):
    # check if iSCSI initiator is installed
    check_iscsi()

    # check if multipath-tools is installed
    check_mpio()

    # get command line arguments
    parser = create_argument_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    
    # parameters
    elastic_san_subscription = args.elastic_san_subscription
    resource_group_name = args.resource_group
    elastic_san_name = args.elastic_san
    volume_group_name = args.volume_group
    volume_names = args.volumes
    if args.enable_zonal_affinity and args.num_of_sessions is not None:
        try:
            requested_sessions = int(args.num_of_sessions)
        except (TypeError, ValueError):
            raise ZonalAffinityError("Zonal affinity requires exactly 32 sessions per volume")
        if requested_sessions != ZONAL_SESSION_COUNT:
            raise ZonalAffinityError("Zonal affinity requires exactly 32 sessions per volume")
    number_of_sessions = min(32, int(args.num_of_sessions)) if args.num_of_sessions is not None else 32 # default is 32, also the maximum allowed number of sessions
    
    if None in [resource_group_name, elastic_san_name, volume_group_name, volume_names]:
        raise Exception('Need to provide resource_group_name, elastic_san_name, volume_group_name, volume_names to connect to the ElasticSAN volume')

    connect_volumes(
        elastic_san_subscription,
        resource_group_name,
        elastic_san_name,
        volume_group_name,
        volume_names,
        number_of_sessions,
        args.enable_zonal_affinity,
    )


if __name__ == "__main__":
    main()
