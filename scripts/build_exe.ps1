$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $Root
try {
    $PortableDir = Join-Path $Root "dist\tgexporter-portable"
    $PortableZip = Join-Path $Root "dist\tgexporter-portable.zip"
    $OldDistExe = Join-Path $Root "dist\tgexporter.exe"
    $OldRootExe = Join-Path $Root "tgexporter.exe"

    Remove-Item -LiteralPath $PortableDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PortableZip -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $OldDistExe -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $OldRootExe -Force -ErrorAction SilentlyContinue

    python -m PyInstaller --clean --noconfirm tgexporter.spec
    $Exe = Join-Path $PortableDir "tgexporter.exe"
    if (!(Test-Path $Exe)) {
        throw "Expected exe was not generated: $Exe"
    }
    Compress-Archive -Path (Join-Path $PortableDir "*") -DestinationPath $PortableZip -Force
    Write-Host "Built portable folder: $PortableDir"
    Write-Host "Built portable zip:    $PortableZip"
}
finally {
    Pop-Location
}
