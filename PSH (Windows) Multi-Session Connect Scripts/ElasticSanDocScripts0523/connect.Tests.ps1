$connectPath = Join-Path $PSScriptRoot 'connect.ps1'
$fixturePath = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'tests\standalone_zonal_affinity_cases.json'
$fixtures = Get-Content -Raw $fixturePath | ConvertFrom-Json
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($connectPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }

# Import helpers without executing the script or touching the host. Entrypoint
# tests below also run the actual file with its mandatory parameters.
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -like '*-Zonal*' }, $false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}

# These cmdlets need not be installed on the machine running this test file.
function Get-AzContext { [CmdletBinding()]param() }
function Get-AzElasticSan { [CmdletBinding()]param($ResourceGroupName, $Name, $SubscriptionId, $DefaultProfile) }
function Get-AzElasticSanVolumeGroup { [CmdletBinding()]param($ResourceGroupName, $ElasticSanName, $Name, $SubscriptionId, $DefaultProfile) }
function Get-AzElasticSanVolume { [CmdletBinding()]param($ResourceGroupName, $ElasticSanName, $VolumeGroupName, $Name, $SubscriptionId, $DefaultProfile) }
function Invoke-AzRestMethod { [CmdletBinding()]param($Method, $Path, $DefaultProfile) }
function Get-WindowsFeature { [CmdletBinding()]param($Name) }
function Get-IscsiSession { [CmdletBinding()]param() }
function Get-IscsiConnection { [CmdletBinding()]param($IscsiSession) }
function Get-IscsiTarget { [CmdletBinding()]param() }

function New-TestSession([int]$Number, [string]$Iqn) {
    [pscustomobject]@{
        SessionIdentifier = ('0000000000000001-{0:x16}' -f $Number)
        TargetNodeAddress = $Iqn; IsConnected = $true; IsPersistent = $true
        IsHeaderDigest = $true; IsDataDigest = $true; NumberOfConnections = 1
    }
}

function New-TestPersistent($Session, [string]$Address, [bool]$Correlate = $true) {
    [pscustomobject]@{
        TargetName = $Session.TargetNodeAddress; Address = $Address; Port = 3260
        SessionIdentifier = $(if ($Correlate) { $Session.SessionIdentifier } else { $null })
        InitiatorInstance = 'Root\ISCSIPRT\0000_0'; IsInformationalSession = $false
        InitiatorPortNumber = [uint32]::MaxValue; Version = 0; SecurityFlags = 0; AuthType = 0
        InformationSpecified = 3; LoginFlags = 2; HeaderDigest = 1; DataDigest = 1
    }
}

function New-TestPlan {
    $vips = @('10.0.0.1', '10.0.0.2', 'fd00::1')
    [pscustomobject]@{
        Name = 'one'; RawIqn = 'iqn.test:one'; Iqn = 'iqn.test:one:az-eastus-az3'
        Port = 3260; Vips = $vips; Slots = @(Get-ZonalSessionSlots $vips); Skip = $false
    }
}

function New-TestInventory($Plan, [int]$Count = 32, [bool]$Correlate = $true) {
    $sessions = @()
    $persistent = @()
    $connections = @{}
    for ($i = 0; $i -lt $Count; $i++) {
        $session = New-TestSession ($i + 1) $Plan.Iqn
        $sessions += $session
        $persistent += New-TestPersistent $session $Plan.Slots[$i % 32] $Correlate
        # Redirected endpoints deliberately differ from the original VIPs.
        $connections[$session.SessionIdentifier] = @([pscustomobject]@{
            ConnectionIdentifier = "connection-$i"; TargetAddress = '10.20.30.40'; TargetPortNumber = 3260
        })
    }
    [pscustomobject]@{ Sessions = $sessions; Persistent = $persistent; Connections = $connections; Targets = @() }
}

# Keep argv capture outside Pester's parameter binder: this receives exactly
# what the script passes to the native executable, including every wildcard.
function iscsicli {
    $argv = @($args)
    $script:nativeCalls.Add($argv)
    $global:LASTEXITCODE = $script:nativeExit
    if ($script:nativeSuccess -and $script:simulateLogin -and $argv[0] -eq 'PersistentLoginTarget') {
        $session = New-TestSession ($script:live.Count + 1) $argv[1]
        $script:live += $session
        $script:persistent += New-TestPersistent $session $argv[3] $false
    }
    if ($script:nativeSuccess) { 'The operation completed successfully.' } else { 'The operation failed. Error 0xefff0003.' }
}

