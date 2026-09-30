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
    [Parameter(HelpMessage = "Opt in to subscription-scoped zonal IQN mapping. Requires backend zonal IQN support.")]
    [switch]
    $EnableZonalAffinity,
    [Parameter(HelpMessage = "Distribute original persistent login portals across resolved IPv4 VIPs, independently of zonal IQN mapping.")]
    [switch]
    $EnableVipDistribution
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

function Get-VipHostAddresses([string]$HostName) {
    [System.Net.Dns]::GetHostAddresses($HostName)
}

function Resolve-VipPortals([string]$HostName) {
    try {
        $addresses = @(Get-VipHostAddresses $HostName |
            Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } |
            Sort-Object { [BitConverter]::ToString($_.GetAddressBytes()) } -Unique |
            ForEach-Object { $_.ToString() })
    } catch [System.Net.Sockets.SocketException] {
        throw "DNS lookup failed for '$HostName': $($_.Exception.Message)"
    }
    if ($addresses.Count -eq 0) { throw "DNS lookup for '$HostName' returned no IPv4 addresses." }
    # A single endpoint keeps DNS indirection for LRS, private endpoints and migrations.
    if ($addresses.Count -eq 1) { return $HostName }
    $addresses
}

function Invoke-ZonalIscsiCli([string[]]$Arguments, [switch]$PassThru) {
    $output = @(& iscsicli @Arguments 2>&1)
    $status = $LASTEXITCODE
    # iscsicli can report an API failure with exit zero. Do not assume success
    # from localized/unknown output or an earlier success line.
    if ($status -ne 0 -or ($output -join "`n").TrimEnd() -cnotmatch '(^|\r?\n)The operation completed successfully\.\z') {
        throw "iscsicli $($Arguments[0]) failed or returned unrecognized status (exit $status): $($output -join ' '). Sessions or persistent entries may remain. Inspect the selected target's live and persistent state before an operator-approved retry; no automatic rollback or disconnect was attempted."
    }
    if ($PassThru) { $output }
}

function Get-ZonalPersistentTargets {
    $count = $null
    $targets = @(foreach ($line in (Invoke-ZonalIscsiCli -Arguments @('ListPersistentTargets') -PassThru)) {
        if ($line -match '\A\s*Total\s+of\s+(\d+)\s+pers?istent\s+targets\s*\z') {
            if ($null -ne $count) { throw 'Persistent target inventory contains multiple totals.' }
            $count = [int]$Matches[1]
        }
        $parts = ([string]$line) -split ':', 2
        if ($parts.Count -eq 2 -and $parts[0].Trim() -match '\ATarget\s+Name\z') {
            if ([string]::IsNullOrWhiteSpace($parts[1])) { throw 'Persistent target inventory contains an empty Target Name.' }
            $parts[1].Trim()
        }
    })
    if ($null -eq $count -or $count -ne $targets.Count) {
        throw 'Persistent target inventory is incomplete or unrecognized; cannot safely determine whether the volume is already configured.'
    }
    $targets
}

