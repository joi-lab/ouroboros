$ErrorActionPreference = "Stop"

$Version = if ($env:RIPGREP_VERSION) { $env:RIPGREP_VERSION } else { "14.1.1" }
$Root = "ripgrep-standalone"
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("rg-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
try {
    $Asset = "ripgrep-$Version-x86_64-pc-windows-msvc.zip"
    $Url = "https://github.com/BurntSushi/ripgrep/releases/download/$Version/$Asset"
    Write-Host "Downloading $Url"
    $Zip = Join-Path $Tmp $Asset
    Invoke-WebRequest -Uri $Url -OutFile $Zip
    $ShaFile = "$Zip.sha256"
    Invoke-WebRequest -Uri "$Url.sha256" -OutFile $ShaFile
    # Upstream sidecar formats differ per asset: some are "<hex>  <name>", the
    # windows zip ships prose ("SHA256 hash of <name>:\n<hex>"), where the old
    # first-token split read the literal word "sha256" as the expected digest.
    # Take the first 64-char hex token wherever it sits; fail loudly if absent.
    $HexTokens = @([regex]::Matches((Get-Content $ShaFile -Raw), '\b[0-9a-fA-F]{64}\b') | ForEach-Object { $_.Value })
    if ($HexTokens.Count -eq 0) {
        throw "no sha256 digest found in checksum sidecar for $Asset"
    }
    $Expected = $HexTokens[0].ToLowerInvariant()
    $Actual = (Get-FileHash -Algorithm SHA256 $Zip).Hash.ToLowerInvariant()
    if ($Actual -ne $Expected) {
        throw "SHA256 mismatch for $Asset`: expected $Expected, got $Actual"
    }
    Write-Host "Verified SHA256 for $Asset`: $Actual"
    Expand-Archive -Path $Zip -DestinationPath $Tmp
    if (Test-Path $Root) { Remove-Item -Recurse -Force $Root }
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    Copy-Item (Join-Path $Tmp "ripgrep-$Version-x86_64-pc-windows-msvc\rg.exe") (Join-Path $Root "rg.exe")
    & (Join-Path $Root "rg.exe") --version
}
finally {
    if (Test-Path $Tmp) { Remove-Item -Recurse -Force $Tmp }
}