Describe 'Shared normalization, mapping, and allocation contract' {
    foreach ($case in $fixtures.addressCases) {
        It "normalizes or rejects address fixture: $($case.name)" {
            if ($case.error) {
                { ConvertTo-ZonalVips $case.addresses } | Should Throw
            } else {
                (@(ConvertTo-ZonalVips $case.addresses) -join ',') | Should Be ($case.expected -join ',')
            }
        }
    }
    foreach ($case in $fixtures.mappingCases) {
        It "resolves or rejects mapping fixture: $($case.name)" {
            if ($case.error) {
                { Get-ZonalPhysicalZone $case.locations $case.location $case.logicalZone } | Should Throw
            } else {
                (Get-ZonalPhysicalZone $case.locations $case.location $case.logicalZone) | Should Be $case.expected
            }
        }
    }
    It 'allocates exactly 32 round-robin slots and fixture counts' {
        $vips = @('fd00::1', '10.0.0.2', '10.0.0.1')
        $sorted = @(ConvertTo-ZonalVips $vips)
        $slots = @(Get-ZonalSessionSlots $vips)
        $slots.Count | Should Be $fixtures.sessionCount
        for ($i = 0; $i -lt $slots.Count; $i++) { $slots[$i] | Should Be $sorted[$i % 3] }
        for ($i = 0; $i -lt 3; $i++) { @($slots | Where-Object { $_ -eq $sorted[$i] }).Count | Should Be $fixtures.expectedCounts[$i] }
    }
    It 'uses IPv6 hexadecimal tails and first longest zero run' {
        (ConvertTo-ZonalAddress '::192.0.2.1') | Should Be '::c000:201'
        (ConvertTo-ZonalAddress '2001:0:0:1:0:0:1:1') | Should Be '2001::1:0:0:1:1'
    }
    It 'rejects additional permissive .NET IPv4 spellings' {
        foreach ($address in @('0x0a000001', '167772161', '0xa.0.0.1', '10.0.0.256', '[fd00::1]', '+10.0.0.1',
            '::ffff:10.1', '::ffff:0xa.0.0.1', '::ffff:010.0.0.1')) {
            { ConvertTo-ZonalAddress $address } | Should Throw
        }
    }
    It 'validates canonical GUIDs without accepting GUID aliases' {
        (ConvertTo-ZonalSubscriptionId ' ABCDEF01-2345-6789-ABCD-0123456789AB ') | Should Be 'abcdef01-2345-6789-abcd-0123456789ab'
        foreach ($value in @('abcdef0123456789abcd0123456789ab', '{abcdef01-2345-6789-abcd-0123456789ab}', 'my-sub', 1, $null)) {
            { ConvertTo-ZonalSubscriptionId $value } | Should Throw
        }
    }
    It 'decorates with a lowercase safe suffix and enforces UTF-8 bytes' {
        (Get-ZonalTargetIqn 'iqn.test:one' ' EASTUS-AZ3 ') | Should Be 'iqn.test:one:az-eastus-az3'
        { Get-ZonalTargetIqn 'iqn.test:one' 'zone/unsafe' } | Should Throw
        { Get-ZonalTargetIqn ('a' * 217) 'x' } | Should Not Throw
        { Get-ZonalTargetIqn ('a' * 219) 'x' } | Should Throw
        { Get-ZonalTargetIqn (([string][char]0x00e9) * 110) 'x' } | Should Throw
    }
    It 'validates ports before mutation' {
        (ConvertTo-ZonalPort '3260') | Should Be 3260
        foreach ($value in @('0', '-1', '65536', '3260x', '', ' 3260')) {
            { ConvertTo-ZonalPort $value } | Should Throw
        }
    }
}

