# Downloads python-build-standalone for Windows (x86_64)
# Run from repo root: powershell -ExecutionPolicy Bypass -File scripts/download_python_standalone.ps1

$ErrorActionPreference = "Stop"

$Release = "20260211"
$PyVersion = "3.10.19"
$Dest = "python-standalone"
$Platform = "x86_64-pc-windows-msvc"

# Pinned SHA256 (from the release's SHA256SUMS): a swapped/truncated archive
# fails here instead of becoming the packaged runtime. Update the pin when
# bumping $Release/$PyVersion, exactly as in download_python_standalone.sh.
$Sha256 = "b892f2c7eb0a04611688d6df7567a2745a204aac694d6d1c56c75b0717dab2d6"

$Filename = "cpython-${PyVersion}+${Release}-${Platform}-install_only_stripped.tar.gz"
$Url = "https://github.com/astral-sh/python-build-standalone/releases/download/${Release}/${Filename}"

Write-Host "=== Downloading Python ${PyVersion} for ${Platform} ==="
Write-Host "URL: ${Url}"

if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
if (Test-Path "_python_tmp") { Remove-Item -Recurse -Force "_python_tmp" }
New-Item -ItemType Directory -Path "_python_tmp" | Out-Null

$ArchivePath = "_python_tmp\python.tar.gz"
Write-Host "Downloading..."
Invoke-WebRequest -Uri $Url -OutFile $ArchivePath -UseBasicParsing

Write-Host "Verifying SHA256..."
$Actual = (Get-FileHash -Algorithm SHA256 $ArchivePath).Hash.ToLower()
if ($Actual -ne $Sha256.ToLower()) {
    throw "SHA256 mismatch for ${Filename}: expected ${Sha256}, got ${Actual} - refusing to install."
}
Write-Host "Checksum OK: ${Actual}"

Write-Host "Extracting..."
tar -xzf $ArchivePath -C "_python_tmp"

Move-Item "_python_tmp\python" $Dest
Remove-Item -Recurse -Force "_python_tmp"

echo ""
Write-Host "=== Installing agent dependencies ==="
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required for locked dependency installation"
}
uv pip install --python "${Dest}\python.exe" --quiet -r requirements-runtime.lock
if ($LASTEXITCODE -ne 0) {
    throw "Agent dependency installation failed with exit code $LASTEXITCODE"
}

echo ""
Write-Host "=== Installing optional: local model support ==="
try {
    uv pip install --python "${Dest}\python.exe" --quiet "llama-cpp-python[server]" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "llama-cpp-python installation failed with exit code $LASTEXITCODE"
    }
    Write-Host "llama-cpp-python installed successfully"
} catch {
    Write-Warning "llama-cpp-python install failed - local model support will not be available"
}

echo ""
Write-Host "=== Done ==="
Write-Host "Python: ${Dest}\python.exe"
& "${Dest}\python.exe" --version
