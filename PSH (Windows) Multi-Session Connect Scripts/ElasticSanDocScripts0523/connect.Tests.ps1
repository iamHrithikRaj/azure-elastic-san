$connectPath = Join-Path $PSScriptRoot 'connect.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($connectPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -like '*-Zonal*' }, $false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}

# Azure and native host commands are never invoked by this suite.
function Get-AzContext { [CmdletBinding()]param() }
function Get-AzElasticSanVolumeGroup { [CmdletBinding()]param($ResourceGroupName, $ElasticSanName, $Name) }
function Get-AzElasticSanVolume { [CmdletBinding()]param($ResourceGroupName, $ElasticSanName, $VolumeGroupName, $Name) }
function Get-WindowsFeature { [CmdletBinding()]param($Name) }
function Get-IscsiSession { [CmdletBinding()]param() }
function Get-IscsiConnection { [CmdletBinding()]param($IscsiSession) }
function Get-IscsiTarget { [CmdletBinding()]param() }
function iscsicli {
    $argv = @($args)
    $script:nativeCalls.Add($argv)
    $global:LASTEXITCODE = $script:nativeExit
    if ($script:simulateLogin -and $script:nativeExit -eq 0 -and $script:nativeOutput -eq 'The operation completed successfully.' -and $argv[0] -eq 'PersistentLoginTarget') {
        $session = [pscustomobject]@{
            SessionIdentifier = ('0000000000000001-{0:x16}' -f ($script:existing.Count + 1))
            TargetNodeAddress = $argv[1]; IsConnected = $true; IsPersistent = $true
            IsHeaderDigest = $true; IsDataDigest = $true; NumberOfConnections = 1
        }
        $script:existing += $session
        $script:persistent += [pscustomobject]@{
            TargetName = $argv[1]; Address = $argv[3]; Port = [int]$argv[4]; SessionIdentifier = $null
            InitiatorInstance = 'Root\ISCSIPRT\0000_0'; IsInformationalSession = $false
            InitiatorPortNumber = [uint32]::MaxValue; Version = 0; SecurityFlags = 0; AuthType = 0
            InformationSpecified = 3; LoginFlags = 2; HeaderDigest = 1; DataDigest = 1
        }
    }
    $script:nativeOutput
}

function New-TestLocations([string]$Mappings = '[{"logicalZone":"2","physicalZone":"eastus-az3"}]') {
    ,(ConvertFrom-Json ('{"value":[{"name":"eastus","availabilityZoneMappings":' + $Mappings + '}]}')).value
}

