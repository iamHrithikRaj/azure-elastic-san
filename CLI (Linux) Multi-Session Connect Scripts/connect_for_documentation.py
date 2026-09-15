import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
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
PHYSICAL_ZONE_PATTERN = re.compile(r"^[a-z0-9.-]+$")
SUBSCRIPTION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class ZonalAffinityError(Exception):
    pass


class VipResolutionError(ValueError):
    """The local resolver could not establish the exact-three-VIP contract."""


DNS_ATTEMPT_TIMEOUT_SECONDS = 5
DNS_RETRY_DELAYS_SECONDS = (1, 2)
ZONAL_SESSION_COUNT = 32


def canonicalize_vips(addresses):
    """Keep this standalone helper consistent with the shared contract fixtures."""
    if sys.version_info < (3, 5):
        raise VipResolutionError("Zonal VIP resolution requires Python 3.5 or later")
    import ipaddress

    if not isinstance(addresses, (list, tuple)):
        raise VipResolutionError("DNS must return an address list")
    unique = {}
    for text in addresses:
        if not isinstance(text, str) or "%" in text:
            raise VipResolutionError("DNS returned an invalid or scoped IP address")
        # Older ipaddress versions accepted leading zeros. Apply the same
        # decimal-only rule to IPv4 and the dotted tail of an IPv6 address.
        if "." in text and re.fullmatch(
            r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}",
            text.rsplit(":", 1)[-1],
        ) is None:
            raise VipResolutionError("DNS returned an invalid IP address: {!r}".format(text))
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            raise VipResolutionError("DNS returned an invalid IP address: {!r}".format(text))
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if (
            address.is_unspecified
            or address.is_loopback
            or address.is_multicast
            or address.is_link_local
            or str(address) == "255.255.255.255"
        ):
            raise VipResolutionError("DNS returned an unusable unicast VIP: {}".format(address))
        unique[(address.version, address.packed)] = str(address)
    if len(unique) != 3:
        raise VipResolutionError(
            "Expected exactly three unique usable SLB VIPs; DNS returned {}".format(len(unique))
        )
    return [unique[key] for key in sorted(unique)]


def _lookup_target_addresses(hostname):
    # getaddrinfo has no portable timeout. A child process bounds native resolver
    # stalls; subprocess.run kills and reaps it on TimeoutExpired.
    worker = (
        "import json,socket,sys;"
        "print(json.dumps([r[4][0] for r in socket.getaddrinfo("
        "sys.argv[1],None,socket.AF_UNSPEC,socket.SOCK_STREAM)]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", worker, hostname],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=DNS_ATTEMPT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise OSError("Local DNS lookup failed: {}".format(detail or result.returncode))
    return json.loads(result.stdout.decode("utf-8"))


