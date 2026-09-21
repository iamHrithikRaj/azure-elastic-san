Param(
    [Parameter(Mandatory, 
    HelpMessage = "Resource group name")]
    [string]
    $ResourceGroupName,
    [Parameter(Mandatory,
    HelpMessage = "Elastic SAN name")]
    [string]
    $ElasticSanName,
    [Parameter(Mandatory,
    HelpMessage = "Volume group name")]
    [string]
    $VolumeGroupName,
    [Parameter(Mandatory,
    HelpMessage = "Volumes to be connected")]
    [string[]]
    $VolumeName,
    [Parameter(HelpMessage = "Number of sessions to be connected for each volume. Default value is 32. Input value should be in range of 1-32.")]
    [ValidateRange(1,32)]
    [int]
    $NumSession,
    [Parameter(HelpMessage = "Opt in to subscription-scoped zonal IQN mapping and 32 sessions across exactly three VIPs (11/11/10). Requires backend zonal IQN support.")]
    [switch]
    $EnableZonalAffinity
)

#################### DEFINITION OF VOLUME DATA ########################
class VolumeData
{
    [ValidateNotNullOrEmpty()][string]$VolumeName
    [ValidateNotNullOrEmpty()][string]$TargetIQN
    [ValidateNotNullOrEmpty()][string]$TargetHostName
    [ValidateNotNullOrEmpty()][string]$TargetPort
    [AllowNull()][Nullable[System.Int32]]$NumSession

    VolumeData($VolumeName, $TargetIQN, $TargetHostName, $TargetPort, $NumSession) {
       $this.VolumeName = $VolumeName
       $this.TargetIQN = $TargetIQN
       $this.TargetHostName = $TargetHostName
       $this.TargetPort = $TargetPort
       $this.NumSession = if ($NumSession -eq 0 -or $NumSession -eq $null) {32} Else {$NumSession}
    }
}

function ConvertTo-ZonalSubscriptionId($Value) {
    if ($Value -isnot [string] -or $Value.Trim() -cnotmatch '\A[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\z') {
        throw 'Subscription ID must be a canonical GUID.'
    }
    $Value.Trim().ToLowerInvariant()
}

function New-ZonalMetadataClient {
    Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(5)
    $client.DefaultRequestHeaders.Add('Metadata', 'true')
    $client
}

function Get-ZonalComputeMetadata {
    $client = New-ZonalMetadataClient
    try {
        # GetStringAsync buffers the complete response under HttpClient.Timeout,
        # unlike a separate per-read timeout that a slow body can keep extending.
        $client.GetStringAsync('http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01').GetAwaiter().GetResult() |
            ConvertFrom-Json -ErrorAction Stop
    } finally { $client.Dispose() }
}

function Initialize-ZonalProviderCancellation {
    if ('ElasticSan.ZonalProviderCancellation' -as [type]) { return }
    # A .NET callback can clean up on the provider thread without a PowerShell
    # event loop. In particular, synchronous Stop/Dispose must not extend the
    # caller deadline when Invoke-AzRestMethod ignores cancellation.
    Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.Management.Automation;
namespace ElasticSan {
    public static class ZonalProviderCancellation {
        public static void StopAndDispose(PowerShell pipeline, IAsyncResult invocation) {
            pipeline.BeginStop(stopped => {
                try {
                    pipeline.EndStop(stopped);
                } finally {
                    pipeline.Dispose();
                    invocation.AsyncWaitHandle.Close();
                }
            }, null);
        }
    }
}
'@
}

function Invoke-ZonalProviderRequest([string]$Command, [hashtable]$Parameters, [ValidateRange(1,300)][int]$TimeoutSeconds = 30) {
    # Get-AzElasticSan* has no -AsJob or timeout parameter. An in-process
    # pipeline keeps DefaultProfile as the real object, not a serialized login.
    Initialize-ZonalProviderCancellation
    $pipeline = [System.Management.Automation.PowerShell]::Create()
    $pending = $null
    $deferredCleanup = $false
    try {
        $null = $pipeline.AddCommand($Command).AddParameters($Parameters).AddParameter('ErrorAction', 'Stop')
        $pending = $pipeline.BeginInvoke()
        if (!$pending.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))) {
            [ElasticSan.ZonalProviderCancellation]::StopAndDispose($pipeline, $pending)
            $deferredCleanup = $true
            throw "$Command exceeded its $TimeoutSeconds-second provider deadline; no connection changes were started. Cancellation was requested; an outstanding read may finish in the background and its result will be discarded."
        }
        $pipeline.EndInvoke($pending)
    } finally {
        if (!$deferredCleanup) {
            if ($null -ne $pending) { $pending.AsyncWaitHandle.Close() }
            $pipeline.Dispose()
        }
    }
}