Describe 'Mapping and complete IQN contract' {
    It 'normalizes mapping values without changing the service identity' {
        $locations = New-TestLocations '[{"logicalZone":" 2 ","physicalZone":" EASTUS-AZ3 "}]'
        (Get-ZonalPhysicalZone $locations ' EASTUS ' ' 2 ') | Should Be 'eastus-az3'
        (Get-ZonalTargetIqn 'iqn.2023-01.example:volume-1' ' EASTUS-AZ3 ') | Should Be 'iqn.2023-01.example:volume-1:az-eastus-az3'
    }
    It 'rejects missing, malformed and duplicate mappings including unused entries' {
        foreach ($mappings in @(
            '[]', '{}', 'null', '[null]', '[{"logicalZone":"2"}]',
            '[{"logicalZone":2,"physicalZone":"eastus-az3"}]',
            '[{"logicalZone":"1","physicalZone":"eastus-az1"}]',
            '[{"logicalZone":"2","physicalZone":"eastus-az3"},{"logicalZone":" 2 ","physicalZone":"eastus-az1"}]',
            '[{"logicalZone":"2","physicalZone":"eastus-az3"},{"logicalZone":"1","physicalZone":" EASTUS-AZ3 "}]',
            '[{"logicalZone":"2","physicalZone":"eastus-az3"},{"logicalZone":"1","physicalZone":"bad/zone"}]'
        )) {
            { Get-ZonalPhysicalZone (New-TestLocations $mappings) 'eastus' '2' } | Should Throw
        }
    }
    It 'rejects absent, duplicate and malformed regions' {
        $region = (New-TestLocations)[0]
        foreach ($locations in @(@(), @($region, $region), @($null), @([pscustomobject]@{name=1}))) {
            { Get-ZonalPhysicalZone $locations 'eastus' '2' } | Should Throw
        }
        { Get-ZonalPhysicalZone @($region) 'westus' '2' } | Should Throw
        { Get-ZonalPhysicalZone $region 'eastus' '2' } | Should Throw
    }
    It 'fails closed on the whole IQN rather than lowercasing an opaque identity' {
        foreach ($iqn in @('iqn.test:UPPER', 'IQN.test:one', 'iqn.test:one/part', 'iqn.test:one_part',
            'iqn.test:one@part', 'iqn.test:one:az-old', 'iqn.test:one ', "iqn.test:one`n", '',
            "iqn.test:$([char]0x00e9)", 'not-an-iqn', $null)) {
            { Get-ZonalTargetIqn $iqn 'eastus-az3' } | Should Throw
        }
    }
    It 'accepts 223 UTF-8 bytes and rejects 224 bytes after decoration' {
        (Get-ZonalTargetIqn ('iqn.' + ('a' * 214)) 'x').Length | Should Be 223
        { Get-ZonalTargetIqn ('iqn.' + ('a' * 215)) 'x' } | Should Throw '223 UTF-8 bytes'
        foreach ($zone in @('', 'bad/zone', "zone`nother", '_zone')) {
            { Get-ZonalTargetIqn 'iqn.test:one' $zone } | Should Throw
        }
    }
    It 'accepts only canonical subscription GUIDs' {
        (ConvertTo-ZonalSubscriptionId ' ABCDEF01-2345-6789-ABCD-0123456789AB ') | Should Be 'abcdef01-2345-6789-abcd-0123456789ab'
        foreach ($value in @('abcdef0123456789abcd0123456789ab', '{abcdef01-2345-6789-abcd-0123456789ab}', 'my-sub', 1, $null)) {
            { ConvertTo-ZonalSubscriptionId $value } | Should Throw
        }
    }
}

Describe 'Bounded metadata and provider requests' {
    It 'configures IMDS with Metadata header, proxy bypass and a complete-response timeout' {
        $client = New-ZonalMetadataClient
        try {
            $client.Timeout.TotalSeconds | Should Be 5
            ($client.DefaultRequestHeaders.GetValues('Metadata') -join '') | Should Be 'true'
        } finally { $client.Dispose() }
        $definition = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'New-ZonalMetadataClient' }, $false)[0]
        $definition.Extent.Text | Should Match '\.UseProxy = \$false'
        $definition = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-ZonalComputeMetadata' }, $false)[0]
        $definition.Extent.Text | Should Match '169\.254\.169\.254/metadata/instance/compute\?api-version=2021-02-01'
        $definition.Extent.Text | Should Match 'GetStringAsync'
    }
    It 'returns real in-process objects without serializing the authentication context' {
        $value = [pscustomobject]@{ TokenCachePlaceholder = New-Object object }
        $result = Invoke-ZonalProviderRequest 'Write-Output' @{ InputObject = $value }
        [object]::ReferenceEquals($result.TokenCachePlaceholder, $value.TokenCachePlaceholder) | Should Be $true
    }
    It 'propagates provider errors and cancels a stalled local pipeline' {
        { Invoke-ZonalProviderRequest 'Write-Error' @{ Message = 'provider failed' } } | Should Throw 'provider failed'
        $watch = [Diagnostics.Stopwatch]::StartNew()
        { Invoke-ZonalProviderRequest 'Start-Sleep' @{ Seconds = 30 } -TimeoutSeconds 1 } | Should Throw 'deadline'
        $watch.Elapsed.TotalSeconds | Should BeLessThan 10
    }
    It 'returns at the deadline even when a provider ignores cancellation' {
        Initialize-ZonalProviderCancellation
        $watch = [Diagnostics.Stopwatch]::StartNew()
        { Invoke-ZonalProviderRequest 'Invoke-Expression' @{
            Command = '[System.Threading.Thread]::Sleep(4000); "late provider result"'
        } -TimeoutSeconds 1 } | Should Throw 'result will be discarded'
        $watch.Elapsed.TotalSeconds | Should BeLessThan 3
    }
    It 'disposes the timed-out pipeline after its outstanding read finally exits' {
        Initialize-ZonalProviderCancellation
        $pipeline = [System.Management.Automation.PowerShell]::Create()
        $started = New-Object System.Threading.ManualResetEvent($false)
        $null = $pipeline.AddScript('param($started) $null = $started.Set(); [System.Threading.Thread]::Sleep(1000)').AddArgument($started)
        $pending = $pipeline.BeginInvoke()
        $started.WaitOne(5000) | Should Be $true
        [ElasticSan.ZonalProviderCancellation]::StopAndDispose($pipeline, $pending)
        $disposed = $false
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while (!$disposed -and $watch.Elapsed.TotalSeconds -lt 5) {
            try { $null = $pending.AsyncWaitHandle.WaitOne(0) }
            catch [System.ObjectDisposedException] { $disposed = $true }
            if (!$disposed) { Start-Sleep -Milliseconds 50 }
        }
        $disposed | Should Be $true
        { $pipeline.BeginInvoke() } | Should Throw
        $started.Dispose()
    }
}

