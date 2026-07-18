param(
    [switch]$NoCopyToRoot
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $Root
try {
    python -m PyInstaller --clean --noconfirm tgexporter.spec
    $Exe = Join-Path $Root "dist\tgexporter.exe"
    if (!(Test-Path $Exe)) {
        throw "Expected exe was not generated: $Exe"
    }
    if (!$NoCopyToRoot) {
        Copy-Item -LiteralPath $Exe -Destination (Join-Path $Root "tgexporter.exe") -Force
    }
    Write-Host "Built: $Exe"
    if (!$NoCopyToRoot) {
        Write-Host "Copied: $(Join-Path $Root "tgexporter.exe")"
    }
}
finally {
    Pop-Location
}
