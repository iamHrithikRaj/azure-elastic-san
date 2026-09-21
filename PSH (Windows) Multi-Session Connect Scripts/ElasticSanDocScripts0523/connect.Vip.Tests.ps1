$connectPath = Join-Path $PSScriptRoot 'connect.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($connectPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -like '*-Zonal*' }, $false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
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
        $id = '0000000000000001-{0:x16}' -f ($i + 1)
        $sessions += [pscustomobject]@{
            SessionIdentifier = $id; TargetNodeAddress = $Plan.Iqn
            IsConnected = $true; IsPersistent = $true
            IsHeaderDigest = $true; IsDataDigest = $true; NumberOfConnections = 1
        }
        $persistent += [pscustomobject]@{
            TargetName = $Plan.Iqn; Address = $Plan.Slots[$i % 32]; Port = $Plan.Port
            SessionIdentifier = $(if ($Correlate) { $id } else { $null })
            InitiatorInstance = 'Root\ISCSIPRT\0000_0'; IsInformationalSession = $false
            InitiatorPortNumber = [uint32]::MaxValue; Version = 0; SecurityFlags = 0; AuthType = 0
            InformationSpecified = 3; LoginFlags = 2; HeaderDigest = 1; DataDigest = 1
        }
        # Current endpoints deliberately differ from the original login VIPs.
        $connections[$id] = @([pscustomobject]@{
            ConnectionIdentifier = "connection-$i"; TargetAddress = '10.20.30.40'; TargetPortNumber = 3260
        })
    }
    [pscustomobject]@{ Sessions = $sessions; Persistent = $persistent; Connections = $connections; Targets = @() }
}

Describe 'Windows-owned VIP normalization and allocation' {
    It 'sorts unsigned packed bytes, IPv4 first, after canonicalization and deduplication' {
        (@(ConvertTo-ZonalVips @('10.0.0.10', '10.0.0.2', '10.0.0.1', '10.0.0.2')) -join ',') | Should Be '10.0.0.1,10.0.0.2,10.0.0.10'
        (@(ConvertTo-ZonalVips @('FD00::0010', 'fd00::2', 'FD00::0001', 'fd00::1')) -join ',') | Should Be 'fd00::1,fd00::2,fd00::10'
        (@(ConvertTo-ZonalVips @('fd00::1', '::ffff:10.0.0.10', '10.0.0.2', '10.0.0.10')) -join ',') | Should Be '10.0.0.2,10.0.0.10,fd00::1'
        (@(ConvertTo-ZonalVips @('192.168.1.1', '20.1.2.3', '172.16.0.1')) -join ',') | Should Be '20.1.2.3,172.16.0.1,192.168.1.1'
        (ConvertTo-ZonalAddress '::192.0.2.1') | Should Be '::c000:201'
        (ConvertTo-ZonalAddress '2001:0:0:1:0:0:1:1') | Should Be '2001::1:0:0:1:1'
    }
    It 'rejects invalid extras instead of filtering a valid triple out of an answer' {
        foreach ($invalid in @('invalid', '', $null, 1, ' 10.0.0.1', "10.0.0.1`n", '10.1',
            '010.0.0.1', '0x0a000001', '167772161', '0xa.0.0.1', '10.0.0.256', '[fd00::1]',
            '+10.0.0.1', '::ffff:10.1', '::ffff:0xa.0.0.1', '::ffff:010.0.0.1',
            '0.0.0.0', '::', '127.0.0.2', '::ffff:127.0.0.1', '::1', '224.0.0.1', 'ff02::1',
            '169.254.1.1', 'fe80::1', 'fd00::1%3', '255.255.255.255')) {
            { ConvertTo-ZonalVips @('10.0.0.1', '10.0.0.2', '10.0.0.3', $invalid) } | Should Throw
        }
    }
    It 'requires exactly three endpoints in a single answer' {
        foreach ($answer in @(@(), @('10.0.0.1'), @('10.0.0.1', '10.0.0.2'),
            @('10.0.0.1', '::ffff:10.0.0.1', '10.0.0.2'),
            @('10.0.0.1', '10.0.0.2', '10.0.0.3', '10.0.0.4'),
            @('10.0.0.1', '10.0.0.2', '10.0.0.3', 'fd00::1', 'fd00::2', 'fd00::3'))) {
            { ConvertTo-ZonalVips $answer } | Should Throw 'exactly three'
        }
    }
    It 'allocates exactly 32 deterministic round-robin slots with 11/11/10 counts' {
        $vips = @('fd00::1', '10.0.0.2', '10.0.0.1')
        $sorted = @(ConvertTo-ZonalVips $vips)
        $slots = @(Get-ZonalSessionSlots $vips)
        $slots.Count | Should Be 32
        for ($i = 0; $i -lt 32; $i++) { $slots[$i] | Should Be $sorted[$i % 3] }
        for ($i = 0; $i -lt 3; $i++) { @($slots | Where-Object { $_ -eq $sorted[$i] }).Count | Should Be @(11,11,10)[$i] }
    }
    It 'requires a decimal port in the supported range without trimming or truncating' {
        (ConvertTo-ZonalPort '1') | Should Be 1
        (ConvertTo-ZonalPort 65535) | Should Be 65535
        foreach ($port in @($null, '', '0', '-1', 65536, '3260x', "3260`n", ' 3260', '3260.0')) {
            { ConvertTo-ZonalPort $port } | Should Throw 'Invalid target port'
        }
    }
}

