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
def get_iqns(subscription, resource_group_name, elastic_san_name, volume_group_name, volume_name):
    check_azcli()
    subscription = " --subscription "+subscription if subscription is not None else ""
    command = "az elastic-san volume show -g {} -e {} -v {} -n {} --query storageTarget{}".format(resource_group_name, elastic_san_name, volume_group_name, volume_name, subscription).split(' ')
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


def resolve_cli_subscription_id(subscription):
    command = ["az", "account", "show"]
    if subscription is not None:
        command.extend(["--subscription", subscription])
    command.extend(["--query", "id", "--output", "tsv"])
    subscription_id = _run_az_command(
        command, "Azure CLI subscription resolution"
    ).strip()
    if not subscription_id:
        raise ZonalAffinityError("Azure CLI subscription resolution returned an empty ID")
    return canonicalize_subscription_id(subscription_id, "Azure CLI")


def get_azure_locations(subscription_id):
    subscription_id = canonicalize_subscription_id(subscription_id, "Azure CLI")
    url = (
        "https://management.azure.com/subscriptions/{}/locations"
        "?api-version=2022-12-01"
    ).format(subscription_id)
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
            "Azure location REST response has no region matching IMDS location '{}'".format(
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
    physical_zone = None
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
        if mapped_logical_zone.strip() == normalized_logical_zone:
            physical_zone = mapped_physical_zone.strip()
            break

    if physical_zone is None:
        raise ZonalAffinityError(
            "Region '{}' has no availability-zone mapping for logical zone '{}'".format(
                region.get("name"), logical_zone
            )
        )
    return physical_zone


def resolve_physical_zone(subscription):
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

    cli_subscription_id = resolve_cli_subscription_id(subscription)
    if cli_subscription_id != imds_subscription_id:
        raise ZonalAffinityError(
            "Azure CLI subscription '{}' does not match VM subscription '{}'".format(
                cli_subscription_id, imds_subscription_id
            )
        )

    locations = get_azure_locations(cli_subscription_id)
    return map_logical_to_physical_zone(locations, location_name, logical_zone)


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


def connect_volumes(
    subscription,
    resource_group_name,
    elastic_san_name,
    volume_group_name,
    volume_names,
    number_of_sessions,
    enable_zonal_affinity=False,
):
    physical_zone = resolve_physical_zone(subscription) if enable_zonal_affinity else None
    for volume_name in volume_names:
        target_iqn, target_hostname, target_port = get_iqns(
            subscription,
            resource_group_name,
            elastic_san_name,
            volume_group_name,
            volume_name,
        )
        if enable_zonal_affinity:
            target_iqn = decorate_target_iqn(target_iqn, physical_zone)

        connected = check_connection(target_iqn, target_hostname, target_port)
        if connected:
            print('{} [{}]: Skipped as this volume is already connected'.format(volume_name, target_iqn))
            continue
        connect_volume(volume_name, target_iqn, target_hostname, target_port, number_of_sessions)


def main(argv=None):
    # check if iSCSI initiator is installed
    check_iscsi()
    
    # check if multipath-tools is installed
    check_mpio()
    
    # get command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription")
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
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    
    # parameters
    subscription = args.subscription
    resource_group_name = args.resource_group
    elastic_san_name = args.elastic_san
    volume_group_name = args.volume_group
    volume_names = args.volumes
    number_of_sessions = min(32, int(args.num_of_sessions)) if args.num_of_sessions is not None else 32 # default is 32, also the maximum allowed number of sessions
    
    if None in [resource_group_name, elastic_san_name, volume_group_name, volume_names]:
        raise Exception('Need to provide resource_group_name, elastic_san_name, volume_group_name, volume_names to connect to the ElasticSAN volume')

    connect_volumes(
        subscription,
        resource_group_name,
        elastic_san_name,
        volume_group_name,
        volume_names,
        number_of_sessions,
        args.enable_zonal_affinity,
    )


if __name__ == "__main__":
    main()