function Connect-ElasticSanVolumes([string]$ResourceGroup, [string]$SanName, [string]$GroupName, [string[]]$Names,
    [ValidateRange(1,32)][int]$SessionCount = 32, [switch]$ZonalAffinity, [switch]$VipDistribution) {
    $context = Get-AzContext -ErrorAction Stop
    $subscription = ConvertTo-ZonalSubscriptionId $context.Subscription.Id
    if ($ZonalAffinity) { $physicalZone = Resolve-ZonalPhysicalZone $context $subscription $ResourceGroup $SanName }
    $null = Invoke-ZonalProviderRequest 'Get-AzElasticSanVolumeGroup' @{
        ResourceGroupName = $ResourceGroup; ElasticSanName = $SanName; Name = $GroupName
        SubscriptionId = $subscription; DefaultProfile = $context
    }
    $seenNames = @{}
    $seenIqns = @{}
    $plans = @(
        foreach ($name in $Names) {
            if ([string]::IsNullOrWhiteSpace($name) -or $seenNames.ContainsKey($name)) { throw "Empty or duplicate selected volume '$name'." }
            $seenNames[$name] = $true
            $volume = Invoke-ZonalProviderRequest 'Get-AzElasticSanVolume' @{
                ResourceGroupName = $ResourceGroup; ElasticSanName = $SanName; VolumeGroupName = $GroupName; Name = $name
                SubscriptionId = $subscription; DefaultProfile = $context
            }
            $iqn = $volume.StorageTargetIqn
            if ([string]::IsNullOrWhiteSpace($iqn)) { throw 'Missing target IQN.' }
            if ($ZonalAffinity) { $iqn = Get-ZonalTargetIqn $iqn $physicalZone }
            if ($seenIqns.ContainsKey($volume.StorageTargetIqn)) { throw 'Selected volumes share the same raw IQN.' }
            $seenIqns[$volume.StorageTargetIqn] = $true
            $hostname = $volume.StorageTargetPortalHostname
            if ($hostname -isnot [string] -or $hostname -match '\s' -or !$hostname.Contains('.') -or
                [Uri]::CheckHostName($hostname) -ne [UriHostNameType]::Dns) { throw 'Expected a target portal FQDN.' }
            $port = 0
            if ([string]$volume.StorageTargetPortalPort -cnotmatch '\A[0-9]+\z' -or
                ![int]::TryParse([string]$volume.StorageTargetPortalPort, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
                throw 'Invalid target port.'
            }
            [pscustomobject]@{
                Name = $name; RawIqn = $volume.StorageTargetIqn; Iqn = $iqn
                Port = $port; HostName = $hostname; Portals = @($hostname); Skip = $false
            }
        }
    )
    if ($plans.Count -eq 0) { throw 'No volumes selected.' }

    # Mapping/IQN/input preflight for the whole batch is complete. These are
    # read-only prerequisite checks, never service/MPIO installation or tuning.
    if ((Get-Service -Name MSiSCSI -ErrorAction Stop).Status -ne 'Running') { throw 'A running iSCSI initiator is required.' }
    if ($SessionCount -gt 1 -and (Get-WindowsFeature -Name 'Multipath-IO' -ErrorAction Stop).InstallState -ne 'Installed') {
        throw 'Multipath I/O must already be installed for multiple sessions.'
    }
    $targets = @((Get-IscsiSession -ErrorAction Stop).TargetNodeAddress) + @(Get-ZonalPersistentTargets)
    foreach ($plan in $plans) {
        $plan.Skip = @($targets | Where-Object {
            $_ -ieq $plan.RawIqn -or ($_ -is [string] -and $_.StartsWith("$($plan.RawIqn):az-", [StringComparison]::OrdinalIgnoreCase))
        }).Count -gt 0
        if (!$plan.Skip -and $VipDistribution) { $plan.Portals = @(Resolve-VipPortals $plan.HostName) }
    }

    if ($ZonalAffinity) { Write-Host 'Zonal IQN routing requires front-end suffix support.' -ForegroundColor Yellow }
    foreach ($plan in $plans) {
        if ($plan.Skip) {
            Write-Host "$($plan.Name) [$($plan.Iqn)]: already configured; run disconnect.ps1 first to change the layout" -ForegroundColor Magenta
            continue
        }
        try {
            foreach ($portal in ($plan.Portals | Select-Object -First $SessionCount)) {
                Invoke-ZonalIscsiCli -Arguments @('AddTarget', $plan.Iqn, '*', $portal, "$($plan.Port)", '*', '0', '*', '*', '*', '*', '*', '*', '*', '*', '*', '0')
            }
            for ($i = 0; $i -lt $SessionCount; $i++) {
                $portal = $plan.Portals[$i % $plan.Portals.Count]
                Invoke-ZonalIscsiCli -Arguments @('PersistentLoginTarget', $plan.Iqn.ToLowerInvariant(), 't', $portal.ToLowerInvariant(), "$($plan.Port)", 'Root\ISCSIPRT\0000_0', '-1', '*', '0x00000002', '1', '1', '*', '*', '*', '*', '*', '*', '*', '0')
            }
        } catch {
            throw "Volume '$($plan.Name)': $($_.Exception.Message) Sessions or persistent entries for this or earlier volumes may remain. No automatic rollback, disconnect or rebalance was attempted. Inspect Get-IscsiSession, Get-IscsiConnection and iscsicli ListPersistentTargets; use an operator-approved target-specific recovery procedure before retrying."
        }
        Write-Host "$($plan.Name) [$($plan.Iqn)]: Saved $SessionCount persistent login requests." -ForegroundColor Cyan
    }
    Write-Host 'Reboot is required to establish the saved persistent logins. The front end may redirect sessions after login; original portals do not determine final placement.' -ForegroundColor Yellow
}

if ($EnableZonalAffinity -or $EnableVipDistribution) {
    $sessionCount = if ($PSBoundParameters.ContainsKey('NumSession')) { $NumSession } else { 32 }
    Connect-ElasticSanVolumes $ResourceGroupName $ElasticSanName $VolumeGroupName $VolumeName $sessionCount -ZonalAffinity:$EnableZonalAffinity -VipDistribution:$EnableVipDistribution
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