def resolve_target_vips(hostname, cache=None):
    if sys.version_info < (3, 5):
        raise VipResolutionError("Zonal VIP resolution requires Python 3.5 or later")
    if (
        not isinstance(hostname, str)
        or not hostname.strip(".")
        or re.fullmatch(r"[A-Za-z0-9_.-]+", hostname) is None
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
    if number_of_sessions != ZONAL_SESSION_COUNT:
        raise VipResolutionError("Zonal affinity requires exactly 32 sessions per volume")
    ordered = canonicalize_vips(vips)
    return [ordered[index % 3] for index in range(ZONAL_SESSION_COUNT)]


def validate_target_port(port):
    if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
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
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        raise ZonalAffinityError("{} failed to start: {}".format(description, error))

    result = []
    communication_errors = []

    def communicate():
        try:
            result.append(process.communicate())
        except (OSError, ValueError) as error:
            communication_errors.append(error)

    communication_thread = threading.Thread(target=communicate)
    communication_thread.daemon = True
    communication_thread.start()
    communication_thread.join(AZ_CLI_TIMEOUT_SECONDS)
    if communication_thread.is_alive():
        process.kill()
        communication_thread.join()
        raise ZonalAffinityError(
            "{} timed out after {} seconds".format(
                description, AZ_CLI_TIMEOUT_SECONDS
            )
        )
    if communication_errors:
        raise ZonalAffinityError(
            "{} failed while collecting output: {}".format(
                description, communication_errors[0]
            )
        )

    out, err = result[0]
    out = _decode_output(out)
    err = _decode_output(err).strip()
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
        if (
            isinstance(candidate_name, string_types)
            and candidate_name.strip().lower() == normalized_location
        ):
            region = candidate
            break

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
        mapped_physical_zone = mapped_physical_zone.strip()
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


def resolve_physical_zone(
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
    return map_logical_to_physical_zone(
        locations, elastic_san_location, logical_zone
    )


def decorate_target_iqn(target_iqn, physical_zone):
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

    # Provisional contract: the Elastic SAN front end must parse and strip this suffix
    # before zonal-affinity routing can be used in production.
    decorated_iqn = "{}:az-{}".format(target_iqn, normalized_physical_zone)
    if len(decorated_iqn.encode("utf-8")) > MAX_IQN_UTF8_BYTES:
        raise ZonalAffinityError(
            "Decorated target IQN exceeds the {}-byte UTF-8 limit".format(
                MAX_IQN_UTF8_BYTES
            )
        )
    return decorated_iqn

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


ISCSI_COMMAND_TIMEOUT_SECONDS = 30
ISCSI_READY_ATTEMPTS = 5
ISCSI_READY_DELAY_SECONDS = 1
ZONAL_RECOVERY_GUIDANCE = (
    " No automatic rollback or cleanup was attempted; node records or sessions may remain. "
    "Inspect 'sudo iscsiadm -m session -P 3' and 'sudo iscsiadm -m node --op show'. "
    "Have the storage administrator reconcile the selected target during an approved "
    "maintenance window before retrying; do not disconnect active workloads."
)


def _zonal_state_error(message):
    return ZonalAffinityError(message + ZONAL_RECOVERY_GUIDANCE)


def _run_zonal_iscsiadm(arguments, empty_message=None):
    # Noninteractive sudo and a fixed locale keep both execution and parsing bounded.
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "LANG": "C"})
    command = ["sudo", "-n", "iscsiadm"] + arguments
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=ISCSI_COMMAND_TIMEOUT_SECONDS, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _zonal_state_error("iSCSI command failed: {}".format(error))
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    # open-iscsi uses 21 for an empty inventory. Do not confuse permission,
    # daemon, or malformed-output failures with an empty machine.
    if result.returncode == 21 and empty_message:
        messages = (stdout + "\n" + stderr).strip()
        if messages in (empty_message, "iscsiadm: " + empty_message):
            return ""
    if result.returncode != 0:
        raise _zonal_state_error(
            "iSCSI command {} failed (exit {}): {}".format(
                arguments, result.returncode, stderr or "no diagnostic"
            )
        )
    if empty_message and not stdout.strip():
        raise _zonal_state_error("iSCSI inventory returned no evidence of its state")
    return stdout


def _get_zonal_storage_target(subscription, resource_group, san, group, volume):
    command = [
        "az", "elastic-san", "volume", "show", "-g", resource_group,
        "-e", san, "-v", group, "-n", volume,
        "--query", "storageTarget", "--output", "json",
    ]
    if subscription is not None:
        command.extend(["--subscription", subscription])
    try:
        target = json.loads(_run_az_command(command, "Volume target lookup"))
        return (
            target["targetIqn"], target["targetPortalHostname"],
            target["targetPortalPort"],
        )
    except (ValueError, KeyError, TypeError) as error:
        raise ZonalAffinityError("Invalid volume storage target: {}".format(error))


