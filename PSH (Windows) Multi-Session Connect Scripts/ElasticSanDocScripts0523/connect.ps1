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
    [Parameter(HelpMessage = "Opt in to zonal IQN routing and exactly 32 sessions across three locally resolved VIPs. Requires backend zonal IQN support.")]
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
    if ($Value -isnot [string] -or $Value.Trim() -notmatch '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
        throw 'Subscription ID must be a canonical GUID.'
    }
    $Value.Trim().ToLowerInvariant()
}

function Get-ZonalComputeMetadata {
    # WebRequest works on Windows PowerShell 5.1 and bypasses IMDS proxies.
    $request = [System.Net.WebRequest]::Create('http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01')
    $request.Proxy = $null
    $request.Timeout = 5000
    $request.ReadWriteTimeout = 5000
    $request.Headers.Add('Metadata', 'true')
    $response = $null
    $reader = $null
    try {
        $response = $request.GetResponse()
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        $reader.ReadToEnd() | ConvertFrom-Json -ErrorAction Stop
    } finally {
        if ($null -ne $reader) { $reader.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
    }
}

function Get-ZonalPhysicalZone($Locations, [string]$Location, [string]$LogicalZone) {
    if ($Locations -isnot [array]) { throw 'ARM locations must be an array.' }
    $region = $null
    foreach ($candidate in $Locations) {
        if ($candidate -isnot [pscustomobject]) { throw 'Malformed ARM location.' }
        if ($candidate.name -is [string] -and $candidate.name.Trim() -ieq $Location.Trim()) {
            $region = $candidate
            break
        }
    }
    if ($null -eq $region) { throw "No ARM location matches '$Location'." }
    if ($region.availabilityZoneMappings -isnot [array] -or $region.availabilityZoneMappings.Count -eq 0) {
        throw 'Missing or malformed availabilityZoneMappings.'
    }
    $zones = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([System.StringComparer]::Ordinal)
    foreach ($mapping in $region.availabilityZoneMappings) {
        if ($mapping.logicalZone -isnot [string] -or [string]::IsNullOrWhiteSpace($mapping.logicalZone) -or
            $mapping.physicalZone -isnot [string] -or [string]::IsNullOrWhiteSpace($mapping.physicalZone)) {
            throw 'Malformed availabilityZoneMappings entry.'
        }
        $key = $mapping.logicalZone.Trim()
        if ($zones.ContainsKey($key)) { throw "Duplicate logical zone '$key'." }
        $zones.Add($key, $mapping.physicalZone.Trim())
    }
    if (!$zones.ContainsKey($LogicalZone.Trim())) { throw "No mapping for logical zone '$LogicalZone'." }
    $zones[$LogicalZone.Trim()]
}

function Resolve-ZonalPhysicalZone($Context, [string]$SubscriptionId, [string]$ResourceGroup, [string]$SanName) {
    $compute = Get-ZonalComputeMetadata
    if ($compute.zone -isnot [string] -or [string]::IsNullOrWhiteSpace($compute.zone)) {
        throw 'VM is not availability-zone pinned; IMDS compute.zone is empty or missing.'
    }
    if ((ConvertTo-ZonalSubscriptionId $compute.subscriptionId) -ne $SubscriptionId) {
        throw 'Elastic SAN subscription does not match VM subscription.'
    }
    if ($compute.location -isnot [string] -or [string]::IsNullOrWhiteSpace($compute.location)) {
        throw 'IMDS compute.location is empty or missing.'
    }
    $san = Get-AzElasticSan -ResourceGroupName $ResourceGroup -Name $SanName -SubscriptionId $SubscriptionId -DefaultProfile $Context -ErrorAction Stop
    if ($san.Location -isnot [string] -or [string]::IsNullOrWhiteSpace($san.Location) -or
        $san.Location.Trim() -ine $compute.location.Trim()) {
        throw 'The VM and Elastic SAN must be in the same region.'
    }
    $response = Invoke-AzRestMethod -Method GET -Path "/subscriptions/$SubscriptionId/locations?api-version=2022-12-01" -DefaultProfile $Context -ErrorAction Stop
    if ($response.StatusCode -ne 200) { throw "ARM locations failed: HTTP $($response.StatusCode)." }
    $payload = $response.Content | ConvertFrom-Json -ErrorAction Stop
    Get-ZonalPhysicalZone $payload.value $san.Location $compute.zone
}

function Get-ZonalTargetIqn([string]$TargetIqn, [string]$PhysicalZone) {
    $zone = $PhysicalZone.Trim().ToLowerInvariant()
    if ($zone -cnotmatch '^[a-z0-9.-]+$') { throw 'Physical zone is unsafe for an IQN suffix.' }
    if ([string]::IsNullOrWhiteSpace($TargetIqn) -or $TargetIqn -match '\s|:az-') {
        throw 'Expected an undecorated target IQN without whitespace.'
    }
    # Provisional contract: the front end must parse and strip this suffix.
    $iqn = "$TargetIqn`:az-$zone"
    if ([System.Text.Encoding]::UTF8.GetByteCount($iqn) -gt 223) { throw 'Decorated target IQN exceeds 223 UTF-8 bytes.' }
    $iqn
}

function ConvertTo-ZonalAddress($Value) {
    if ($Value -isnot [string] -or [string]::IsNullOrEmpty($Value) -or $Value -match '[\s%\[\]]') {
        throw "Invalid or scoped IP address '$Value'."
    }
    # IPAddress accepts short, hex and octal IPv4; the cross-OS contract does not.
    if ($Value.Contains('.') -or !$Value.Contains(':')) {
        $tail = ($Value -split ':')[-1]
        if ($tail -cnotmatch '^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$') {
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
    # Framework IPAddress prints some IPv6 addresses with dotted tails, unlike
    # Python. Use RFC 5952 hex compression, including the first longest zero run.
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
    if ([string]::IsNullOrWhiteSpace($HostName) -or $HostName -match '\s' -or
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
    if ([string]$Value -notmatch '^[0-9]+$' -or ![int]::TryParse([string]$Value, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw "Invalid target port '$Value'."
    }
    $port
}

function Initialize-ZonalNativeInventory {
    if ('ElasticSan.ZonalPersistentInventory' -as [type]) { return }
    # Read-only iscsidsc.h interop avoids localized CLI parsing and retains the
    # optional persistent mapping's SessionId. A missing mapping is NOT proof.
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
                    return Decode(buffer, allocated, count);
                } finally { Marshal.FreeHGlobal(buffer); }
            }
            throw new InvalidOperationException("Persistent inventory changed during enumeration.");
        }
        public static Record[] Decode(IntPtr buffer, uint size, uint count) {
            int stride = Marshal.SizeOf(typeof(Login));
            if ((count != 0 && buffer == IntPtr.Zero) || (ulong)count * (ulong)stride > size)
                throw new InvalidOperationException("Truncated persistent inventory.");
            var records = new List<Record>();
            for (int i = 0; i < count; i++) {
                Login login = (Login)Marshal.PtrToStructure(IntPtr.Add(buffer, i * stride), typeof(Login));
                string id = null;
                if (login.Mappings != IntPtr.Zero) {
                    long offset = login.Mappings.ToInt64() - buffer.ToInt64();
                    if (offset < 0 || (ulong)offset + (ulong)Marshal.SizeOf(typeof(Mapping)) > size)
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
    [pscustomobject]@{
        Sessions = $sessions
        Connections = $connections
        Persistent = $persistent
        Targets = $targets
    }
}

function Test-ZonalRelatedTarget([string]$Name, [string]$RawIqn) {
    $Name.Equals($RawIqn, [StringComparison]::OrdinalIgnoreCase) -or
        $Name.StartsWith("$RawIqn`:az-", [StringComparison]::OrdinalIgnoreCase)
}

function ConvertTo-ZonalSessionId([string]$Value) {
    if ($Value -notmatch '^([0-9a-fA-F]{1,16})-([0-9a-fA-F]{1,16})$') { throw 'Missing or ambiguous persistent-to-live session correlation.' }
    $Matches[1].PadLeft(16, '0').ToLowerInvariant() + '-' + $Matches[2].PadLeft(16, '0').ToLowerInvariant()
}

function Test-ZonalLayout($Plan, $Inventory, [int]$ExpectedCount = 32, [hashtable]$ObservedOrigins = @{}, [switch]$AllowPending) {
    $sessions = @($Inventory.Sessions | Where-Object { Test-ZonalRelatedTarget $_.TargetNodeAddress $Plan.RawIqn })
    $persistent = @($Inventory.Persistent | Where-Object { Test-ZonalRelatedTarget $_.TargetName $Plan.RawIqn })
    $targets = @($Inventory.Targets | Where-Object { Test-ZonalRelatedTarget $_.NodeAddress $Plan.RawIqn })
    if ($sessions.Count -eq 0 -and $persistent.Count -eq 0 -and $targets.Count -eq 0 -and $ExpectedCount -eq 32) { return $false }
    if ($sessions.Count -ne $ExpectedCount -or $persistent.Count -ne $ExpectedCount) {
        if ($AllowPending -and $sessions.Count -le $ExpectedCount -and $persistent.Count -le $ExpectedCount) {
            return $false
        }
        throw "Partial, extra, or stale state for '$($Plan.RawIqn)': expected $ExpectedCount live and persistent sessions."
    }
    if (@($targets | Where-Object { $_.NodeAddress -ine $Plan.Iqn }).Count -gt 0) { throw 'A different zonal or undecorated target is registered.' }
    $liveIds = @{}
    foreach ($session in $sessions) {
        $id = ConvertTo-ZonalSessionId $session.SessionIdentifier
        if ($liveIds.ContainsKey($id) -or $session.TargetNodeAddress -ine $Plan.Iqn) {
            throw 'Conflicting, foreign, or ambiguous live session.'
        }
        if ($session.IsConnected -ne $true -or $session.IsPersistent -ne $true -or
            $session.IsHeaderDigest -ne $true -or $session.IsDataDigest -ne $true -or
            $session.NumberOfConnections -ne 1) {
            if ($AllowPending -and $session.NumberOfConnections -le 1) { return $false }
            throw 'Unhealthy, foreign, or ambiguous live session.'
        }
        $connections = @($Inventory.Connections[$session.SessionIdentifier])
        if ($connections.Count -ne 1 -or [string]::IsNullOrWhiteSpace($connections[0].ConnectionIdentifier)) {
            if ($AllowPending -and $connections.Count -eq 0) { return $false }
            throw 'Live session connection readiness cannot be established.'
        }
        $null = ConvertTo-ZonalAddress $connections[0].TargetAddress
        $null = ConvertTo-ZonalPort $connections[0].TargetPortNumber
        $liveIds[$id] = $session
    }
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
        if (![string]::IsNullOrWhiteSpace($record.SessionIdentifier)) {
            $id = ConvertTo-ZonalSessionId $record.SessionIdentifier
            if (!$liveIds.ContainsKey($id) -or $mappedIds.ContainsKey($id)) { throw 'Stale or duplicate persistent session mapping.' }
            $mappedIds[$id] = $true
            if ($ObservedOrigins.ContainsKey($id) -and $ObservedOrigins[$id] -ne $address) { throw 'Persistent portal conflicts with observed login origin.' }
        } elseif ($ObservedOrigins.Count -ne $ExpectedCount) {
            # Current endpoints may be redirected. Never use them as original VIPs.
            throw 'Windows did not expose persistent-to-live session correlation; refusing to infer original VIPs from current endpoints.'
        }
    }
    $expected = @{}
    for ($i = 0; $i -lt $ExpectedCount; $i++) { $expected[$Plan.Slots[$i]] = 1 + $expected[$Plan.Slots[$i]] }
    foreach ($vip in $Plan.Vips) {
        if ([int]$counts[$vip] -ne [int]$expected[$vip]) { throw 'Persistent VIP allocation does not match the planned 11/11/10 layout.' }
    }
    if ($ObservedOrigins.Count -gt 0) {
        $origins = @{}
        foreach ($id in $ObservedOrigins.Keys) {
            if (!$liveIds.ContainsKey($id)) { throw 'A session established by this invocation is no longer ready.' }
            $origins[$ObservedOrigins[$id]] = 1 + $origins[$ObservedOrigins[$id]]
        }
        foreach ($vip in $Plan.Vips) {
            if ([int]$origins[$vip] -ne [int]$expected[$vip]) { throw 'Observed login origins differ from the plan.' }
        }
    }
    $true
}

function Invoke-ZonalIscsiCli([string[]]$Arguments) {
    $output = @(& iscsicli @Arguments 2>&1)
    $status = $LASTEXITCODE
    # iscsicli can report an API failure without a failing process exit code.
    # Unsupported/localized output is deliberately not treated as success.
    if ($status -ne 0 -or ($output -join "`n").TrimEnd() -cnotmatch '(^|\r?\n)The operation completed successfully\.\z') {
        throw "iscsicli $($Arguments[0]) failed or returned unrecognized status (exit $status): $($output -join ' ')"
    }
}

function Connect-ZonalVolumes([string]$ResourceGroup, [string]$SanName, [string]$GroupName, [string[]]$Names) {
    $recovery = 'No automatic disconnect or rebalance was attempted. Inspect Get-IscsiSession, Get-IscsiConnection and iscsicli ListPersistentTargets; use the approved disconnect/recovery procedure for these volumes, then retry.'
    try {
        if ((Get-Service -Name MSiSCSI -ErrorAction Stop).Status -ne 'Running' -or
            (Get-WindowsFeature -Name 'Multipath-IO' -ErrorAction Stop).InstallState -ne 'Installed') {
            throw 'Zonal multi-session mode requires a running iSCSI initiator and installed Multipath I/O.'
        }
        $context = Get-AzContext -ErrorAction Stop
        $subscription = ConvertTo-ZonalSubscriptionId $context.Subscription.Id
        $physicalZone = Resolve-ZonalPhysicalZone $context $subscription $ResourceGroup $SanName
        $null = Get-AzElasticSanVolumeGroup -ResourceGroupName $ResourceGroup -ElasticSanName $SanName -Name $GroupName -SubscriptionId $subscription -DefaultProfile $context -ErrorAction Stop
        $seenNames = @{}
        $seenIqns = @{}
        $cache = @{}
        $plans = @(
            foreach ($name in $Names) {
                if ([string]::IsNullOrWhiteSpace($name) -or $seenNames.ContainsKey($name)) { throw "Empty or duplicate selected volume '$name'." }
                $seenNames[$name] = $true
                $volume = Get-AzElasticSanVolume -ResourceGroupName $ResourceGroup -ElasticSanName $SanName -VolumeGroupName $GroupName -Name $name -SubscriptionId $subscription -DefaultProfile $context -ErrorAction Stop
                $iqn = Get-ZonalTargetIqn $volume.StorageTargetIqn $physicalZone
                if ($seenIqns.ContainsKey($volume.StorageTargetIqn)) { throw 'Selected volumes share the same raw IQN.' }
                $seenIqns[$volume.StorageTargetIqn] = $true
                $port = ConvertTo-ZonalPort $volume.StorageTargetPortalPort
                $vips = @(Resolve-ZonalVips $volume.StorageTargetPortalHostname $cache)
                [pscustomobject]@{
                    Name = $name; RawIqn = $volume.StorageTargetIqn; Iqn = $iqn.ToLowerInvariant()
                    Port = $port; Vips = $vips; Slots = @(Get-ZonalSessionSlots $vips); Skip = $false
                }
            }
        )
        if ($plans.Count -eq 0) { throw 'No volumes selected.' }
        $inventory = Get-ZonalInventory
        foreach ($plan in $plans) { $plan.Skip = Test-ZonalLayout $plan $inventory }

        Write-Host 'Zonal IQN suffix routing requires Elastic SAN front-end parsing support; DNS alone does not establish affinity.' -ForegroundColor Yellow
        foreach ($plan in $plans) {
            if ($plan.Skip) {
                Write-Host "$($plan.Name) [$($plan.Iqn)]: Skipped; healthy persistent 11/11/10 layout verified." -ForegroundColor Magenta
                continue
            }
            # One static target identity only. Every login overrides its portal.
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
                    if ($newSessions.Count -eq 1) {
                        $candidate = @{} + $observed
                        $candidate[(ConvertTo-ZonalSessionId $newSessions[0].SessionIdentifier)] = $plan.Slots[$i]
                        $ready = Test-ZonalLayout $plan $after ($i + 1) $candidate -AllowPending
                        if ($ready) { $observed = $candidate; break }
                    } elseif ($newSessions.Count -gt 1) { throw 'Concurrent or ambiguous new sessions appeared during login.' }
                    if ($poll -lt 4) { Start-Sleep -Seconds 1 }
                }
                if (!$ready) { throw 'Native login returned success but a new healthy persistent session was not established.' }
            }
            Write-Host "$($plan.Name) [$($plan.Iqn)]: Verified 32 healthy persistent sessions (11/11/10)." -ForegroundColor Cyan
        }
    } catch [System.Management.Automation.RuntimeException], [System.InvalidOperationException] {
        throw "$($_.Exception.Message) $recovery"
    }
}

if ($EnableZonalAffinity) {
    if ($PSBoundParameters.ContainsKey('NumSession') -and $NumSession -ne 32) {
        throw 'EnableZonalAffinity requires exactly 32 sessions per volume.'
    }
    Connect-ZonalVolumes $ResourceGroupName $ElasticSanName $VolumeGroupName $VolumeName
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

