# sf-lib.ps1 - Salesforce auth + file download helpers (Windows PowerShell 5.1)
# Reused by the golden test and the hourly routine.
#
# Functions:
#   Get-SfAuth            -> exchanges client_credentials for an access token
#   Save-SfContentVersion -> downloads a ContentVersion's VersionData blob to a file
#
# Secrets live in sf-auth.json (never pasted in chat / never committed).

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-SfAuth {
    param([string]$AuthFile = (Join-Path $PSScriptRoot 'sf-auth.json'))

    if (-not (Test-Path $AuthFile)) { throw "Auth file not found: $AuthFile" }
    $cfg = Get-Content $AuthFile -Raw | ConvertFrom-Json

    if (-not $cfg.client_id -or $cfg.client_id -like 'PASTE_*') {
        throw "client_id not set in $AuthFile - paste the Connected App Consumer Key."
    }
    if (-not $cfg.client_secret -or $cfg.client_secret -like 'PASTE_*') {
        throw "client_secret not set in $AuthFile - paste the Connected App Consumer Secret."
    }

    $apiVersion = if ($cfg.api_version) { $cfg.api_version } else { '64.0' }
    $base = $cfg.instance_url.TrimEnd('/')
    $tokenUrl = "$base/services/oauth2/token"
    $body = @{
        grant_type    = 'client_credentials'
        client_id     = $cfg.client_id
        client_secret = $cfg.client_secret
    }

    try {
        $resp = Invoke-RestMethod -Method Post -Uri $tokenUrl -Body $body `
            -ContentType 'application/x-www-form-urlencoded'
    }
    catch {
        $detail = ''
        if ($_.Exception.Response) {
            $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
            $detail = $reader.ReadToEnd()
        }
        throw "Token request to $tokenUrl failed: $($_.Exception.Message) $detail"
    }

    return [pscustomobject]@{
        access_token = $resp.access_token
        instance_url = $resp.instance_url.TrimEnd('/')
        api_version  = $apiVersion
    }
}

function Save-SfContentVersion {
    param(
        [Parameter(Mandatory)] [string]$VersionId,
        [Parameter(Mandatory)] [string]$OutFile,
        [object]$Auth
    )
    if (-not $Auth) { $Auth = Get-SfAuth }

    $url = "{0}/services/data/v{1}/sobjects/ContentVersion/{2}/VersionData" -f `
        $Auth.instance_url, $Auth.api_version, $VersionId

    # curl.exe handles binary cleanly; -f fails on HTTP errors, -s silent, -L follow redirects
    & curl.exe -sfL -H "Authorization: Bearer $($Auth.access_token)" -o $OutFile $url
    if ($LASTEXITCODE -ne 0) { throw "curl failed (exit $LASTEXITCODE) downloading $VersionId" }
    if (-not (Test-Path $OutFile)) { throw "Download produced no file: $OutFile" }

    # Guard: confirm we got a PDF, not a JSON/HTML error page written to disk
    $head = [System.IO.File]::ReadAllBytes($OutFile)[0..4] -join ','
    $pdfMagic = (([byte][char]'%'),([byte][char]'P'),([byte][char]'D'),([byte][char]'F'),(45)) -join ','
    if ($head -ne $pdfMagic) {
        $preview = Get-Content $OutFile -Raw -ErrorAction SilentlyContinue
        throw "Downloaded file is not a PDF (no %PDF- header). First bytes: $head. Body: $($preview.Substring(0,[Math]::Min(300,$preview.Length)))"
    }

    return (Get-Item $OutFile)
}
