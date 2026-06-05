# Invoke-Audit.ps1 - Deterministic Closed-Won booking audit (Windows PowerShell 5.1)
#
# Compares contract terms (extracted from the signed PDF by Claude) against the
# Salesforce opportunity + line items. Pure comparison logic, no I/O to SF/Slack.
#
# Inputs (two JSON files):
#   -SfDataJson    : { opp{...}, olis[...], contract_record{...}, slack{...} }
#   -ContractJson  : { shops[], services[], term_start, term_end,
#                      sms_marketing{...}, addendums[...], is_amendment_or_renewal, notes }
#
# Output: a JSON object on stdout: { color, findings[], owner{...}, slack_text }
# Slack emoji are written as :shortcodes: so the source stays ASCII-only.

param(
    [Parameter(Mandatory)] [string]$SfDataJson,
    [Parameter(Mandatory)] [string]$ContractJson
)
$ErrorActionPreference = 'Stop'

$sf       = Get-Content $SfDataJson   -Raw | ConvertFrom-Json
$contract = Get-Content $ContractJson -Raw | ConvertFrom-Json

$findings = New-Object System.Collections.ArrayList
function Add-Finding($status, $check, $message) {
    [void]$findings.Add([pscustomobject]@{ status = $status; check = $check; message = $message })
}

function Get-Canon([string]$name) {
    $n = ([string]$name).ToLower().Trim()
    $n = ($n -replace '\s+', ' ')
    switch -regex ($n) {
        'postscript ai|ps ai'                 { return 'postscript ai' }
        'sms marketing|^sms$'                 { return 'sms marketing' }
        'postscript plus|ps plus|ps\+'        { return 'postscript plus' }
        'fondue'                              { return 'fondue' }
        'it automations|^ita$'                { return 'it automations' }
        'it campaigns|^itc$'                  { return 'it campaigns' }
        'shopper'                             { return 'shopper' }
        'dsc|dedicated short code|short code' { return 'dsc' }
        default                               { return $n }
    }
}
function Test-MoneyEq($a, $b) {
    $na = $null; $nb = $null
    if ($null -ne $a -and "$a" -ne '') { $na = [double]$a }
    if ($null -ne $b -and "$b" -ne '') { $nb = [double]$b }
    if ($null -eq $na -and $null -eq $nb) { return $true }
    if ($null -eq $na -or  $null -eq $nb) { return $false }
    return ([math]::Abs($na - $nb) -lt 0.01)
}
function Get-Iso($d) { if ($null -eq $d) { return '' } else { return ([string]$d).Substring(0, [Math]::Min(10, ([string]$d).Length)) } }
function Find-Oli($canon) { $sf.olis | Where-Object { (Get-Canon $_.product) -eq $canon } | Select-Object -First 1 }

# ---- 1. Dates (PRD 6.1) : opp Start_Date__c / DocuSign_End_Date__c vs contract term ----
$oppStart = Get-Iso $sf.opp.Start_Date__c
$oppEnd   = Get-Iso $sf.opp.DocuSign_End_Date__c
$cStart   = Get-Iso $contract.term_start
$cEnd     = Get-Iso $contract.term_end
if ($cStart -eq $oppStart) { Add-Finding 'pass' 'Contract Start Date' "$oppStart" }
else { Add-Finding 'fail' 'Contract Start Date' "contract $cStart | opp $(if($oppStart){$oppStart}else{'(blank)'})" }
if ($cEnd -eq $oppEnd) { Add-Finding 'pass' 'Contract End Date' "$oppEnd" }
else { Add-Finding 'fail' 'Contract End Date' "contract $cEnd | opp $(if($oppEnd){$oppEnd}else{'(blank)'})" }

# ---- 2. Products with Postscript AI normalization (PRD 6.2 + 7.1) ----
$AI_SUBS = @('it automations', 'it campaigns', 'shopper')
$contractServices = @($contract.services | ForEach-Object { Get-Canon $_ } | Where-Object { $_ -ne 'dsc' })
$oppProducts      = @($sf.olis | ForEach-Object { Get-Canon $_.product } | Where-Object { $_ -ne 'dsc' })