Describe 'Bounded host-local DNS resolution' {
    BeforeEach {
        $script:dnsAttempts = 0
        Mock Start-Sleep {}
        Mock Start-ZonalDnsLookup { $script:dnsAttempts++; [pscustomobject]@{ Attempt = $script:dnsAttempts } }
    }
    It 'uses a five-second asynchronous wait, not a blocking resolver' {
        $lookup = New-Object PSObject
        $lookup | Add-Member ScriptMethod Wait { param($milliseconds) $script:waitMilliseconds = $milliseconds; $false }
        { Wait-ZonalDnsLookup $lookup } | Should Throw 'five-second'
        $script:waitMilliseconds | Should Be 5000
        $start = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Start-ZonalDnsLookup' }, $false)[0]
        $start.Extent.Text | Should Match 'GetHostAddressesAsync'
    }
    It 'reads an already completed .NET DNS task without invoking DNS' {
        $completion = New-Object 'System.Threading.Tasks.TaskCompletionSource[System.Net.IPAddress[]]'
        $completion.SetResult([System.Net.IPAddress[]]@([System.Net.IPAddress]::Parse('10.0.0.1'), [System.Net.IPAddress]::Parse('fd00::1')))
        (@(Wait-ZonalDnsLookup $completion.Task) -join ',') | Should Be '10.0.0.1,fd00::1'
    }
    It 'retries count validation and reuses only successful FQDN results' {
        Mock Wait-ZonalDnsLookup {
            if ($Lookup.Attempt -eq 1) { '10.0.0.1'; return }
            '10.0.0.3'; '10.0.0.1'; '10.0.0.2'
        }
        $cache = @{}
        (@(Resolve-ZonalVips 'portal.example' $cache) -join ',') | Should Be '10.0.0.1,10.0.0.2,10.0.0.3'
        $null = Resolve-ZonalVips 'PORTAL.EXAMPLE' $cache
        $script:dnsAttempts | Should Be 2
        Assert-MockCalled Start-Sleep -Scope It -Times 1 -Exactly -ParameterFilter { $Seconds -eq 1 }
    }
    It 'does not union incomplete rotating answers or cache failures' {
        Mock Wait-ZonalDnsLookup { "10.0.0.$($Lookup.Attempt)" }
        $cache = @{}
        { Resolve-ZonalVips 'portal.example' $cache } | Should Throw 'three attempts'
        $script:dnsAttempts | Should Be 3
        $cache.Count | Should Be 0
        Assert-MockCalled Start-Sleep -Scope It -Times 1 -Exactly -ParameterFilter { $Seconds -eq 1 }
        Assert-MockCalled Start-Sleep -Scope It -Times 1 -Exactly -ParameterFilter { $Seconds -eq 2 }
    }
    It 'retries timeouts and resolver errors with no FQDN fallback' {
        Mock Wait-ZonalDnsLookup { throw 'DNS timeout' }
        { Resolve-ZonalVips 'portal.example' @{} } | Should Throw 'DNS timeout'
        Assert-MockCalled Start-ZonalDnsLookup -Scope It -Times 3 -Exactly
    }
}

Describe 'Subscription-scoped mapping and metadata validation' {
    BeforeEach {
        $script:subscription = 'abcdef01-2345-6789-abcd-0123456789ab'
        $script:context = [pscustomobject]@{ Subscription = [pscustomobject]@{ Id = $script:subscription } }
        Mock Get-ZonalComputeMetadata { [pscustomobject]@{ zone = ' 2 '; subscriptionId = $script:subscription; location = ' EASTUS ' } }
        Mock Get-AzElasticSan { [pscustomobject]@{ Location = 'eastus' } }
        Mock Invoke-AzRestMethod {
            [pscustomobject]@{ StatusCode = 200; Content = '{"value":[{"name":"eastus","availabilityZoneMappings":[{"logicalZone":"2","physicalZone":"eastus-az3"}]}]}' }
        }
    }
    It 'uses the existing context and explicitly scoped SAN and ARM calls' {
        (Resolve-ZonalPhysicalZone $script:context $script:subscription 'rg' 'san') | Should Be 'eastus-az3'
        Assert-MockCalled Get-AzElasticSan -Scope It -Times 1 -Exactly -ParameterFilter { $SubscriptionId -eq $script:subscription -and $DefaultProfile -eq $script:context }
        Assert-MockCalled Invoke-AzRestMethod -Scope It -Times 1 -Exactly -ParameterFilter {
            $Method -eq 'GET' -and $Path -eq "/subscriptions/$script:subscription/locations?api-version=2022-12-01" -and $DefaultProfile -eq $script:context
        }
    }
    It 'rejects subscription mismatch before SAN or locations lookup' {
        Mock Get-ZonalComputeMetadata { [pscustomobject]@{ zone = '2'; subscriptionId = '11111111-1111-1111-1111-111111111111'; location = 'eastus' } }
        { Resolve-ZonalPhysicalZone $script:context $script:subscription 'rg' 'san' } | Should Throw 'subscription does not match'
        Assert-MockCalled Get-AzElasticSan -Scope It -Times 0 -Exactly
    }
    It 'rejects a non-zonal VM before other Azure lookups' {
        Mock Get-ZonalComputeMetadata { [pscustomobject]@{ zone = ''; subscriptionId = $script:subscription; location = 'eastus' } }
        { Resolve-ZonalPhysicalZone $script:context $script:subscription 'rg' 'san' } | Should Throw 'not availability-zone pinned'
        Assert-MockCalled Get-AzElasticSan -Scope It -Times 0 -Exactly
    }
    It 'rejects region mismatch and bad ARM status' {
        Mock Get-AzElasticSan { [pscustomobject]@{ Location = 'westus' } }
        { Resolve-ZonalPhysicalZone $script:context $script:subscription 'rg' 'san' } | Should Throw 'same region'
        Assert-MockCalled Invoke-AzRestMethod -Scope It -Times 0 -Exactly
    }
    It 'rejects ARM failures rather than using their payload' {
        Mock Invoke-AzRestMethod { [pscustomobject]@{ StatusCode = 403; Content = '{}' } }
        { Resolve-ZonalPhysicalZone $script:context $script:subscription 'rg' 'san' } | Should Throw 'HTTP 403'
    }
    It 'keeps the IMDS API, proxy bypass, and five-second deadlines explicit' {
        $definition = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-ZonalComputeMetadata' }, $false)[0]
        $definition.Extent.Text | Should Match 'api-version=2021-02-01'
        $definition.Extent.Text | Should Match '\.Proxy = \$null'
        $definition.Extent.Text | Should Match "\.Headers.Add\('Metadata', 'true'\)"
        $definition.Extent.Text | Should Match '\.Timeout = 5000'
        $definition.Extent.Text | Should Match '\.ReadWriteTimeout = 5000'
    }
}