def build_zonal_plan(subscription, resource_group, san, group, volumes, count):
    if count != ZONAL_SESSION_COUNT:
        raise ZonalAffinityError("Zonal affinity requires exactly 32 sessions per volume")
    if not volumes:
        raise ZonalAffinityError("At least one volume must be selected")
    if sys.version_info < (3, 5):
        raise ZonalAffinityError("Zonal affinity requires Python 3.5 or later")
    zone = resolve_physical_zone(subscription, resource_group, san)
    plans, seen_volumes, seen_iqns = [], set(), {}
    for volume in volumes:
        if volume in seen_volumes:
            continue
        seen_volumes.add(volume)
        raw_iqn, hostname, port = _get_zonal_storage_target(
            subscription, resource_group, san, group, volume
        )
        if (
            not isinstance(raw_iqn, str)
            or re.fullmatch(r"iqn\.[a-z0-9.:-]+", raw_iqn) is None
            or ":az-" in raw_iqn
        ):
            raise ZonalAffinityError("Invalid or already decorated target IQN")
        iqn = decorate_target_iqn(raw_iqn, zone)
        port = validate_target_port(port)
        if (
            not isinstance(hostname, str)
            or not hostname.strip(".")
            or re.fullmatch(r"[A-Za-z0-9_.-]+", hostname) is None
        ):
            raise VipResolutionError("Invalid target portal hostname")
        identity = (hostname.lower(), port)
        if raw_iqn in seen_iqns:
            if seen_iqns[raw_iqn] != identity:
                raise ZonalAffinityError("Conflicting portal selections for " + raw_iqn)
            continue
        seen_iqns[raw_iqn] = identity
        plans.append(dict(volume=volume, raw_iqn=raw_iqn, iqn=iqn,
                          hostname=hostname, port=port))

    # Validate all discovery/IQN/port data before resolving any target, and
    # finish all DNS plans before inspecting or changing native state.
    cache = {}
    for plan in plans:
        plan["vips"] = resolve_target_vips(plan["hostname"], cache)
        plan["slots"] = allocate_vip_sessions(plan["vips"], count)
    return plans


def _state_host(host):
    import ipaddress
    if "%" in host:
        raise _zonal_state_error("Scoped portal address in iSCSI inventory")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Legacy FQDN records must remain visible, not be mistaken for emptiness.
        if re.fullmatch(r"[A-Za-z0-9_.-]+", host) is None:
            raise _zonal_state_error("Invalid portal in iSCSI inventory")
        return host.lower()
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


def _parse_state_portal(text, flat_node=False):
    match = re.fullmatch(r"(?:\[([^\]]+)\]|([^:\s]+)):(\d+),(-?\d+)", text)
    if match is None:
        raise _zonal_state_error("Cannot establish persistent portal: " + text)
    tpgt = int(match.group(4))
    # Released open-iscsi prints the signed unknown node TPGT (-1) through
    # PRIu16, which expands to %u on glibc. The scoped node --op show below
    # must still confirm node.tpgt = -1 before accepting an existing layout.
    if flat_node and tpgt == 4294967295:
        tpgt = -1
    if not -1 <= tpgt <= 65535:
        raise _zonal_state_error("Invalid target portal group tag")
    return (
        _state_host(match.group(1) or match.group(2)),
        validate_target_port(match.group(3)), tpgt,
    )


def _read_zonal_inventory(mode):
    output = _run_zonal_iscsiadm(
        ["-m", mode],
        "No active sessions." if mode == "session" else "No records found",
    )
    entries = []
    for line in output.splitlines():
        if not line.strip():
            continue
        if mode == "session":
            match = re.fullmatch(
                r"\S+: \[(\d+)\] (\S+) (\S+)(?: \((?:non-flash|flash)\))?", line.strip()
            )
            if match is None:
                raise _zonal_state_error("Cannot parse iSCSI session inventory")
            sid, portal, iqn = match.groups()
        else:
            match = re.fullmatch(r"(\S+) (\S+)", line.strip())
            if match is None:
                raise _zonal_state_error("Cannot parse iSCSI node inventory")
            portal, iqn = match.groups()
            sid = None
        entries.append(dict(
            sid=sid, portal=_parse_state_portal(portal, flat_node=mode == "node"), iqn=iqn
        ))
    if mode == "session" and len(set(e["sid"] for e in entries)) != len(entries):
        raise _zonal_state_error("Duplicate session IDs in inventory")
    return entries


def _selected_zonal_entries(plan, entries):
    return [
        entry for entry in entries
        if entry["iqn"] == plan["raw_iqn"]
        or entry["iqn"].startswith(plan["raw_iqn"] + ":az-")
    ]


