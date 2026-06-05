# Parse-Report.ps1 - extract distinct Opportunities from a Salesforce HTML-table .xls export
param([Parameter(Mandatory)][string]$Path)
$ErrorActionPreference = 'Stop'

function Strip([string]$s) {
    $s = [regex]::Replace($s, '<[^>]+>', '')
    $s = [System.Net.WebUtility]::HtmlDecode($s)
    return $s.Trim()
}

$html = Get-Content $Path -Raw
$rows = [regex]::Matches($html, '(?s)<tr[^>]*>(.*?)</tr>')
if ($rows.Count -lt 2) { throw "No <tr> rows found - is this the HTML-table export?" }

$headerCells = [regex]::Matches($rows[0].Groups[1].Value, '(?s)<t[hd][^>]*>(.*?)</t[hd]>')
$headers = @($headerCells | ForEach-Object { Strip $_.Groups[1].Value })

$records = New-Object System.Collections.ArrayList
for ($i = 1; $i -lt $rows.Count; $i++) {
    $cells = [regex]::Matches($rows[$i].Groups[1].Value, '(?s)<t[hd][^>]*>(.*?)</t[hd]>')
    if ($cells.Count -eq 0) { continue }
    $o = [ordered]@{}
    for ($c = 0; $c -lt $headers.Count; $c++) {
        $o[$headers[$c]] = if ($c -lt $cells.Count) { Strip $cells[$c].Groups[1].Value } else { '' }
    }
    [void]$records.Add([pscustomobject]$o)
}

# keep only real product rows (valid Opportunity Id), drop totals/footers
$records = @($records | Where-Object { $_.'Opportunity 18XID' -match '^006' })
"product_rows=$($records.Count)"

$groups = $records | Group-Object 'Opportunity 18XID'
"distinct_opportunities=$($groups.Count)"

$opps = foreach ($g in $groups) {
    $f = $g.Group[0]
    $sum = 0.0
    foreach ($r in $g.Group) { $v = $r.'Final Quota Relief Value'; if ($v -match '^-?\d+(\.\d+)?$') { $sum += [double]$v } }
    [pscustomobject]@{
        OpportunityName  = $f.'Opportunity Name'
        OppId            = $f.'Opportunity 18XID'
        Account          = $f.'Account Name'
        AccountId        = $f.'Account 18XID'
        Type             = $f.'Type'
        Owner            = $f.'Opportunity Owner'
        CloseDate        = $f.'Product Close Date'
        Products         = (($g.Group | ForEach-Object { $_.'Product Name' } | Select-Object -Unique) -join ', ')
        TotalQuotaRelief = ('{0:N2}' -f $sum)
    }
}
$opps = $opps | Sort-Object OpportunityName

$opps | Format-Table OpportunityName, Type, Owner, Products, TotalQuotaRelief -AutoSize -Wrap | Out-String -Width 4096
$outJson = Join-Path (Split-Path $Path) 'report-opps.json'
$opps | ConvertTo-Json -Depth 4 | Set-Content $outJson -Encoding utf8
"saved_json=$outJson"