Describe 'Existing layout safety, including redirected portals' {
    BeforeEach { $plan = New-TestPlan; $inventory = New-TestInventory $plan }
    It 'skips only correlated healthy persistent 11/11/10 even after redirects' {
        (Test-ZonalLayout $plan $inventory) | Should Be $true
    }
    It 'refuses missing correlation even when current endpoints look correct' {
        $inventory = New-TestInventory $plan 32 $false
        for ($i = 0; $i -lt 32; $i++) { $inventory.Connections[$inventory.Sessions[$i].SessionIdentifier][0].TargetAddress = $plan.Slots[$i] }
        { Test-ZonalLayout $plan $inventory } | Should Throw 'correlation'
    }
    It 'refuses partial and extra layouts' {
        foreach ($count in @(1, 31, 33)) {
            $inventory = New-TestInventory $plan $count
            { Test-ZonalLayout $plan $inventory } | Should Throw 'Partial'
        }
    }
    It 'refuses another suffix or the original undecorated IQN' {
        foreach ($iqn in @($plan.RawIqn, "$($plan.RawIqn):az-eastus-az1")) {
            $inventory = New-TestInventory $plan
            $inventory.Sessions[0].TargetNodeAddress = $iqn
            { Test-ZonalLayout $plan $inventory } | Should Throw 'foreign'
        }
    }
    It 'refuses stale registrations, persistence-only, and nonpersistent live sessions' {
        $empty = [pscustomobject]@{ Sessions = @(); Persistent = @(); Connections = @{}; Targets = @([pscustomobject]@{NodeAddress = $plan.Iqn}) }
        { Test-ZonalLayout $plan $empty } | Should Throw 'stale'
        $inventory.Sessions = @()
        { Test-ZonalLayout $plan $inventory } | Should Throw 'Partial'
        $inventory = New-TestInventory $plan
        $inventory.Sessions[0].IsPersistent = $false
        { Test-ZonalLayout $plan $inventory } | Should Throw 'Unhealthy'
    }
    It 'refuses missing connections, bad digests, and wrong portal allocation' {
        $inventory.Connections[$inventory.Sessions[0].SessionIdentifier] = @()
        { Test-ZonalLayout $plan $inventory } | Should Throw 'readiness'
        $inventory = New-TestInventory $plan
        $inventory.Persistent[0].HeaderDigest = 0
        { Test-ZonalLayout $plan $inventory } | Should Throw 'incompatible'
        $inventory = New-TestInventory $plan
        $inventory.Persistent[0].Address = $plan.Vips[1]
        { Test-ZonalLayout $plan $inventory } | Should Throw 'allocation'
    }
    It 'refuses duplicate and stale native session mappings' {
        $inventory.Persistent[0].SessionIdentifier = $inventory.Persistent[1].SessionIdentifier
        { Test-ZonalLayout $plan $inventory } | Should Throw 'duplicate'
        $inventory = New-TestInventory $plan
        $inventory.Persistent[0].SessionIdentifier = '1-ff'
        { Test-ZonalLayout $plan $inventory } | Should Throw 'Stale'
    }
    It 'rejects a foreign initiator port and persistent authentication options' {
        $inventory.Persistent[0].InitiatorPortNumber = 1
        { Test-ZonalLayout $plan $inventory } | Should Throw 'incompatible'
        $inventory = New-TestInventory $plan
        $inventory.Persistent[0].AuthType = 1
        { Test-ZonalLayout $plan $inventory } | Should Throw 'incompatible'
    }
}