def _node_arguments(plan, host, tpgt=None):
    portal = format_target_portal(host, plan["port"])
    if tpgt is not None:
        portal += "," + str(tpgt)
    return [
        "-m", "node", "--targetname", plan["iqn"], "--portal", portal,
        "--interface", "default",
    ]


def _read_zonal_node(plan, entry):
    # No interface filter on inspection: multiple iface records must be rejected.
    output = _run_zonal_iscsiadm([
        "-m", "node", "--targetname", entry["iqn"],
        "--portal", format_target_portal(*entry["portal"][:2]) + "," + str(entry["portal"][2]),
        "--op", "show",
    ])
    fields = {}
    for line in output.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key in fields:
            raise _zonal_state_error("Ambiguous selected node configuration")
        fields[key] = value
    expected = {
        "node.name": plan["iqn"],
        "node.tpgt": str(entry["portal"][2]),
        "iface.iscsi_ifacename": "default",
        "iface.transport_name": "tcp",
        "node.conn[0].port": str(plan["port"]),
        "node.startup": "automatic",
        "node.session.nr_sessions": str(plan["slots"].count(entry["portal"][0])),
        "node.conn[0].iscsi.HeaderDigest": "CRC32C",
        "node.conn[0].iscsi.DataDigest": "CRC32C",
    }
    if (
        any(fields.get(key) != value for key, value in expected.items())
        or _state_host(fields.get("node.conn[0].address", "")) != entry["portal"][0]
    ):
        raise _zonal_state_error("Conflicting or incomplete persistent node configuration")


def _read_zonal_session(entry):
    # A grouped -P listing suppresses repeated portals based on the CURRENT
    # endpoint. Query each SID separately: redirected sessions may share that
    # endpoint while having DIFFERENT original/persistent VIPs.
    output = _run_zonal_iscsiadm(["-m", "session", "-r", entry["sid"], "-P", "3"])

    def field(name):
        values = re.findall(r"^\s*" + re.escape(name) + r":\s*(.*?)\s*$", output, re.M)
        if len(values) != 1:
            raise _zonal_state_error("Missing or ambiguous session field: " + name)
        return values[0]

    target = field("Target").split()[0]
    if (
        target != entry["iqn"] or field("SID") != entry["sid"]
        or _parse_state_portal(field("Persistent Portal")) != entry["portal"]
        or field("Iface Name") != "default" or field("Iface Transport") != "tcp"
    ):
        raise _zonal_state_error("Session identity or original VIP cannot be proved")
    _parse_state_portal(field("Current Portal"))
    healthy = (
        field("iSCSI Connection State") == "LOGGED IN"
        and field("iSCSI Session State") == "LOGGED_IN"
        and field("Internal iscsid Session State") == "NO CHANGE"
        and field("HeaderDigest") == "CRC32C"
        and field("DataDigest") == "CRC32C"
    )
    disks = re.findall(r"Attached scsi disk\s+\S+\s+State:\s*(\S+)", output)
    hosts = re.findall(r"Host Number:\s*\d+\s+State:\s*(\S+)", output)
    return healthy and bool(disks) and all(s == "running" for s in disks) and hosts == ["running"]


def check_zonal_layout(plan, sessions, nodes):
    sessions = _selected_zonal_entries(plan, sessions)
    nodes = _selected_zonal_entries(plan, nodes)
    if not sessions and not nodes:
        return False
    expected = dict((vip, plan["slots"].count(vip)) for vip in plan["vips"])
    if (
        len(sessions) != ZONAL_SESSION_COUNT or len(nodes) != 3
        or any(e["iqn"] != plan["iqn"] or e["portal"][:2] not in
               [(vip, plan["port"]) for vip in plan["vips"]] for e in sessions + nodes)
        or any(sum(e["portal"][0] == vip for e in sessions) != count
               for vip, count in expected.items())
        or len(set(e["portal"][0] for e in nodes)) != 3
    ):
        raise _zonal_state_error(
            "Partial, extra, or conflicting state for " + plan["raw_iqn"]
        )
    for node in nodes:
        _read_zonal_node(plan, node)
    by_host = dict((node["portal"][0], node) for node in nodes)
    for session in sessions:
        node = by_host[session["portal"][0]]
        # Static records commonly have an unknown TPGT (-1). A known TPGT must
        # agree; the unique persistent portal and iface provide the correlation.
        if (
            node["portal"][2] not in (-1, session["portal"][2])
            or not _read_zonal_session(session)
        ):
            raise _zonal_state_error("Unhealthy or mismatched session for " + plan["iqn"])
    return True