$hasAiSubOnOpp    = @($oppProducts | Where-Object { $AI_SUBS -contains $_ }).Count -gt 0
$hasAiParentOnOpp = $oppProducts -contains 'postscript ai'
$hasAiOnContract  = $contractServices -contains 'postscript ai'
if ($hasAiSubOnOpp) {
    if (-not $hasAiParentOnOpp) { Add-Finding 'fail' 'Postscript AI parent on Opportunity' 'Opp has an AI sub-product (IT Automations / IT Campaigns / Shopper) but no Postscript AI line item.' }
    if (-not $hasAiOnContract)  { Add-Finding 'fail' 'Postscript AI on Contract' 'Opp has an AI sub-product but the contract services do not list Postscript AI.' }
    $oppProducts = @($oppProducts | Where-Object { $AI_SUBS -notcontains $_ })
}
$missingOnOpp = @($contractServices | Where-Object { $oppProducts -notcontains $_ } | Select-Object -Unique)
$extraOnOpp   = @($oppProducts | Where-Object { $contractServices -notcontains $_ } | Select-Object -Unique)
if ($missingOnOpp.Count -or $extraOnOpp.Count) {
    Add-Finding 'fail' 'Products match' "missing on opp: [$($missingOnOpp -join ', ')] | extra on opp: [$($extraOnOpp -join ', ')]"
} else {
    Add-Finding 'pass' 'Products match' "$($contractServices -join ', ')"
}

# ---- 3. SMS Marketing Minimum Commit (PRD 6.4 + 7.5) ----
# Opp UnitPrice for SMS is the MONTHLY-equivalent min commit; contract states a
# cadence (often quarterly). Normalize the contract amount to monthly before compare.
$smsOli = Find-Oli 'sms marketing'
if ($smsOli -and $contract.sms_marketing) {
    $cadence = $contract.sms_marketing.min_commit_cadence
    $mc = $contract.sms_marketing.min_commit
    $div = 1
    if ($cadence -eq 'quarterly') { $div = 3 }
    elseif ($cadence -eq 'annually') { $div = 12 }
    $monthly = $mc
    if ($null -ne $mc -and "$mc" -ne '' -and $div -ne 1) { $monthly = [double]$mc / $div }
    # Round both sides to whole dollars: quarterly/annual -> monthly division introduces cents.
    $monthlyR = if ($null -ne $monthly -and "$monthly" -ne '') { [math]::Round([double]$monthly) } else { $null }
    $oppMcR   = if ($null -ne $smsOli.UnitPrice -and "$($smsOli.UnitPrice)" -ne '') { [math]::Round([double]$smsOli.UnitPrice) } else { $null }
    if (Test-MoneyEq $oppMcR $monthlyR) {
        Add-Finding 'pass' 'SMS Min Commit' ("$" + $smsOli.UnitPrice + "/mo  (contract $" + $mc + "/" + $cadence + ")")
    } else {
        $cad = if ($cadence) { $cadence } else { '?' }
        Add-Finding 'fail' 'SMS Min Commit' ("contract $" + $mc + "/" + $cad + " (= $" + [math]::Round([double]$monthly, 2) + "/mo) | opp $" + $smsOli.UnitPrice + "/mo")
    }
    if (-not $cadence) {
        Add-Finding 'warn' 'SMS Min Commit Cadence' 'Cadence not stated in contract - verify manually.'
    }
}

# ---- 4. Platform / monthly fees per service, net of waivers (PRD 6.3) ----
function Test-PlatformFee($displayName, $obj) {
    if (-not $obj) { return }
    if ($null -eq $obj.platform_fee) { return }
    $oli = Find-Oli (Get-Canon $displayName)
    if (-not $oli) { return }
    # A waived fee should book as $0; a missing (null) monthly fee on the opp counts as $0.
    $actual = $oli.Platform_Fee__c
    if ($null -eq $actual -or "$actual" -eq '') { $actual = 0 }
    $expected = if ($obj.platform_fee_waived) { 0 } else { $obj.platform_fee }
    if (Test-MoneyEq $actual $expected) {
        $note = if ($obj.platform_fee_waived) { ' (waived -> $0)' } else { '' }
        Add-Finding 'pass' "$displayName Platform Fee" ("$" + $actual + $note)
    } else {
        $w = if ($obj.platform_fee_waived) { ' (waived -> expect $0)' } else { '' }
        Add-Finding 'fail' "$displayName Platform Fee" ("contract $" + $obj.platform_fee + $w + " | opp $" + $actual)
    }
}
Test-PlatformFee 'SMS Marketing' $contract.sms_marketing
foreach ($a in @($contract.addendums)) { Test-PlatformFee $a.name $a }