Describe 'Read-only native inventory ABI' {
    BeforeEach {
        Initialize-ZonalNativeInventory
        $nativePlan = New-TestPlan
        $nativeLogin = New-Object 'ElasticSan.ZonalPersistentInventory+Login'
        $nativeLogin.TargetName = $nativePlan.Iqn
        $nativeLogin.InitiatorInstance = 'Root\ISCSIPRT\0000_0'
        $nativeLogin.InitiatorPortNumber = [uint32]::MaxValue
        $nativePortal = New-Object 'ElasticSan.ZonalPersistentInventory+Portal'
        $nativePortal.Address = $nativePlan.Vips[0]
        $nativePortal.Socket = 3260
        $nativeLogin.TargetPortal = $nativePortal
        $nativeOptions = New-Object 'ElasticSan.ZonalPersistentInventory+Options'
        $nativeOptions.InformationSpecified = 3
        $nativeOptions.LoginFlags = 2
        $nativeOptions.HeaderDigest = 1
        $nativeOptions.DataDigest = 1
        $nativeLogin.LoginOptions = $nativeOptions
        $nativeMapping = New-Object 'ElasticSan.ZonalPersistentInventory+Mapping'
        $nativeMapping.TargetName = $nativePlan.Iqn
        $nativeId = New-Object 'ElasticSan.ZonalPersistentInventory+SessionId'
        $nativeId.AdapterUnique = 1
        $nativeId.AdapterSpecific = 1
        $nativeMapping.SessionId = $nativeId
        $nativeStride = [System.Runtime.InteropServices.Marshal]::SizeOf($nativeLogin)
        $nativeSize = $nativeStride + [System.Runtime.InteropServices.Marshal]::SizeOf($nativeMapping)
        $nativeBuffer = [System.Runtime.InteropServices.Marshal]::AllocHGlobal($nativeSize)
        $nativeLogin.Mappings = [IntPtr]::Add($nativeBuffer, $nativeStride)
        [System.Runtime.InteropServices.Marshal]::StructureToPtr($nativeMapping, $nativeLogin.Mappings, $false)
        [System.Runtime.InteropServices.Marshal]::StructureToPtr($nativeLogin, $nativeBuffer, $false)
    }
    AfterEach {
        if ($nativeBuffer -ne [IntPtr]::Zero) { [System.Runtime.InteropServices.Marshal]::FreeHGlobal($nativeBuffer) }
    }
    It 'compiles the standalone interop without loading or calling the native DLL' {
        Initialize-ZonalNativeInventory
        $portalType = [type]'ElasticSan.ZonalPersistentInventory+Portal'
        [System.Runtime.InteropServices.Marshal]::SizeOf([Activator]::CreateInstance($portalType)) | Should Be 1026
        $mappingType = [type]'ElasticSan.ZonalPersistentInventory+Mapping'
        [System.Runtime.InteropServices.Marshal]::OffsetOf($mappingType, 'SessionId').ToInt32() | Should Be 1480
        $loginType = [type]'ElasticSan.ZonalPersistentInventory+Login'
        [System.Runtime.InteropServices.Marshal]::OffsetOf($loginType, 'TargetPortal').ToInt32() | Should Be 968
        [System.Runtime.InteropServices.Marshal]::OffsetOf($loginType, 'SecurityFlags').ToInt32() | Should Be 2000
        [System.Runtime.InteropServices.Marshal]::OffsetOf($loginType, 'Mappings').ToInt32() | Should Be 2008
    }
    It 'decodes the original persistent portal and its explicit native session mapping' {
        $records = @([ElasticSan.ZonalPersistentInventory]::Decode($nativeBuffer, $nativeSize, 1))
        $records.Count | Should Be 1
        $records[0].Address | Should Be '10.0.0.1'
        $records[0].Port | Should Be 3260
        $records[0].SessionIdentifier | Should Be '0000000000000001-0000000000000001'
        $nativeInventory = New-TestInventory $nativePlan
        $nativeInventory.Persistent[0] = $records[0]
        # The corresponding live connection is redirected to 10.20.30.40.
        (Test-ZonalLayout $nativePlan $nativeInventory) | Should Be $true
    }
    It 'refuses a counts-correct layout when one native persistent mapping is missing' {
        $nativeLogin.Mappings = [IntPtr]::Zero
        [System.Runtime.InteropServices.Marshal]::StructureToPtr($nativeLogin, $nativeBuffer, $false)
        $record = [ElasticSan.ZonalPersistentInventory]::Decode($nativeBuffer, $nativeSize, 1)[0]
        $record.SessionIdentifier | Should BeNullOrEmpty
        $nativeInventory = New-TestInventory $nativePlan
        $nativeInventory.Persistent[0] = $record
        { Test-ZonalLayout $nativePlan $nativeInventory } | Should Throw 'correlation'
    }
    It 'does not manufacture a session identifier from a zero native SessionId' {
        $nativeMapping.SessionId = New-Object 'ElasticSan.ZonalPersistentInventory+SessionId'
        [System.Runtime.InteropServices.Marshal]::StructureToPtr($nativeMapping, $nativeLogin.Mappings, $false)
        $record = [ElasticSan.ZonalPersistentInventory]::Decode($nativeBuffer, $nativeSize, 1)[0]
        $record.SessionIdentifier | Should BeNullOrEmpty
    }
    It 'rejects a native mapping for a different target' {
        $nativeMapping.TargetName = 'iqn.test:foreign'
        [System.Runtime.InteropServices.Marshal]::StructureToPtr($nativeMapping, $nativeLogin.Mappings, $false)
        { [ElasticSan.ZonalPersistentInventory]::Decode($nativeBuffer, $nativeSize, 1) } | Should Throw 'Conflicting'
    }
    It 'rejects truncated native arrays and out-of-buffer mapping pointers before dereferencing' {
        { [ElasticSan.ZonalPersistentInventory]::Decode($nativeBuffer, ($nativeStride - 1), 1) } | Should Throw 'Truncated'
        $nativeLogin.Mappings = [IntPtr]::Add($nativeBuffer, $nativeSize)
        [System.Runtime.InteropServices.Marshal]::StructureToPtr($nativeLogin, $nativeBuffer, $false)
        { [ElasticSan.ZonalPersistentInventory]::Decode($nativeBuffer, $nativeSize, 1) } | Should Throw 'Invalid persistent session mapping'
    }
}