def _wait_for_zonal_session(plan, previous, host):
    for attempt in range(ISCSI_READY_ATTEMPTS):
        entries = _selected_zonal_entries(plan, _read_zonal_inventory("session"))
        current = dict((entry["sid"], entry) for entry in entries)
        added = set(current) - set(previous)
        if (
            any(current.get(sid) != entry for sid, entry in previous.items())
            or len(added) > 1
            or any(entry["iqn"] != plan["iqn"] for entry in entries)
        ):
            raise _zonal_state_error("Session layout changed unexpectedly during login")
        if added:
            sid = next(iter(added))
            entry = current[sid]
            if entry["portal"][:2] != (host, plan["port"]):
                raise _zonal_state_error("New session did not retain its allocated VIP")
            if _read_zonal_session(entry):
                return sid, current
        if attempt + 1 < ISCSI_READY_ATTEMPTS:
            time.sleep(ISCSI_READY_DELAY_SECONDS)
    raise _zonal_state_error("Timed out waiting for a healthy session through " + host)


def connect_zonal_volume(plan):
    print("{} [{}]: Connecting with 32 zonal sessions".format(plan["volume"], plan["iqn"]))
    for host in plan["vips"]:
        arguments = _node_arguments(plan, host)
        _run_zonal_iscsiadm(arguments + ["--op", "new"])
        # Keep seed logins at one regardless of system-wide defaults. Persist
        # the full allocation only after all explicitly requested slots are up.
        for key, value in (
            ("node.startup", "manual"),
            ("node.session.nr_sessions", "1"),
            ("node.conn[0].iscsi.HeaderDigest", "CRC32C"),
            ("node.conn[0].iscsi.DataDigest", "CRC32C"),
        ):
            _run_zonal_iscsiadm(arguments + ["--op", "update", "-n", key, "-v", value])
    seeds, sessions = {}, {}
    for host in plan["slots"]:
        if host in seeds:
            arguments = ["-m", "session", "-r", seeds[host], "--op", "new"]
        else:
            arguments = _node_arguments(plan, host) + ["--login"]
        _run_zonal_iscsiadm(arguments)
        sid, sessions = _wait_for_zonal_session(plan, sessions, host)
        if host not in seeds:
            seeds[host] = sid
    for host in plan["vips"]:
        for key, value in (
            ("node.session.nr_sessions", str(plan["slots"].count(host))),
            ("node.startup", "automatic"),
        ):
            _run_zonal_iscsiadm(
                _node_arguments(plan, host)
                + ["--op", "update", "-n", key, "-v", value]
            )
    if not check_zonal_layout(
        plan, _read_zonal_inventory("session"), _read_zonal_inventory("node")
    ):
        raise _zonal_state_error("New zonal connection disappeared before verification")
    print("{} [{}]: Verified 32 healthy persistent sessions (11/11/10)".format(
        plan["volume"], plan["iqn"]
    ))


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
        sessions = _read_zonal_inventory("session")
        nodes = _read_zonal_inventory("node")
        connected = [check_zonal_layout(plan, sessions, nodes) for plan in plans]
        for plan, already_connected in zip(plans, connected):
            if already_connected:
                print("{} [{}]: Skipped; verified healthy persistent 11/11/10 layout".format(
                    plan["volume"], plan["iqn"]
                ))
            else:
                connect_zonal_volume(plan)
        return

    for volume_name in volume_names:
        target_iqn, target_hostname, target_port = get_iqns(
            elastic_san_subscription,
            resource_group_name,
            elastic_san_name,
            volume_group_name,
            volume_name,
        )
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
            "Opt in to provisional logical-to-physical availability-zone routing. "
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