function Get-ZonalPhysicalZone($Locations, [string]$Location, [string]$LogicalZone) {
    if ($Locations -isnot [array]) { throw 'ARM locations must be an array.' }
    $regions = @{}
    foreach ($candidate in $Locations) {
        if ($candidate -isnot [pscustomobject] -or $candidate.name -isnot [string] -or
            [string]::IsNullOrWhiteSpace($candidate.name)) { throw 'Malformed ARM location.' }
        $key = $candidate.name.Trim()
        if ($regions.ContainsKey($key)) { throw "Duplicate ARM location '$key'." }
        $regions[$key] = $candidate
    }
    $region = $regions[$Location.Trim()]
    if ($null -eq $region) { throw "No ARM location matches '$Location'." }
    if ($region.availabilityZoneMappings -isnot [array] -or $region.availabilityZoneMappings.Count -eq 0) {
        throw 'Missing or malformed availabilityZoneMappings.'
    }
    $zones = @{}
    $physicalZones = @{}
    foreach ($mapping in $region.availabilityZoneMappings) {
        if ($mapping -isnot [pscustomobject] -or $mapping.logicalZone -isnot [string] -or
            $mapping.logicalZone.Trim() -cnotmatch '\A[1-9][0-9]*\z' -or
            $mapping.physicalZone -isnot [string]) { throw 'Malformed availabilityZoneMappings entry.' }
        $logical = $mapping.logicalZone.Trim()
        $physical = $mapping.physicalZone.Trim().ToLowerInvariant()
        if ($physical -cnotmatch '\A[a-z0-9.-]+\z') { throw 'Physical zone is unsafe for an IQN suffix.' }
        if ($zones.ContainsKey($logical) -or $physicalZones.ContainsKey($physical)) {
            throw "Duplicate logical or physical zone in availabilityZoneMappings ('$logical', '$physical')."
        }
        $zones[$logical] = $physical
        $physicalZones[$physical] = $true
    }
    if (!$zones.ContainsKey($LogicalZone.Trim())) { throw "No mapping for logical zone '$LogicalZone'." }
    $zones[$LogicalZone.Trim()]
}

function Resolve-ZonalPhysicalZone($Context, [string]$SubscriptionId, [string]$ResourceGroup, [string]$SanName) {
    $compute = Get-ZonalComputeMetadata
    if ($compute.zone -isnot [string] -or $compute.zone.Trim() -cnotmatch '\A[1-9][0-9]*\z') {
        throw 'VM is not availability-zone pinned; IMDS compute.zone is empty, missing or malformed.'
    }
    if ((ConvertTo-ZonalSubscriptionId $compute.subscriptionId) -ne $SubscriptionId) {
        throw 'Elastic SAN subscription does not match VM subscription.'
    }
    if ($compute.location -isnot [string] -or [string]::IsNullOrWhiteSpace($compute.location)) {
        throw 'IMDS compute.location is empty or missing.'
    }
    $san = Invoke-ZonalProviderRequest 'Get-AzElasticSan' @{
        ResourceGroupName = $ResourceGroup; Name = $SanName; SubscriptionId = $SubscriptionId; DefaultProfile = $Context
    }
    if ($san.Location -isnot [string] -or [string]::IsNullOrWhiteSpace($san.Location) -or
        $san.Location.Trim() -ine $compute.location.Trim()) { throw 'The VM and Elastic SAN must be in the same region.' }
    $response = Invoke-ZonalProviderRequest 'Invoke-AzRestMethod' @{
        Method = 'GET'; Path = "/subscriptions/$SubscriptionId/locations?api-version=2022-12-01"; DefaultProfile = $Context
    }
    if ($response.StatusCode -ne 200) { throw "ARM locations failed: HTTP $($response.StatusCode)." }
    $payload = $response.Content | ConvertFrom-Json -ErrorAction Stop
    Get-ZonalPhysicalZone $payload.value $san.Location $compute.zone
}

function Get-ZonalTargetIqn($TargetIqn, [string]$PhysicalZone) {
    $zone = $PhysicalZone.Trim().ToLowerInvariant()
    if ($zone -cnotmatch '\A[a-z0-9.-]+\z') { throw 'Physical zone is unsafe for an IQN suffix.' }
    if ($TargetIqn -isnot [string] -or $TargetIqn -match ':az-') { throw 'Expected an undecorated service target IQN.' }
    # Only the physical zone is normalized. The service identity is opaque:
    # upper/mixed case needs a service contract decision, not a client rewrite.
    $iqn = "$TargetIqn`:az-$zone"
    if ($iqn -cnotmatch '\Aiqn\.[a-z0-9.:-]+\z') {
        throw 'The complete decorated IQN must use safe lowercase IQN characters. The service identity was not rewritten; confirm the front-end identity contract.'
    }
    if ([System.Text.Encoding]::UTF8.GetByteCount($iqn) -gt 223) { throw 'Decorated target IQN exceeds 223 UTF-8 bytes.' }
    $iqn
}