Describe 'Opt-in connection orchestration and literal native argv' {
    BeforeEach {
        $script:nativeCalls = New-Object 'System.Collections.Generic.List[object]'
        $script:nativeSuccess = $true; $script:nativeExit = 0; $script:simulateLogin = $true
        $script:live = @(); $script:persistent = @()
        Mock Get-Service { [pscustomobject]@{ Status = 'Running' } }
        Mock Get-WindowsFeature { [pscustomobject]@{ InstallState = 'Installed' } }
        Mock Get-AzContext { [pscustomobject]@{ Subscription = [pscustomobject]@{ Id = 'abcdef01-2345-6789-abcd-0123456789ab' } } }
        Mock Resolve-ZonalPhysicalZone { 'eastus-az3' }
        Mock Get-AzElasticSanVolumeGroup { [pscustomobject]@{} }
        Mock Get-AzElasticSanVolume { [pscustomobject]@{ StorageTargetIqn = "iqn.test:$Name"; StorageTargetPortalHostname = "$Name.example"; StorageTargetPortalPort = 3260 } }
        Mock Start-ZonalDnsLookup { [pscustomobject]@{ HostName = $HostName } }
        Mock Wait-ZonalDnsLookup { 'fd00::1'; '10.0.0.2'; '10.0.0.1' }
        Mock Get-IscsiSession { $script:live }
        Mock Get-IscsiConnection { [pscustomobject]@{ ConnectionIdentifier = 'connection'; TargetAddress = '10.20.30.40'; TargetPortNumber = 3260 } }
        Mock Get-ZonalPersistentLogins { $script:persistent }
        Mock Get-IscsiTarget {}
        Mock Start-Sleep {}
        Mock Write-Host {}
    }
    It 'registers once and logs in 32 times with literal host, separate port, multipath and digests' {
        Connect-ZonalVolumes 'rg' 'san' 'vg' @('one')
        $script:nativeCalls.Count | Should Be 33
        ($script:nativeCalls[0] -join '|') | Should Be 'AddTarget|iqn.test:one:az-eastus-az3|*|10.0.0.1|3260|*|0|*|*|*|*|*|*|*|*|*|0'
        $vips = @('10.0.0.1', '10.0.0.2', 'fd00::1')
        for ($i = 0; $i -lt 32; $i++) {
            ($script:nativeCalls[$i + 1] -join '|') | Should Be "PersistentLoginTarget|iqn.test:one:az-eastus-az3|t|$($vips[$i % 3])|3260|Root\ISCSIPRT\0000_0|-1|*|0x00000002|1|1|*|*|*|*|*|*|*|0"
        }
        Assert-MockCalled Get-ZonalPersistentLogins -Scope It -Times 33 -Exactly
    }
    It 'finishes DNS discovery for every volume before mutation' {
        Mock Wait-ZonalDnsLookup { if ($Lookup.HostName -eq 'two.example') { throw 'second volume DNS failed' }; '10.0.0.1'; '10.0.0.2'; 'fd00::1' }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', 'two') } | Should Throw 'second volume DNS failed'
        $script:nativeCalls.Count | Should Be 0
    }
    It 'finishes existing-state checks for every volume before mutation' {
        $script:live = @(New-TestSession 1 'iqn.test:two')
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', 'two') } | Should Throw 'approved disconnect/recovery'
        $script:nativeCalls.Count | Should Be 0
    }
    It 'rejects duplicate selected volumes and duplicate raw IQNs before mutation' {
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', 'ONE') } | Should Throw 'duplicate selected volume'
        Mock Get-AzElasticSanVolume { [pscustomobject]@{ StorageTargetIqn = 'iqn.test:shared'; StorageTargetPortalHostname = 'one.example'; StorageTargetPortalPort = 3260 } }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', 'two') } | Should Throw 'same raw IQN'
        $script:nativeCalls.Count | Should Be 0
    }
    It 'rejects invalid later ports before any native mutation' {
        Mock Get-AzElasticSanVolume { [pscustomobject]@{ StorageTargetIqn = "iqn.test:$Name"; StorageTargetPortalHostname = "$Name.example"; StorageTargetPortalPort = $(if ($Name -eq 'two') { 65536 } else { 3260 }) } }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', 'two') } | Should Throw 'Invalid target port'
        $script:nativeCalls.Count | Should Be 0
    }
    It 'does not treat native process exit zero with API failure as success' {
        $script:nativeSuccess = $false
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') } | Should Throw 'unrecognized status'
        $script:nativeCalls.Count | Should Be 1
    }
    It 'requires a successful process status too' {
        $script:nativeExit = 5
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') } | Should Throw 'exit 5'
        $script:nativeCalls.Count | Should Be 1
    }
    It 'does not assume successful native requests establish sessions' {
        $script:simulateLogin = $false
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') } | Should Throw 'not established'
        $script:nativeCalls.Count | Should Be 2
        Assert-MockCalled Start-Sleep -Scope It -Times 4 -Exactly
    }
    It 'waits for a new visible session and its persistence to become ready before adding another' {
        $script:inventoryReads = 0
        Mock Get-IscsiSession {
            $script:inventoryReads++
            if ($script:live.Count -gt 0) {
                $script:live[-1].IsConnected = $script:inventoryReads -gt 2
            }
            $script:live
        }
        Mock Get-ZonalPersistentLogins {
            if ($script:inventoryReads -eq 3) { return }
            $script:persistent
        }
        Connect-ZonalVolumes 'rg' 'san' 'vg' @('one')
        $script:nativeCalls.Count | Should Be 33
        Assert-MockCalled Start-Sleep -Scope It -Times 2 -Exactly
    }
    It 'stops after the bounded wait if the newly visible session never becomes healthy' {
        Mock Get-IscsiSession {
            foreach ($session in $script:live) { $session.IsConnected = $false }
            $script:live
        }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') } | Should Throw 'not established'
        $script:nativeCalls.Count | Should Be 2
        Assert-MockCalled Start-Sleep -Scope It -Times 4 -Exactly
    }
    It 'skips a provably complete layout without native mutations' {
        $inventory = New-TestInventory (New-TestPlan)
        $script:live = $inventory.Sessions; $script:persistent = $inventory.Persistent
        Connect-ZonalVolumes 'rg' 'san' 'vg' @('one')
        $script:nativeCalls.Count | Should Be 0
    }
    It 'fails closed if native persistent inventory cannot be read' {
        Mock Get-ZonalPersistentLogins { throw 'native inventory unavailable' }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') } | Should Throw 'native inventory unavailable'
        $script:nativeCalls.Count | Should Be 0
    }
    It 'fails closed for malformed target inventory' {
        Mock Get-ZonalPersistentLogins { [pscustomobject]@{ Address = '10.0.0.1' } }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') } | Should Throw 'ambiguous target identity'
        $script:nativeCalls.Count | Should Be 0
    }
    It 'preflights a shared FQDN once before connecting either volume' {
        Mock Get-AzElasticSanVolume {
            if ($script:nativeCalls.Count -ne 0) { throw 'mutation before all-volume discovery' }
            [pscustomobject]@{ StorageTargetIqn = "iqn.test:$Name"; StorageTargetPortalHostname = 'shared.example'; StorageTargetPortalPort = 3260 }
        }
        Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', 'two')
        $script:nativeCalls.Count | Should Be 66
        Assert-MockCalled Start-ZonalDnsLookup -Scope It -Times 1 -Exactly
    }
    It 'requires terminal native success rather than a success line preceding an error' {
        Mock iscsicli { $global:LASTEXITCODE = 0; 'The operation completed successfully.'; 'The operation failed.' }
        { Invoke-ZonalIscsiCli -Arguments @('AddTarget') } | Should Throw 'unrecognized status'
    }
}