Describe 'Whole-batch mapping and VIP orchestration' {
    BeforeEach {
        $script:nativeCalls = New-Object 'System.Collections.Generic.List[object]'
        $script:nativeExit = 0
        $script:nativeOutput = 'The operation completed successfully.'
        $script:subscription = 'abcdef01-2345-6789-abcd-0123456789ab'
        $script:context = [pscustomobject]@{ Subscription = [pscustomobject]@{ Id = $script:subscription } }
        $script:compute = [pscustomobject]@{ zone = '2'; subscriptionId = $script:subscription; location = 'eastus' }
        $script:sanLocation = 'eastus'
        $script:armStatus = 200
        $script:armContent = '{"value":[{"name":"eastus","availabilityZoneMappings":[{"logicalZone":"2","physicalZone":"eastus-az3"}]}]}'
        $script:badSecond = ''
        $script:duplicateIqn = $false
        $script:requests = New-Object 'System.Collections.Generic.List[object]'
        $script:existing = @()
        $script:persistent = @()
        $script:simulateLogin = $true
        Mock Get-Service { [pscustomobject]@{ Status = 'Running' } }
        Mock Get-WindowsFeature { [pscustomobject]@{ InstallState = 'Installed' } }
        Mock Get-IscsiSession { $script:existing }
        Mock Get-IscsiConnection { [pscustomobject]@{ ConnectionIdentifier = 'connection'; TargetAddress = '10.20.30.40'; TargetPortNumber = 3260 } }
        Mock Get-IscsiTarget {}
        Mock Get-ZonalPersistentLogins { $script:persistent }
        Mock Start-ZonalDnsLookup { [pscustomobject]@{ HostName = $HostName } }
        Mock Wait-ZonalDnsLookup { 'fd00::1'; '10.0.0.2'; '10.0.0.1' }
        Mock Start-Sleep {}
        Mock Write-Host {}
        Mock Get-AzContext { $script:context }
        Mock Get-ZonalComputeMetadata { $script:compute }
        Mock Invoke-ZonalProviderRequest {
                $script:requests.Add([pscustomobject]@{ Command = $Command; Parameters = $Parameters })
                if ($script:nativeCalls.Count) { throw 'mutation preceded whole-batch discovery' }
                switch ($Command) {
                    'Get-AzElasticSan' { [pscustomobject]@{ Location = $script:sanLocation } }
                    'Invoke-AzRestMethod' { [pscustomobject]@{ StatusCode = $script:armStatus; Content = $script:armContent } }
                    'Get-AzElasticSanVolumeGroup' { [pscustomobject]@{} }
                    'Get-AzElasticSanVolume' {
                        $name = $Parameters.Name
                        if ($name -eq 'two' -and $script:badSecond -eq 'provider') { throw 'second volume provider deadline' }
                        [pscustomobject]@{
                            StorageTargetIqn = $(if ($script:duplicateIqn) { 'iqn.test:shared' } elseif ($name -eq 'two' -and $script:badSecond -eq 'iqn') { 'iqn.test:UPPER' } else { "iqn.test:$name" })
                            StorageTargetPortalHostname = $(if ($name -eq 'two' -and $script:badSecond -eq 'host') { 'bad host' } else { "$name.example" })
                            StorageTargetPortalPort = $(if ($name -eq 'two' -and $script:badSecond -eq 'port') { 65536 } else { 3260 })
                        }
                    }
                    default { throw "Unexpected provider request: $Command" }
                }
        }
    }
    It 'uses explicit subscription and DefaultProfile for every provider read' {
        Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', 'two') 32
        $script:requests.Count | Should Be 5
        foreach ($request in $script:requests) {
            [object]::ReferenceEquals($request.Parameters.DefaultProfile, $script:context) | Should Be $true
            if ($request.Command -eq 'Invoke-AzRestMethod') {
                $request.Parameters.Method | Should Be 'GET'
                $request.Parameters.Path | Should Be "/subscriptions/$script:subscription/locations?api-version=2022-12-01"
            } else { $request.Parameters.SubscriptionId | Should Be $script:subscription }
        }
        $script:nativeCalls.Count | Should Be 66
        ($script:nativeCalls[0] -join '|') | Should Be 'AddTarget|iqn.test:one:az-eastus-az3|*|10.0.0.1|3260|*|0|*|*|*|*|*|*|*|*|*|0'
        $vips = @('10.0.0.1', '10.0.0.2', 'fd00::1')
        for ($i = 0; $i -lt 32; $i++) {
            ($script:nativeCalls[$i + 1] -join '|') | Should Be "PersistentLoginTarget|iqn.test:one:az-eastus-az3|t|$($vips[$i % 3])|3260|Root\ISCSIPRT\0000_0|-1|*|0x00000002|1|1|*|*|*|*|*|*|*|0"
        }
        $script:nativeCalls[34][1] | Should Be 'iqn.test:two:az-eastus-az3'
    }
    It 'rejects every non-32 enabled count before any discovery or mutation' {
        foreach ($count in @(0,1,2,31,33)) {
            { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') $count } | Should Throw 'exactly 32'
        }
        $script:requests.Count | Should Be 0
        $script:nativeCalls.Count | Should Be 0
    }
    It 'rejects invalid parameter counts before discovery or mutation' {
        foreach ($count in @(0, 1, 31, 33)) {
            { . $connectPath 'rg' 'san' 'vg' @('one') $count -EnableZonalAffinity } | Should Throw
        }
        $script:requests.Count | Should Be 0
        $script:nativeCalls.Count | Should Be 0
    }
    It 'rejects a mismatched subscription and non-zonal VM before ARM discovery' {
        $script:compute.subscriptionId = '11111111-1111-1111-1111-111111111111'
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'subscription does not match'
        $script:compute.subscriptionId = $script:subscription
        $script:compute.zone = ''
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'not availability-zone pinned'
        $script:requests.Count | Should Be 0
        $script:nativeCalls.Count | Should Be 0
    }
    It 'rejects a mismatched region, failed ARM response and malformed JSON' {
        $script:sanLocation = 'westus'
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'same region'
        $script:requests.Count | Should Be 1
        $script:sanLocation = 'eastus'
        $script:armStatus = 403
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'HTTP 403'
        $script:armStatus = 200
        $script:armContent = '{'
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw
        $script:nativeCalls.Count | Should Be 0
    }
    It 'preflights every volume IQN, FQDN, port and provider result before native mutation' {
        foreach ($failure in @(
            @{ Kind = 'iqn'; Message = 'complete decorated IQN' },
            @{ Kind = 'host'; Message = 'FQDN' },
            @{ Kind = 'port'; Message = 'Invalid target port' },
            @{ Kind = 'provider'; Message = 'second volume provider deadline' }
        )) {
            $script:badSecond = $failure.Kind
            { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', 'two') 32 } | Should Throw $failure.Message
            $script:nativeCalls.Count | Should Be 0
        }
    }
    It 'rejects empty and duplicate selections and duplicate service identities' {
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', 'ONE') 32 } | Should Throw 'duplicate selected volume'
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', ' ') 32 } | Should Throw 'Empty'
        $script:duplicateIqn = $true
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', 'two') 32 } | Should Throw 'same raw IQN'
        $script:nativeCalls.Count | Should Be 0
    }
    It 'refuses partial, undecorated and differently decorated state on any selected volume' {
        foreach ($iqn in @('iqn.test:two:az-eastus-az3', 'iqn.test:two', 'iqn.test:two:az-eastus-az1')) {
            $script:existing = @([pscustomobject]@{ TargetNodeAddress = $iqn; SessionIdentifier = '1-1' })
            { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', 'two') 32 } | Should Throw
            $script:nativeCalls.Count | Should Be 0
        }
    }
    It 'stops native failures and reports partial-progress recovery guidance' {
        $script:nativeExit = 5
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'exit 5'
        $script:nativeCalls.Count | Should Be 1
        $script:nativeCalls.Clear()
        $script:nativeExit = 0
        $script:nativeOutput = 'The operation failed.'
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'may remain'
        $script:nativeCalls.Count | Should Be 1
    }
    It 'finishes all DNS preflight before reading existing state or mutating' {
        Mock Wait-ZonalDnsLookup {
            if ($Lookup.HostName -eq 'two.example') { throw 'second volume DNS failed' }
            '10.0.0.1'; '10.0.0.2'; 'fd00::1'
        }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one', 'two') 32 } | Should Throw 'second volume DNS failed'
        $script:nativeCalls.Count | Should Be 0
        Assert-MockCalled Get-IscsiSession -Scope It -Times 0 -Exactly
    }
    It 'fails closed on unavailable or malformed read-only inventory' {
        Mock Get-ZonalPersistentLogins { throw 'native inventory unavailable' }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'native inventory unavailable'
        Mock Get-ZonalPersistentLogins { [pscustomobject]@{ Address = '10.0.0.1' } }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'ambiguous target identity'
        $script:nativeCalls.Count | Should Be 0
    }
    It 'waits for the first session and its persistence before submitting another login' {
        $script:reads = 0
        Mock Get-IscsiSession {
            $script:reads++
            if ($script:existing.Count) { $script:existing[-1].IsConnected = $script:reads -gt 2 }
            $script:existing
        }
        Mock Get-ZonalPersistentLogins { if ($script:reads -ne 3) { $script:persistent } }
        Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32
        $script:nativeCalls.Count | Should Be 33
        Assert-MockCalled Start-Sleep -Scope It -Times 2 -Exactly
    }
    It 'stops after five observations if a native-success login never appears' {
        $script:simulateLogin = $false
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'not established'
        $script:nativeCalls.Count | Should Be 2
        Assert-MockCalled Get-IscsiSession -Scope It -Times 6 -Exactly
        Assert-MockCalled Start-Sleep -Scope It -Times 4 -Exactly
    }
    It 'stops after five observations if the first session never becomes connected' {
        Mock Get-IscsiSession {
            foreach ($session in $script:existing) { $session.IsConnected = $false }
            $script:existing
        }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'not established'
        $script:nativeCalls.Count | Should Be 2
        Assert-MockCalled Start-Sleep -Scope It -Times 4 -Exactly
    }
    It 'reports partial apply without rollback claims when later inventory fails' {
        Mock Get-ZonalPersistentLogins {
            if ($script:existing.Count -gt 1) { throw 'inventory read failed after login' }
            $script:persistent
        }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'may remain'
        $script:nativeCalls.Count | Should Be 3
        @($script:nativeCalls | Where-Object { $_[0] -match 'Disconnect|Remove|Logout' }).Count | Should Be 0
    }
    It 'rejects terminal API failures even if a prior line reported success' {
        $script:nativeOutput = "The operation completed successfully.`nThe operation failed."
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'unrecognized status'
        $script:nativeCalls.Count | Should Be 1
    }
    It 'refuses an uncorrelated later run and skips only after complete native correlation' {
        Mock Wait-ZonalDnsLookup { '10.0.0.3'; '10.0.0.2'; '10.0.0.1' }
        Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32
        $script:nativeCalls.Clear()
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'correlation'
        $script:nativeCalls.Count | Should Be 0
        for ($i = 0; $i -lt 32; $i++) { $script:persistent[$i].SessionIdentifier = $script:existing[$i].SessionIdentifier }
        Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32
        $script:nativeCalls.Count | Should Be 0
    }
    It 'never installs prerequisites or submits login when the initiator or MPIO is unavailable' {
        Mock Get-Service { [pscustomobject]@{ Status = 'Stopped' } }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'running iSCSI'
        Mock Get-Service { [pscustomobject]@{ Status = 'Running' } }
        Mock Get-WindowsFeature { [pscustomobject]@{ InstallState = 'Available' } }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'already be installed'
        $script:nativeCalls.Count | Should Be 0
    }
    It 'fails immediately on visible incompatible persistence even while a session is pending' {
        Mock Get-IscsiSession {
            foreach ($session in $script:existing) { $session.IsConnected = $false }
            $script:existing
        }
        Mock Get-ZonalPersistentLogins {
            foreach ($record in $script:persistent) { $record.Port = 3261 }
            $script:persistent
        }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'incompatible'
        $script:nativeCalls.Count | Should Be 2
        Assert-MockCalled Start-Sleep -Scope It -Times 0 -Exactly
    }
    It 'refuses concurrent newly observed sessions without submitting another login' {
        Mock Get-IscsiSession {
            $script:existing
            if ($script:existing.Count) {
                [pscustomobject]@{ TargetNodeAddress = $script:existing[0].TargetNodeAddress; SessionIdentifier = '1-ff' }
            }
        }
        { Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32 } | Should Throw 'Concurrent or ambiguous'
        $script:nativeCalls.Count | Should Be 2
    }
    It 'preserves the service-provided non-default port for static and every persistent portal' {
        Mock Invoke-ZonalProviderRequest {
            switch ($Command) {
                'Get-AzElasticSan' { [pscustomobject]@{ Location = $script:sanLocation } }
                'Invoke-AzRestMethod' { [pscustomobject]@{ StatusCode = $script:armStatus; Content = $script:armContent } }
                'Get-AzElasticSanVolumeGroup' { [pscustomobject]@{} }
                'Get-AzElasticSanVolume' { [pscustomobject]@{
                    StorageTargetIqn = 'iqn.test:one'; StorageTargetPortalHostname = 'one.example'; StorageTargetPortalPort = 3261
                } }
                default { throw "Unexpected provider request $Command" }
            }
        }
        Connect-ZonalVolumes 'rg' 'san' 'vg' @('one') 32
        $script:nativeCalls.Count | Should Be 33
        foreach ($call in $script:nativeCalls) { $call[4] | Should Be '3261' }
    }
}

    foreach ($countCase in @(@{ Label = 'omitted default'; Count = $null; Expected = 32 },
        @{ Label = 'explicit 32'; Count = 32; Expected = 32 })) {
        Describe "Actual enabled entrypoint: $($countCase.Label) count" {
            It 'maps both selected volumes before verifying 32 numeric-VIP logins per volume' {
                $script:nativeCalls = New-Object 'System.Collections.Generic.List[object]'
                $script:nativeExit = 0
                $script:nativeOutput = 'The operation completed successfully.'
                $script:simulateLogin = $true
                $script:existing = @(); $script:persistent = @()
                Mock Get-IscsiSession { $script:existing }
                Mock Get-IscsiConnection { [pscustomobject]@{ ConnectionIdentifier = 'connection'; TargetAddress = '10.20.30.40'; TargetPortNumber = 3260 } }
                Mock Get-IscsiTarget {}
                Mock Get-Service { [pscustomobject]@{ Status = 'Running' } }
                Mock Get-WindowsFeature { [pscustomobject]@{ InstallState = 'Installed' } }
                Mock Write-Host {}
                Mock Get-AzContext {
                    # Install these after the file has defined the helpers. Each
                    # Describe owns one entrypoint invocation (Pester 3 mock scope).
                    Mock Start-ZonalDnsLookup { [pscustomobject]@{} }
                    Mock Wait-ZonalDnsLookup { 'fd00::1'; '10.0.0.2'; '10.0.0.1' }
                    Mock Get-ZonalPersistentLogins { $script:persistent }
                    Mock Get-ZonalComputeMetadata {
                        [pscustomobject]@{ zone = '2'; subscriptionId = 'abcdef01-2345-6789-abcd-0123456789ab'; location = 'eastus' }
                    }
                    Mock Invoke-ZonalProviderRequest {
                        if ($script:nativeCalls.Count) { throw 'mutation before discovery completed' }
                        switch ($Command) {
                            'Get-AzElasticSan' { [pscustomobject]@{ Location = 'eastus' } }
                            'Invoke-AzRestMethod' { [pscustomobject]@{
                                StatusCode = 200; Content = '{"value":[{"name":"eastus","availabilityZoneMappings":[{"logicalZone":"2","physicalZone":"eastus-az3"}]}]}'
                            } }
                            'Get-AzElasticSanVolumeGroup' { [pscustomobject]@{} }
                            'Get-AzElasticSanVolume' { [pscustomobject]@{
                                StorageTargetIqn = "iqn.test:$($Parameters.Name)"; StorageTargetPortalHostname = 'portal.example'; StorageTargetPortalPort = 3260
                            } }
                            default { throw "Unexpected command $Command" }
                        }
                    }
                    [pscustomobject]@{ Subscription = [pscustomobject]@{ Id = 'abcdef01-2345-6789-abcd-0123456789ab' } }
                }
                $parameters = @{ ResourceGroupName = 'rg'; ElasticSanName = 'san'; VolumeGroupName = 'vg'; VolumeName = @('one', 'two'); EnableZonalAffinity = $true }
                if ($null -ne $countCase.Count) { $parameters.NumSession = $countCase.Count }
                . $connectPath @parameters
                $script:nativeCalls.Count | Should Be (2 * ($countCase.Expected + 1))
                $script:nativeCalls[1][1] | Should Be 'iqn.test:one:az-eastus-az3'
                $script:nativeCalls[-1][1] | Should Be 'iqn.test:two:az-eastus-az3'
                $script:nativeCalls[-1][3] | Should Be '10.0.0.2'
                Assert-MockCalled Start-ZonalDnsLookup -Scope It -Times 1 -Exactly
            }
        }
    }

Describe 'Unchanged legacy block and golden commands' {
    BeforeEach {
        $script:nativeCalls = New-Object 'System.Collections.Generic.List[object]'
        $script:nativeExit = 0
        $script:nativeOutput = 'The operation completed successfully.'
        $script:simulateLogin = $false
        Mock Get-Service { [pscustomobject]@{ Status = 'Running' } }
        Mock Get-WindowsFeature { [pscustomobject]@{ InstallState = 'Installed' } }
        Mock Get-AzElasticSanVolumeGroup { [pscustomobject]@{} }
        Mock Get-AzElasticSanVolume { [pscustomobject]@{ StorageTargetIqn = 'iqn.test:ONE'; StorageTargetPortalHostname = 'PORTAL.EXAMPLE'; StorageTargetPortalPort = 3260 } }
        Mock Get-IscsiSession {}
        Mock Get-AzContext { throw 'Legacy execution must not request mapping.' }
        Mock Write-Host {}
    }
    It 'matches the entire upstream execution block golden fingerprint' {
        $text = [IO.File]::ReadAllText($connectPath).Replace("`r`n", "`n")
        $marker = '##################### CHECK DEPENDENCY #################################'
        $legacy = $text.Substring($text.IndexOf($marker)).TrimEnd("`r", "`n")
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($legacy))).Replace('-', '').ToLowerInvariant() |
                Should Be 'b5798ac33e3ae96cc0c77bf163582d2cb1780b8dc2fa618763029168042806d9'
        } finally { $sha.Dispose() }
    }
    It 'retains the default count, FQDN casing and literal native argv' {
        . $connectPath 'rg' 'san' 'vg' @('one')
        $script:nativeCalls.Count | Should Be 33
        ($script:nativeCalls[0] -join '|') | Should Be 'AddTarget|iqn.test:ONE|*|PORTAL.EXAMPLE|3260|*|0|*|*|*|*|*|*|*|*|*|0'
        ($script:nativeCalls[1] -join '|') | Should Be 'PersistentLoginTarget|iqn.test:one|t|portal.example|3260|Root\ISCSIPRT\0000_0|-1|*|0x00000002|1|1|*|*|*|*|*|*|*|0'
        Assert-MockCalled Get-AzContext -Scope It -Times 0 -Exactly
    }
    It 'retains positional count and explicitly disabled opt-in' {
        . $connectPath 'rg' 'san' 'vg' @('one') 2 -EnableZonalAffinity:$false
        $script:nativeCalls.Count | Should Be 3
    }
    It 'retains case-insensitive existing-target skip behavior' {
        Mock Get-IscsiSession { [pscustomobject]@{ TargetNodeAddress = 'iqn.test:one' } }
        . $connectPath 'rg' 'san' 'vg' @('one') 1
        $script:nativeCalls.Count | Should Be 0
    }
}