function ConvertTo-ZonalAddress($Value) {
    if ($Value -isnot [string] -or [string]::IsNullOrEmpty($Value) -or $Value -match '[\s%\[\]]') {
        throw "Invalid or scoped IP address '$Value'."
    }
    # IPAddress accepts short, hex and octal IPv4; the VIP contract does not.
    if ($Value.Contains('.') -or !$Value.Contains(':')) {
        $tail = ($Value -split ':')[-1]
        if ($tail -cnotmatch '\A(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}\z') {
            throw "Invalid dotted-decimal IPv4 address '$Value'."
        }
        foreach ($octet in ($tail -split '\.')) {
            if ([int]$octet -gt 255) { throw "Invalid IPv4 address '$Value'." }
        }
    }
    $ip = $null
    if (![System.Net.IPAddress]::TryParse($Value, [ref]$ip)) { throw "Invalid IP address '$Value'." }
    if ($ip.IsIPv4MappedToIPv6) { $ip = $ip.MapToIPv4() }
    $bytes = $ip.GetAddressBytes()
    $v4 = $ip.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork
    $zero = @($bytes | Where-Object { $_ -ne 0 }).Count -eq 0
    if ($zero -or [System.Net.IPAddress]::IsLoopback($ip) -or
        ($v4 -and (($bytes[0] -ge 224 -and $bytes[0] -le 239) -or
            ($bytes[0] -eq 169 -and $bytes[1] -eq 254) -or
            (@($bytes | Where-Object { $_ -eq 255 }).Count -eq 4))) -or
        (!$v4 -and ($ip.IsIPv6Multicast -or $ip.IsIPv6LinkLocal))) {
        throw "IP address '$Value' is not usable unicast."
    }
    if ($v4) { return $ip.ToString() }
    # Framework IPAddress can print dotted IPv6 tails. Use hex compression,
    # selecting the first longest zero run, for a stable cross-platform plan.
    $groups = @(for ($i = 0; $i -lt 16; $i += 2) { '{0:x}' -f (256 * [int]$bytes[$i] + [int]$bytes[$i + 1]) })
    $bestStart = -1
    $bestLength = 1
    for ($i = 0; $i -lt 8; $i++) {
        if ($groups[$i] -ne '0') { continue }
        $start = $i
        while ($i -lt 8 -and $groups[$i] -eq '0') { $i++ }
        if ($i - $start -gt $bestLength) { $bestStart = $start; $bestLength = $i - $start }
    }
    if ($bestStart -lt 0) { return $groups -join ':' }
    $before = @($groups | Select-Object -First $bestStart) -join ':'
    $after = @($groups | Select-Object -Skip ($bestStart + $bestLength)) -join ':'
    "$before`::$after"
}

function ConvertTo-ZonalVips($Addresses) {
    $unique = @{}
    foreach ($address in $Addresses) {
        $canonical = ConvertTo-ZonalAddress $address
        $ip = [System.Net.IPAddress]::Parse($canonical)
        $family = if ($ip.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) { '0' } else { '1' }
        $key = $family + [System.BitConverter]::ToString($ip.GetAddressBytes())
        $unique[$key] = $canonical
    }
    if ($unique.Count -ne 3) { throw "Expected exactly three unique usable VIPs; received $($unique.Count)." }
    foreach ($key in @($unique.Keys | Sort-Object)) { $unique[$key] }
}

function Start-ZonalDnsLookup([string]$HostName) {
    [System.Net.Dns]::GetHostAddressesAsync($HostName)
}

function Wait-ZonalDnsLookup($Lookup) {
    if (!$Lookup.Wait(5000)) { throw 'DNS lookup exceeded its five-second deadline.' }
    foreach ($address in $Lookup.GetAwaiter().GetResult()) { $address.ToString() }
}

function Resolve-ZonalVips([string]$HostName, [hashtable]$Cache) {
    if ([string]::IsNullOrWhiteSpace($HostName) -or $HostName -match '\s' -or !$HostName.Contains('.') -or
        [Uri]::CheckHostName($HostName) -ne [UriHostNameType]::Dns) {
        throw "Expected a target portal FQDN, not '$HostName'."
    }
    $key = $HostName.ToLowerInvariant()
    if ($Cache.ContainsKey($key)) { return $Cache[$key] }
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            $lookup = Start-ZonalDnsLookup $HostName
            $vips = @(ConvertTo-ZonalVips @(Wait-ZonalDnsLookup $lookup))
            $Cache[$key] = $vips
            return $vips
        } catch [System.Management.Automation.RuntimeException], [System.Net.Sockets.SocketException], [System.TimeoutException], [System.AggregateException] {
            if ($attempt -eq 3) { throw "DNS preflight failed for '$HostName' after three attempts: $($_.Exception.Message)" }
            Start-Sleep -Seconds $attempt
        }
    }
}

function Get-ZonalSessionSlots($Vips) {
    $sorted = @(ConvertTo-ZonalVips $Vips)
    for ($i = 0; $i -lt 32; $i++) { $sorted[$i % 3] }
}