Describe 'Actual script entrypoint and legacy parity' {
    BeforeEach {
        $script:nativeCalls = New-Object 'System.Collections.Generic.List[object]'
        $script:nativeSuccess = $true; $script:nativeExit = 0; $script:simulateLogin = $false
        Mock Get-Service { [pscustomobject]@{ Status = 'Running' } }
        Mock Get-WindowsFeature { [pscustomobject]@{ InstallState = 'Installed' } }
        Mock Get-AzElasticSanVolumeGroup { [pscustomobject]@{} }
        Mock Get-AzElasticSanVolume { [pscustomobject]@{ StorageTargetIqn = 'iqn.test:ONE'; StorageTargetPortalHostname = 'PORTAL.EXAMPLE'; StorageTargetPortalPort = 3260 } }
        Mock Get-IscsiSession {}
        Mock Get-AzContext { throw 'opt-in context boundary reached' }
        Mock Write-Host {}
    }
    It 'preserves legacy omitted count, original FQDN case, and native argv' {
        . $connectPath 'rg' 'san' 'vg' @('one')
        $script:nativeCalls.Count | Should Be 33
        ($script:nativeCalls[0] -join '|') | Should Be 'AddTarget|iqn.test:ONE|*|PORTAL.EXAMPLE|3260|*|0|*|*|*|*|*|*|*|*|*|0'
        ($script:nativeCalls[1] -join '|') | Should Be 'PersistentLoginTarget|iqn.test:one|t|portal.example|3260|Root\ISCSIPRT\0000_0|-1|*|0x00000002|1|1|*|*|*|*|*|*|*|0'
        Assert-MockCalled Get-AzContext -Scope It -Times 0 -Exactly
    }
    It 'preserves fifth positional session count and explicit disabled switch' {
        . $connectPath 'rg' 'san' 'vg' @('one') 2 -EnableZonalAffinity:$false
        $script:nativeCalls.Count | Should Be 3
        Assert-MockCalled Get-AzContext -Scope It -Times 0 -Exactly
    }
    It 'preserves legacy skip behavior' {
        Mock Get-IscsiSession { [pscustomobject]@{ TargetNodeAddress = 'iqn.test:one' } }
        . $connectPath 'rg' 'san' 'vg' @('one') 1
        $script:nativeCalls.Count | Should Be 0
    }
    It 'rejects enabled wrong explicit counts, including parameter range zero' {
        foreach ($count in @(0, 1, 31)) {
            { . $connectPath 'rg' 'san' 'vg' @('one') $count -EnableZonalAffinity } | Should Throw
        }
        Assert-MockCalled Get-Service -Scope It -Times 0 -Exactly
        $script:nativeCalls.Count | Should Be 0
    }
    It 'routes enabled omitted and explicit 32 counts into the new path' {
        { . $connectPath 'rg' 'san' 'vg' @('one') -EnableZonalAffinity } | Should Throw 'opt-in context boundary reached'
        { . $connectPath 'rg' 'san' 'vg' @('one') 32 -EnableZonalAffinity } | Should Throw 'opt-in context boundary reached'
        Assert-MockCalled Get-AzContext -Scope It -Times 2 -Exactly
        $script:nativeCalls.Count | Should Be 0
    }
    It 'runs the enabled entrypoint through full mapping, DNS, native login and persistence verification' {
        $script:simulateLogin = $true
        $script:live = @(); $script:persistent = @()
        Mock Get-AzContext { [pscustomobject]@{ Subscription = [pscustomobject]@{ Id = 'abcdef01-2345-6789-abcd-0123456789ab' } } }
        Mock Get-AzElasticSan { [pscustomobject]@{ Location = 'eastus' } }
        Mock Invoke-AzRestMethod {
            [pscustomobject]@{ StatusCode = 200; Content = '{"value":[{"name":"eastus","availabilityZoneMappings":[{"logicalZone":"2","physicalZone":"eastus-az3"}]}]}' }
        }
        Mock Get-IscsiSession { $script:live }
        Mock Get-IscsiConnection { [pscustomobject]@{ ConnectionIdentifier = 'connection'; TargetAddress = '10.20.30.40'; TargetPortNumber = 3260 } }
        Mock Get-IscsiTarget {}
        # The file defines its helpers before the first external boundary.
        # Install their mocks here, after definition, rather than rewriting the
        # entrypoint or introducing a production-only test flag.
        Mock Get-Service {
            Mock Get-ZonalComputeMetadata { [pscustomobject]@{ zone = '2'; subscriptionId = 'abcdef01-2345-6789-abcd-0123456789ab'; location = 'eastus' } }
            Mock Start-ZonalDnsLookup { [pscustomobject]@{} }
            Mock Wait-ZonalDnsLookup { 'fd00::1'; '10.0.0.2'; '10.0.0.1' }
            Mock Get-ZonalPersistentLogins { $script:persistent }
            [pscustomobject]@{ Status = 'Running' }
        }
        . $connectPath 'rg' 'san' 'vg' @('one') -EnableZonalAffinity
        $script:nativeCalls.Count | Should Be 33
        $script:nativeCalls[1][1] | Should Be 'iqn.test:one:az-eastus-az3'
        $script:nativeCalls[3][3] | Should Be 'fd00::1'
        Assert-MockCalled Get-ZonalPersistentLogins -Scope It -Times 33 -Exactly
        Assert-MockCalled Invoke-AzRestMethod -Scope It -Times 1 -Exactly
    }
}