Describe 'Bounded host-local DNS' {
    BeforeEach {
        $script:dnsAttempts = 0
        Mock Start-Sleep {}
        Mock Start-ZonalDnsLookup { $script:dnsAttempts++; [pscustomobject]@{ Attempt = $script:dnsAttempts } }
    }
    It 'bounds each asynchronous wait to five seconds' {
        $lookup = New-Object PSObject
        $lookup | Add-Member ScriptMethod Wait { param($milliseconds) $script:waitMilliseconds = $milliseconds; $false }
        { Wait-ZonalDnsLookup $lookup } | Should Throw 'five-second'
        $script:waitMilliseconds | Should Be 5000
        $completion = New-Object 'System.Threading.Tasks.TaskCompletionSource[System.Net.IPAddress[]]'
        $completion.SetResult([System.Net.IPAddress[]]@([System.Net.IPAddress]::Parse('10.0.0.1')))
        (@(Wait-ZonalDnsLookup $completion.Task) -join ',') | Should Be '10.0.0.1'
        $start = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Start-ZonalDnsLookup' }, $false)[0]
        $start.Extent.Text | Should Match 'GetHostAddressesAsync'
    }
    It 'retries independent answers and caches only a successful triple within this run' {
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
    It 'never unions rotating partial answers or caches failure' {
        Mock Wait-ZonalDnsLookup { "10.0.0.$($Lookup.Attempt)" }
        $cache = @{}
        { Resolve-ZonalVips 'portal.example' $cache } | Should Throw 'three attempts'
        $script:dnsAttempts | Should Be 3
        $cache.Count | Should Be 0
        Assert-MockCalled Start-Sleep -Scope It -Times 1 -Exactly -ParameterFilter { $Seconds -eq 1 }
        Assert-MockCalled Start-Sleep -Scope It -Times 1 -Exactly -ParameterFilter { $Seconds -eq 2 }
    }
    It 'bounds retries on timeout or resolver failure without FQDN fallback' {
        Mock Wait-ZonalDnsLookup { throw 'DNS timeout' }
        { Resolve-ZonalVips 'portal.example' @{} } | Should Throw 'DNS timeout'
        Assert-MockCalled Start-ZonalDnsLookup -Scope It -Times 3 -Exactly
    }
    It 'rejects numeric, missing, single-label and malformed hostnames before lookup' {
        foreach ($name in @('10.0.0.1', 'fd00::1', '', 'portal', 'bad host.example')) {
            { Resolve-ZonalVips $name @{} } | Should Throw 'FQDN'
        }
        $script:dnsAttempts | Should Be 0
    }
}

Describe 'Original persistent portal and live-session proof' {
    BeforeEach { $plan = New-TestPlan; $inventory = New-TestInventory $plan }
    It 'skips only the complete correlated layout even after redirects' {
        (Test-ZonalLayout $plan $inventory) | Should Be $true
    }
    It 'does not infer missing correlation from apparently correct current endpoints' {
        $inventory = New-TestInventory $plan 32 $false
        for ($i = 0; $i -lt 32; $i++) { $inventory.Connections[$inventory.Sessions[$i].SessionIdentifier][0].TargetAddress = $plan.Slots[$i] }
        { Test-ZonalLayout $plan $inventory } | Should Throw 'correlation'
    }
    It 'accepts current-run observations only when every live identity and origin is accounted for' {
        $inventory = New-TestInventory $plan 32 $false
        $observed = @{}
        for ($i = 0; $i -lt 32; $i++) { $observed[$inventory.Sessions[$i].SessionIdentifier] = $plan.Slots[$i] }
        (Test-ZonalLayout $plan $inventory 32 $observed) | Should Be $true
        $observed[$inventory.Sessions[0].SessionIdentifier] = $plan.Vips[1]
        { Test-ZonalLayout $plan $inventory 32 $observed } | Should Throw 'origins'
    }
    It 'refuses partial, extra, stale, undecorated and differently decorated state' {
        foreach ($count in @(1,31,33)) { { Test-ZonalLayout $plan (New-TestInventory $plan $count) } | Should Throw }
        foreach ($iqn in @($plan.RawIqn, "$($plan.RawIqn):az-eastus-az1")) {
            $inventory = New-TestInventory $plan
            $inventory.Sessions[0].TargetNodeAddress = $iqn
            { Test-ZonalLayout $plan $inventory } | Should Throw
        }
        $empty = New-TestInventory $plan 0
        (Test-ZonalLayout $plan $empty) | Should Be $false
        $empty.Targets = @([pscustomobject]@{NodeAddress = $plan.Iqn})
        { Test-ZonalLayout $plan $empty } | Should Throw
        $inventory = New-TestInventory $plan
        $inventory.Sessions = @()
        { Test-ZonalLayout $plan $inventory } | Should Throw
    }
    It 'refuses unhealthy sessions, missing connections and duplicate identifiers' {
        foreach ($field in @('IsConnected','IsPersistent','IsHeaderDigest','IsDataDigest')) {
            $inventory = New-TestInventory $plan
            $inventory.Sessions[0].$field = $false
            { Test-ZonalLayout $plan $inventory } | Should Throw
        }
        $inventory = New-TestInventory $plan
        $inventory.Connections[$inventory.Sessions[0].SessionIdentifier] = @()
        { Test-ZonalLayout $plan $inventory } | Should Throw 'readiness'
        $inventory = New-TestInventory $plan
        $inventory.Sessions[0].SessionIdentifier = $inventory.Sessions[1].SessionIdentifier
        { Test-ZonalLayout $plan $inventory } | Should Throw 'ambiguous'
    }
    It 'rejects foreign persistent options, ports, identities and allocations' {
        foreach ($change in @(
            @{ Field='Port'; Value=3261 }, @{ Field='InitiatorPortNumber'; Value=1 },
            @{ Field='AuthType'; Value=1 }, @{ Field='SecurityFlags'; Value=1 },
            @{ Field='InformationSpecified'; Value=1 }, @{ Field='HeaderDigest'; Value=0 },
            @{ Field='DataDigest'; Value=0 }, @{ Field='LoginFlags'; Value=0 },
            @{ Field='Version'; Value=1 }, @{ Field='IsInformationalSession'; Value=$true },
            @{ Field='InitiatorInstance'; Value='another-initiator' },
            @{ Field='TargetName'; Value=$plan.RawIqn },
            @{ Field='Address'; Value='10.0.0.99' }, @{ Field='Address'; Value=$plan.Vips[1] }
        )) {
            $inventory = New-TestInventory $plan
            $inventory.Persistent[0].($change.Field) = $change.Value
            { Test-ZonalLayout $plan $inventory } | Should Throw
        }
    }
    It 'refuses duplicate, stale and malformed optional SessionId correlations' {
        foreach ($id in @('1-2', '1-ff', "1-1`n", 'not-an-id', '0-0')) {
            $inventory = New-TestInventory $plan
            $inventory.Persistent[0].SessionIdentifier = $id
            { Test-ZonalLayout $plan $inventory } | Should Throw
        }
    }
    It 'does not hide incompatible visible records behind pending counts' {
        $inventory = New-TestInventory $plan 1
        $inventory.Sessions = @()
        $inventory.Persistent[0].Port = 3261
        { Test-ZonalLayout $plan $inventory 1 @{} -AllowPending } | Should Throw 'incompatible'
    }
    It 'checks partial target identities and portal over-allocation before allowing pending state' {
        $inventory = New-TestInventory $plan 1
        $inventory.Targets = @([pscustomobject]@{ NodeAddress = $plan.RawIqn })
        { Test-ZonalLayout $plan $inventory 2 @{} -AllowPending } | Should Throw 'different zonal'
        $inventory = New-TestInventory $plan 2
        $inventory.Persistent[1].Address = $plan.Vips[0]
        { Test-ZonalLayout $plan $inventory 3 @{} -AllowPending } | Should Throw 'allocation'
    }
    It 'refuses lost current-run sessions and contradictory native correlation' {
        $inventory = New-TestInventory $plan 1
        $observed = @{ '0000000000000001-0000000000000001' = $plan.Vips[1] }
        { Test-ZonalLayout $plan $inventory 1 $observed -AllowPending } | Should Throw 'conflicts'
        $inventory = New-TestInventory $plan 0
        { Test-ZonalLayout $plan $inventory 2 $observed -AllowPending } | Should Throw 'no longer ready'
    }
}

Describe 'Read-only native ABI without calling the native DLL' {
    BeforeEach {
        $buffer = [IntPtr]::Zero
        Initialize-ZonalNativeInventory
        $nativePlan = New-TestPlan
        $nativeLogin = New-Object 'ElasticSan.ZonalPersistentInventory+Login'
        $nativeLogin.TargetName = $nativePlan.Iqn
        $nativeLogin.InitiatorInstance = 'Root\ISCSIPRT\0000_0'
        $nativeLogin.InitiatorPortNumber = [uint32]::MaxValue
        $portal = New-Object 'ElasticSan.ZonalPersistentInventory+Portal'
        $portal.Address = $nativePlan.Vips[0]; $portal.Socket = 3260
        $nativeLogin.TargetPortal = $portal
        $options = New-Object 'ElasticSan.ZonalPersistentInventory+Options'
        $options.InformationSpecified = 3; $options.LoginFlags = 2
        $options.HeaderDigest = 1; $options.DataDigest = 1
        $nativeLogin.LoginOptions = $options
        $nativeMapping = New-Object 'ElasticSan.ZonalPersistentInventory+Mapping'
        $nativeMapping.TargetName = $nativePlan.Iqn
        $id = New-Object 'ElasticSan.ZonalPersistentInventory+SessionId'
        $id.AdapterUnique = 1; $id.AdapterSpecific = 1
        $nativeMapping.SessionId = $id
        $stride = [Runtime.InteropServices.Marshal]::SizeOf($nativeLogin)
        $size = $stride + [Runtime.InteropServices.Marshal]::SizeOf($nativeMapping)
        $buffer = [Runtime.InteropServices.Marshal]::AllocHGlobal($size)
        $nativeLogin.Mappings = [IntPtr]::Add($buffer, $stride)
        [Runtime.InteropServices.Marshal]::StructureToPtr($nativeMapping, $nativeLogin.Mappings, $false)
        [Runtime.InteropServices.Marshal]::StructureToPtr($nativeLogin, $buffer, $false)
    }
    AfterEach { if ($null -ne $buffer -and $buffer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::FreeHGlobal($buffer) } }
    It 'matches the Windows Unicode ABI field offsets' {
        [Runtime.InteropServices.Marshal]::SizeOf($portal) | Should Be 1026
        [Runtime.InteropServices.Marshal]::OffsetOf($nativeMapping.GetType(), 'SessionId').ToInt32() | Should Be 1480
        [Runtime.InteropServices.Marshal]::OffsetOf($nativeLogin.GetType(), 'TargetPortal').ToInt32() | Should Be 968
        [Runtime.InteropServices.Marshal]::OffsetOf($nativeLogin.GetType(), 'SecurityFlags').ToInt32() | Should Be 2000
        [Runtime.InteropServices.Marshal]::OffsetOf($nativeLogin.GetType(), 'Mappings').ToInt32() | Should Be 2008
    }
    It 'decodes original portals and optional mappings, not redirected connections' {
        $records = @([ElasticSan.ZonalPersistentInventory]::Decode($buffer, $size, 1))
        $records.Count | Should Be 1
        $records[0].Address | Should Be '10.0.0.1'
        $records[0].Port | Should Be 3260
        $records[0].SessionIdentifier | Should Be '0000000000000001-0000000000000001'
        $inventory = New-TestInventory $nativePlan
        $inventory.Persistent[0] = $records[0]
        (Test-ZonalLayout $nativePlan $inventory) | Should Be $true
    }
    It 'never manufactures correlation from absent or zero native SessionId' {
        $nativeMapping.SessionId = New-Object 'ElasticSan.ZonalPersistentInventory+SessionId'
        [Runtime.InteropServices.Marshal]::StructureToPtr($nativeMapping, $nativeLogin.Mappings, $false)
        [ElasticSan.ZonalPersistentInventory]::Decode($buffer, $size, 1)[0].SessionIdentifier | Should BeNullOrEmpty
        $nativeLogin.Mappings = [IntPtr]::Zero
        [Runtime.InteropServices.Marshal]::StructureToPtr($nativeLogin, $buffer, $false)
        $record = [ElasticSan.ZonalPersistentInventory]::Decode($buffer, $size, 1)[0]
        $record.SessionIdentifier | Should BeNullOrEmpty
        $inventory = New-TestInventory $nativePlan
        $inventory.Persistent[0] = $record
        { Test-ZonalLayout $nativePlan $inventory } | Should Throw 'correlation'
    }
    It 'rejects conflicting target identity and out-of-buffer mappings before dereferencing' {
        $nativeMapping.TargetName = 'iqn.test:foreign'
        [Runtime.InteropServices.Marshal]::StructureToPtr($nativeMapping, $nativeLogin.Mappings, $false)
        { [ElasticSan.ZonalPersistentInventory]::Decode($buffer, $size, 1) } | Should Throw 'Conflicting'
        { [ElasticSan.ZonalPersistentInventory]::Decode($buffer, ($stride - 1), 1) } | Should Throw 'Truncated'
        foreach ($offset in @(-1, $size, 0)) {
            $nativeLogin.Mappings = [IntPtr]::Add($buffer, $offset)
            [Runtime.InteropServices.Marshal]::StructureToPtr($nativeLogin, $buffer, $false)
            { [ElasticSan.ZonalPersistentInventory]::Decode($buffer, $size, 1) } | Should Throw 'Invalid persistent session mapping'
        }
    }
}