function ConvertTo-ZonalPort($Value) {
    $port = 0
    if ([string]$Value -cnotmatch '\A[0-9]+\z' -or ![int]::TryParse([string]$Value, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw "Invalid target port '$Value'."
    }
    $port
}

function Initialize-ZonalNativeInventory {
    if ('ElasticSan.ZonalPersistentInventory' -as [type]) { return }
    # Read-only iscsidsc.h interop retains original portals and the optional
    # SessionId mapping. Missing correlation is not proof of an existing layout.
    Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace ElasticSan {
    public static class ZonalPersistentInventory {
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct Portal {
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string SymbolicName;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Address;
            public ushort Socket;
        }
        [StructLayout(LayoutKind.Sequential)]
        public struct Options {
            public uint Version, InformationSpecified, LoginFlags, AuthType, HeaderDigest, DataDigest,
                MaximumConnections, DefaultTime2Wait, DefaultTime2Retain, UsernameLength, PasswordLength;
            public IntPtr Username, Password;
        }
        [StructLayout(LayoutKind.Sequential)]
        public struct SessionId { public ulong AdapterUnique, AdapterSpecific; }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct Mapping {
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string InitiatorName;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 224)] public string TargetName;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string OSDeviceName;
            public SessionId SessionId;
            public uint OSBusNumber, OSTargetNumber, LUNCount;
            public IntPtr LUNList;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct Login {
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 224)] public string TargetName;
            [MarshalAs(UnmanagedType.U1)] public bool IsInformationalSession;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string InitiatorInstance;
            public uint InitiatorPortNumber;
            public Portal TargetPortal;
            public ulong SecurityFlags;
            public IntPtr Mappings;
            public Options LoginOptions;
        }
        public sealed class Record {
            public string TargetName, Address, SessionIdentifier, InitiatorInstance;
            public ushort Port;
            public bool IsInformationalSession;
            public uint InitiatorPortNumber, Version, InformationSpecified, LoginFlags, AuthType, HeaderDigest, DataDigest;
            public ulong SecurityFlags;
        }
        [DllImport("iscsidsc.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
        private static extern uint ReportIScsiPersistentLoginsW(out uint count, IntPtr buffer, ref uint size);
        public static Record[] Read() {
            uint count, size = 0;
            uint status = ReportIScsiPersistentLoginsW(out count, IntPtr.Zero, ref size);
            if (status == 0 && count == 0) return new Record[0];
            if (status != 122) throw new InvalidOperationException("Persistent inventory status: " + status);
            for (int attempt = 0; attempt < 3; attempt++) {
                if (size == 0 || size > 16777216) throw new InvalidOperationException("Invalid persistent inventory size.");
                uint allocated = size;
                IntPtr buffer = Marshal.AllocHGlobal((int)allocated);
                try {
                    status = ReportIScsiPersistentLoginsW(out count, buffer, ref size);
                    if (status == 122) continue;
                    if (status != 0) throw new InvalidOperationException("Persistent inventory status: " + status);
                    if (size > allocated) throw new InvalidOperationException("Invalid persistent inventory size.");
                    return Decode(buffer, size, count);
                } finally { Marshal.FreeHGlobal(buffer); }
            }
            throw new InvalidOperationException("Persistent inventory changed during enumeration.");
        }
        public static Record[] Decode(IntPtr buffer, uint size, uint count) {
            int stride = Marshal.SizeOf(typeof(Login));
            ulong arraySize = (ulong)count * (ulong)stride;
            if ((count != 0 && buffer == IntPtr.Zero) || arraySize > size)
                throw new InvalidOperationException("Truncated persistent inventory.");
            var records = new List<Record>();
            for (int i = 0; i < count; i++) {
                Login login = (Login)Marshal.PtrToStructure(IntPtr.Add(buffer, i * stride), typeof(Login));
                string id = null;
                if (login.Mappings != IntPtr.Zero) {
                    long offset = login.Mappings.ToInt64() - buffer.ToInt64();
                    if (offset < 0 || (ulong)offset < arraySize || (ulong)offset + (ulong)Marshal.SizeOf(typeof(Mapping)) > size)
                        throw new InvalidOperationException("Invalid persistent session mapping.");
                    Mapping mapping = (Mapping)Marshal.PtrToStructure(login.Mappings, typeof(Mapping));
                    if (!String.Equals(mapping.TargetName, login.TargetName, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidOperationException("Conflicting persistent session mapping.");
                    if (mapping.SessionId.AdapterUnique != 0 || mapping.SessionId.AdapterSpecific != 0)
                        id = mapping.SessionId.AdapterUnique.ToString("x16") + "-" + mapping.SessionId.AdapterSpecific.ToString("x16");
                }
                records.Add(new Record {
                    TargetName = login.TargetName, Address = login.TargetPortal.Address, Port = login.TargetPortal.Socket,
                    SessionIdentifier = id, InitiatorInstance = login.InitiatorInstance, InitiatorPortNumber = login.InitiatorPortNumber,
                    IsInformationalSession = login.IsInformationalSession,
                    Version = login.LoginOptions.Version, SecurityFlags = login.SecurityFlags, AuthType = login.LoginOptions.AuthType,
                    InformationSpecified = login.LoginOptions.InformationSpecified, LoginFlags = login.LoginOptions.LoginFlags,
                    HeaderDigest = login.LoginOptions.HeaderDigest, DataDigest = login.LoginOptions.DataDigest
                });
            }
            return records.ToArray();
        }
    }
}
'@
}

function Get-ZonalPersistentLogins {
    Initialize-ZonalNativeInventory
    [ElasticSan.ZonalPersistentInventory]::Read()
}

function Get-ZonalInventory {
    $sessions = @(Get-IscsiSession -ErrorAction Stop)
    $connections = @{}
    foreach ($session in $sessions) {
        if ([string]::IsNullOrWhiteSpace($session.TargetNodeAddress) -or
            [string]::IsNullOrWhiteSpace($session.SessionIdentifier) -or $connections.ContainsKey($session.SessionIdentifier)) {
            throw 'Ambiguous live iSCSI session identifiers.'
        }
        $connections[$session.SessionIdentifier] = @(Get-IscsiConnection -IscsiSession $session -ErrorAction Stop)
    }
    $persistent = @(Get-ZonalPersistentLogins)
    $targets = @(Get-IscsiTarget -ErrorAction Stop)
    if (@($persistent | Where-Object { [string]::IsNullOrWhiteSpace($_.TargetName) }).Count -gt 0 -or
        @($targets | Where-Object { [string]::IsNullOrWhiteSpace($_.NodeAddress) }).Count -gt 0) {
        throw 'Persistent or registered target inventory contains an ambiguous target identity.'
    }
    [pscustomobject]@{ Sessions = $sessions; Connections = $connections; Persistent = $persistent; Targets = $targets }
}

function Test-ZonalRelatedTarget([string]$Name, [string]$RawIqn) {
    $Name.Equals($RawIqn, [StringComparison]::OrdinalIgnoreCase) -or
        $Name.StartsWith("$RawIqn`:az-", [StringComparison]::OrdinalIgnoreCase)
}

function ConvertTo-ZonalSessionId([string]$Value) {
    if ($Value -cnotmatch '\A([0-9a-fA-F]{1,16})-([0-9a-fA-F]{1,16})\z') { throw 'Missing or ambiguous persistent-to-live session correlation.' }
    $id = $Matches[1].PadLeft(16, '0').ToLowerInvariant() + '-' + $Matches[2].PadLeft(16, '0').ToLowerInvariant()
    if ($id -eq '0000000000000000-0000000000000000') { throw 'Missing persistent-to-live session correlation.' }
    $id
}

function Test-ZonalLayout($Plan, $Inventory, [int]$ExpectedCount = 32, [hashtable]$ObservedOrigins = @{}, [switch]$AllowPending) {
    $sessions = @($Inventory.Sessions | Where-Object { Test-ZonalRelatedTarget $_.TargetNodeAddress $Plan.RawIqn })
    $persistent = @($Inventory.Persistent | Where-Object { Test-ZonalRelatedTarget $_.TargetName $Plan.RawIqn })
    $targets = @($Inventory.Targets | Where-Object { Test-ZonalRelatedTarget $_.NodeAddress $Plan.RawIqn })
    if ($sessions.Count -eq 0 -and $persistent.Count -eq 0 -and $targets.Count -eq 0 -and !$AllowPending) { return $false }
    if ($sessions.Count -gt $ExpectedCount -or $persistent.Count -gt $ExpectedCount -or
        (!$AllowPending -and ($sessions.Count -ne $ExpectedCount -or $persistent.Count -ne $ExpectedCount))) {
        throw "Partial, extra, or stale state for '$($Plan.RawIqn)': expected $ExpectedCount live and persistent sessions."
    }
    if (@($targets | Where-Object { $_.NodeAddress -ine $Plan.Iqn }).Count -gt 0) { throw 'A different zonal or undecorated target is registered.' }
    $pending = $sessions.Count -ne $ExpectedCount -or $persistent.Count -ne $ExpectedCount
    $liveIds = @{}
    foreach ($session in $sessions) {
        $id = ConvertTo-ZonalSessionId $session.SessionIdentifier
        if ($liveIds.ContainsKey($id) -or $session.TargetNodeAddress -ine $Plan.Iqn) {
            throw 'Conflicting, foreign, or ambiguous live session.'
        }
        $liveIds[$id] = $session
        if ($session.IsConnected -ne $true -or $session.IsPersistent -ne $true -or
            $session.IsHeaderDigest -ne $true -or $session.IsDataDigest -ne $true -or $session.NumberOfConnections -ne 1) {
            if ($AllowPending -and $session.IsConnected -eq $false -and $session.NumberOfConnections -in @(0,1)) { $pending = $true }
            else { throw 'Unhealthy, foreign, or ambiguous live session.' }
        }
        $connections = @($Inventory.Connections[$session.SessionIdentifier])
        if ($connections.Count -ne 1 -or [string]::IsNullOrWhiteSpace($connections[0].ConnectionIdentifier)) {
            if ($AllowPending -and $connections.Count -eq 0) { $pending = $true; continue }
            throw 'Live session connection readiness cannot be established.'
        }
        $null = ConvertTo-ZonalAddress $connections[0].TargetAddress
        $null = ConvertTo-ZonalPort $connections[0].TargetPortNumber
    }
    $expected = @{}
    for ($i = 0; $i -lt $ExpectedCount; $i++) { $expected[$Plan.Slots[$i]] = 1 + $expected[$Plan.Slots[$i]] }
    $counts = @{}
    $mappedIds = @{}
    foreach ($record in $persistent) {
        if ($record.TargetName -ine $Plan.Iqn -or $record.IsInformationalSession -ne $false -or
            $record.InitiatorInstance -ine 'Root\ISCSIPRT\0000_0' -or
            $record.InitiatorPortNumber -ne [uint32]::MaxValue -or $record.Version -ne 0 -or
            $record.SecurityFlags -ne 0 -or $record.AuthType -ne 0 -or
            ($record.InformationSpecified -band 3) -ne 3 -or $record.LoginFlags -ne 2 -or
            $record.HeaderDigest -ne 1 -or $record.DataDigest -ne 1 -or $record.Port -ne $Plan.Port) {
            throw 'Foreign or incompatible persistent login.'
        }
        $address = ConvertTo-ZonalAddress $record.Address
        if ($Plan.Vips -notcontains $address) { throw 'Persistent login uses an old or foreign portal.' }
        $counts[$address] = 1 + $counts[$address]
        if ($counts[$address] -gt [int]$expected[$address]) { throw 'Persistent VIP allocation does not match the planned 11/11/10 layout.' }
        if (![string]::IsNullOrWhiteSpace($record.SessionIdentifier)) {
            $id = ConvertTo-ZonalSessionId $record.SessionIdentifier
            if ($mappedIds.ContainsKey($id)) { throw 'Stale or duplicate persistent session mapping.' }
            $mappedIds[$id] = $true
            if (!$liveIds.ContainsKey($id)) {
                if ($AllowPending -and $sessions.Count -lt $ExpectedCount) { $pending = $true }
                else { throw 'Stale or duplicate persistent session mapping.' }
            }
            if ($ObservedOrigins.ContainsKey($id) -and $ObservedOrigins[$id] -ne $address) { throw 'Persistent portal conflicts with observed login origin.' }
        } elseif ($ObservedOrigins.Count -ne $ExpectedCount) {
            # A redirected current endpoint is never proof of an original VIP.
            if ($AllowPending -and $sessions.Count -lt $ExpectedCount) { $pending = $true }
            else { throw 'Windows did not expose persistent-to-live session correlation; refusing to infer original VIPs from current endpoints.' }
        }
    }
    $origins = @{}
    foreach ($id in $ObservedOrigins.Keys) {
        if (!$liveIds.ContainsKey($id)) { throw 'A session established by this invocation is no longer ready.' }
        if ($Plan.Vips -notcontains $ObservedOrigins[$id]) { throw 'Observed login origins differ from the plan.' }
        $origins[$ObservedOrigins[$id]] = 1 + $origins[$ObservedOrigins[$id]]
    }
    if ($pending) { return $false }
    foreach ($vip in $Plan.Vips) {
        if ([int]$counts[$vip] -ne [int]$expected[$vip]) { throw 'Persistent VIP allocation does not match the planned 11/11/10 layout.' }
        if ($ObservedOrigins.Count -gt 0 -and [int]$origins[$vip] -ne [int]$expected[$vip]) { throw 'Observed login origins differ from the plan.' }
    }
    $true
}

function Invoke-ZonalIscsiCli([string[]]$Arguments) {
    $output = @(& iscsicli @Arguments 2>&1)
    $status = $LASTEXITCODE
    # iscsicli can report an API failure with exit zero. Do not assume success
    # from localized/unknown output or an earlier success line.
    if ($status -ne 0 -or ($output -join "`n").TrimEnd() -cnotmatch '(^|\r?\n)The operation completed successfully\.\z') {
        throw "iscsicli $($Arguments[0]) failed or returned unrecognized status (exit $status): $($output -join ' '). Sessions or persistent entries may remain. Inspect the selected target's live and persistent state before an operator-approved retry; no automatic rollback or disconnect was attempted."
    }
}

function Connect-ZonalVolumes([string]$ResourceGroup, [string]$SanName, [string]$GroupName, [string[]]$Names, [int]$SessionCount = 32) {
    if ($SessionCount -ne 32) { throw 'EnableZonalAffinity requires exactly 32 sessions per volume.' }
    $context = Get-AzContext -ErrorAction Stop
    $subscription = ConvertTo-ZonalSubscriptionId $context.Subscription.Id
    $physicalZone = Resolve-ZonalPhysicalZone $context $subscription $ResourceGroup $SanName
    $null = Invoke-ZonalProviderRequest 'Get-AzElasticSanVolumeGroup' @{
        ResourceGroupName = $ResourceGroup; ElasticSanName = $SanName; Name = $GroupName
        SubscriptionId = $subscription; DefaultProfile = $context
    }
    $seenNames = @{}
    $seenIqns = @{}
    $cache = @{}
    $plans = @(
        foreach ($name in $Names) {
            if ([string]::IsNullOrWhiteSpace($name) -or $seenNames.ContainsKey($name)) { throw "Empty or duplicate selected volume '$name'." }
            $seenNames[$name] = $true
            $volume = Invoke-ZonalProviderRequest 'Get-AzElasticSanVolume' @{
                ResourceGroupName = $ResourceGroup; ElasticSanName = $SanName; VolumeGroupName = $GroupName; Name = $name
                SubscriptionId = $subscription; DefaultProfile = $context
            }
            $iqn = Get-ZonalTargetIqn $volume.StorageTargetIqn $physicalZone
            if ($seenIqns.ContainsKey($volume.StorageTargetIqn)) { throw 'Selected volumes share the same raw IQN.' }
            $seenIqns[$volume.StorageTargetIqn] = $true
            $hostname = $volume.StorageTargetPortalHostname
            if ($hostname -isnot [string] -or $hostname -match '\s' -or !$hostname.Contains('.') -or
                [Uri]::CheckHostName($hostname) -ne [UriHostNameType]::Dns) { throw 'Expected a target portal FQDN.' }
            $port = ConvertTo-ZonalPort $volume.StorageTargetPortalPort
            $vips = @(Resolve-ZonalVips $hostname $cache)
            [pscustomobject]@{
                Name = $name; RawIqn = $volume.StorageTargetIqn; Iqn = $iqn
                Port = $port; Vips = $vips; Slots = @(Get-ZonalSessionSlots $vips); Skip = $false
            }
        }
    )
    if ($plans.Count -eq 0) { throw 'No volumes selected.' }

    # Mapping/IQN/DNS/planning for the whole batch is complete. These are
    # read-only prerequisite checks, never service/MPIO installation or tuning.
    if ((Get-Service -Name MSiSCSI -ErrorAction Stop).Status -ne 'Running') { throw 'A running iSCSI initiator is required.' }
    if ($SessionCount -gt 1 -and (Get-WindowsFeature -Name 'Multipath-IO' -ErrorAction Stop).InstallState -ne 'Installed') {
        throw 'Multipath I/O must already be installed for multiple sessions.'
    }
    $inventory = Get-ZonalInventory
    foreach ($plan in $plans) { $plan.Skip = Test-ZonalLayout $plan $inventory }

    Write-Host 'Zonal IQN routing requires front-end suffix support; DNS alone does not establish affinity.' -ForegroundColor Yellow
    foreach ($plan in $plans) {
        if ($plan.Skip) {
            Write-Host "$($plan.Name) [$($plan.Iqn)]: Skipped; healthy persistent 11/11/10 layout verified." -ForegroundColor Magenta
            continue
        }
        try {
            # Register one identity; every login explicitly supplies its original VIP.
            Invoke-ZonalIscsiCli -Arguments @('AddTarget', $plan.Iqn, '*', $plan.Vips[0], "$($plan.Port)", '*', '0', '*', '*', '*', '*', '*', '*', '*', '*', '*', '0')
            $observed = @{}
            for ($i = 0; $i -lt 32; $i++) {
                Invoke-ZonalIscsiCli -Arguments @('PersistentLoginTarget', $plan.Iqn, 't', $plan.Slots[$i], "$($plan.Port)", 'Root\ISCSIPRT\0000_0', '-1', '*', '0x00000002', '1', '1', '*', '*', '*', '*', '*', '*', '*', '0')
                $ready = $false
                for ($poll = 0; $poll -lt 5; $poll++) {
                    $after = Get-ZonalInventory
                    $newSessions = @($after.Sessions | Where-Object {
                        $_.TargetNodeAddress -ieq $plan.Iqn -and !$observed.ContainsKey((ConvertTo-ZonalSessionId $_.SessionIdentifier))
                    })
                    if ($newSessions.Count -gt 1) { throw 'Concurrent or ambiguous new sessions appeared during login.' }
                    $candidate = @{} + $observed
                    if ($newSessions.Count -eq 1) { $candidate[(ConvertTo-ZonalSessionId $newSessions[0].SessionIdentifier)] = $plan.Slots[$i] }
                    $ready = Test-ZonalLayout $plan $after ($i + 1) $candidate -AllowPending
                    if ($ready) { $observed = $candidate; break }
                    if ($poll -lt 4) { Start-Sleep -Seconds 1 }
                }
                if (!$ready) { throw 'Native login returned success but a new healthy persistent session was not established after five readiness observations.' }
            }
        } catch {
            throw "Volume '$($plan.Name)': $($_.Exception.Message) Sessions or persistent entries for this or earlier volumes may remain. No automatic rollback, disconnect or rebalance was attempted. Inspect Get-IscsiSession, Get-IscsiConnection and iscsicli ListPersistentTargets; use an operator-approved target-specific recovery procedure before retrying."
        }
        Write-Host "$($plan.Name) [$($plan.Iqn)]: Verified 32 healthy persistent sessions (11/11/10)." -ForegroundColor Cyan
    }
}

if ($EnableZonalAffinity) {
    $sessionCount = if ($PSBoundParameters.ContainsKey('NumSession')) { $NumSession } else { 32 }
    Connect-ZonalVolumes $ResourceGroupName $ElasticSanName $VolumeGroupName $VolumeName $sessionCount
    return
}

##################### CHECK DEPENDENCY #################################
$title    = 'Confirm'
$choices  = '&Yes to terminate','&No to proceed with rest of the steps'
$choices = @(
    [System.Management.Automation.Host.ChoiceDescription]::new("&Yes to terminate", "Yes to terminate")
    [System.Management.Automation.Host.ChoiceDescription]::new("&No to proceed with rest of the steps", "No to proceed with rest of the steps")
)

## iSCSI initiator check 
$iscsiWarning = $false 
try {
    $checkResult = Get-Service -Name MSiSCSI -ErrorAction Stop
} catch {
    $iscsiWarning = $true 
}
if (($checkResult.Status -ne "Running") -or $iscsiWarning) {
    $question = 'iSCSI initiator is not installed or enabled. It is required for successful execution of this connect script. Do you wish to terminate the script to install it?'
    $decision = $Host.UI.PromptForChoice($title, $question, $choices, 0)
    if ($decision -eq 0) {
        exit
    }
}

## Multipath I/O check
$multipathWarning = $false 
try {
    $checkResult = Get-WindowsFeature -Name 'Multipath-IO' -ErrorAction Stop
} catch {
    $multipathWarning = $true 
}
if (($checkResult.InstallState -ne "Installed") -or $multipathWarning) {
    $question = 'Multipath I/O is not installed or enabled. It is recommended for multi-session setup. Do you wish to terminate the script to install it?'
    $decision = $Host.UI.PromptForChoice($title, $question, $choices, 0)
    if ($decision -eq 0) {
        exit
    }
}


##################### GATHER INFORMATION OF INPUT VOLUMES ####################
# Get volume group resource to fail fast
$vg = Get-AzElasticSanVolumeGroup -ResourceGroupName $ResourceGroupName -ElasticSanName $ElasticSanName -Name $VolumeGroupName -ErrorAction Stop

$volumesToConnect= New-Object System.Collections.Generic.List[VolumeData]
$invalidVolumes = New-Object System.Collections.Generic.List[string]
# Get each volume in the input volume list and extract the required info for connections 
foreach($volume in $volumeName) {
    try {
        $vol = Get-AzElasticSanVolume -ResourceGroupName $ResourceGroupName -ElasticSanName $ElasticSanName -VolumeGroupName $VolumeGroupName -Name $volume -ErrorAction Stop
        $targetIqn = $vol.StorageTargetIqn
        $targetHostname = $vol.StorageTargetPortalHostname
        $targetPort = $vol.StorageTargetPortalPort
        $volumesToConnect.Add([VolumeData]::new($volume,$targetIqn,$targetHostname, $targetPort, $numSession))
        Write-Host Gathered info of $volume successfully -ForegroundColor Cyan
    } catch {
        Write-Error $_
        $invalidVolumes.Add($volume)
    }    
}
if ($invalidVolumes.Count -gt 0) {
    # Terminate the script if any of the input volumes are invalid
    Write-Error "Invalid volumes: $($invalidVolumes -Join ",")" -ErrorAction Stop
}

############################### CONNECT VOLUMES ############################
$sessions = Get-IscsiSession
if ($sessions -ne $null) {
    $sessions = (Get-IscsiSession).TargetNodeAddress.ToLower() | Select -Unique
}

foreach($volume in $volumesToConnect) {
    # Check if the volume is already connected 
    if ($sessions -ne $null -and $sessions.Contains($volume.TargetIQN.ToLower())) {
        Write-Host $volume.VolumeName [$($volume.TargetIQN)]: Skipped as this volume is already connected -ForegroundColor Magenta
        continue
    }
    # connect volume 
    Write-Host $volume.VolumeName [$($volume.TargetIQN)]: Connecting to this volume -ForegroundColor Cyan
    iscsicli AddTarget $volume.TargetIQN * $volume.TargetHostName $volume.TargetPort * 0 * * * * * * * * * 0
    $LoginOptions = '0x00000002'
    for ($i = 0; $i -lt $volume.NumSession; $i++) {
        iscsicli PersistentLoginTarget $volume.TargetIQN.ToLower() t $volume.TargetHostname.ToLower() $volume.TargetPort Root\ISCSIPRT\0000_0 -1 * $LoginOptions 1 1 * * * * * * * 0
    }
}