# ---- 5. DSC (PRD 6.5) ----
$dsc = if ($contract.sms_marketing) { $contract.sms_marketing.dsc } else { $null }
$dscOli = $sf.olis | Where-Object { (Get-Canon $_.product) -eq 'dsc' } | Select-Object -First 1
if ($dsc -and $dsc.included) {
    if (-not $dscOli) {
        Add-Finding 'fail' 'DSC' 'Contract includes DSC but no DSC line item on opp.'
    } else {
        $dscActual = $dscOli.Platform_Fee__c
        if ($null -eq $dscActual -or "$dscActual" -eq '') { $dscActual = 0 }
        $expected = if ($dsc.waived) { 0 } else { $dsc.fee }
        if (Test-MoneyEq $dscActual $expected) {
            $note = if ($dsc.waived) { ' (waived -> $0)' } else { '' }
            Add-Finding 'pass' 'DSC Fee' ("$" + $dscActual + $note)
        }
        else {
            $w = if ($dsc.waived) { ' (waived -> expect $0)' } else { '' }
            Add-Finding 'fail' 'DSC Fee' ("contract $" + $dsc.fee + $w + " | opp $" + $dscActual)
        }
    }
} elseif ($dscOli) {
    Add-Finding 'fail' 'DSC' 'Opp has a DSC line item but the contract does not include DSC.'
}

# ---- 6. Multi-shop callout (PRD 6.6) ----
if (@($contract.shops).Count -gt 1) {
    Add-Finding 'warn' 'Multi-shop deal' "$(@($contract.shops).Count) shops: $((@($contract.shops)) -join ', ')"
}

# ---- 7. Amendment / Renewal (PRD 7.4) ----
if ($contract.is_amendment_or_renewal) {
    Add-Finding 'warn' 'Amendment or Renewal' 'Most recent contract appears to be an amendment/renewal - v1 audit may need manual review.'
}

# ---- Roll-up ----
$hasFail = @($findings | Where-Object { $_.status -eq 'fail' }).Count -gt 0
$hasWarn = @($findings | Where-Object { $_.status -eq 'warn' }).Count -gt 0
if ($hasFail) { $color = 'red'; $dot = ':red_circle:' }
elseif ($hasWarn) { $color = 'yellow'; $dot = ':large_yellow_circle:' }
else { $color = 'green'; $dot = ':large_green_circle:' }

# ---- Slack text (mrkdwn, :shortcode: emoji) ----
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("$dot *Booking Audit - $($sf.opp.Name)*")
[void]$sb.AppendLine("Opportunity: $($sf.opp.Id)  |  Account: $($sf.opp.AccountId)  |  Contract: $($sf.contract_record.Id)")
foreach ($f in $findings) {
    $icon = switch ($f.status) { 'pass' { ':white_check_mark:' } 'warn' { ':warning:' } default { ':x:' } }
    $line = "$icon *$($f.check)*"
    if ($f.message) { $line += " - $($f.message)" }
    [void]$sb.AppendLine($line)
}
if ($color -eq 'red' -and $sf.opp.OwnerId) {
    [void]$sb.AppendLine("<@$($sf.opp.OwnerId)> please review the flagged items and re-book if needed.")
}

$result = [pscustomobject]@{
    color     = $color
    findings  = $findings
    owner     = [pscustomobject]@{ id = $sf.opp.OwnerId; name = $sf.opp.OwnerName; email = $sf.opp.OwnerEmail }
    slack_text = $sb.ToString()
}
$result | ConvertTo-Json -Depth 8
