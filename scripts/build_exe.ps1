$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $Root
try {
    $DistRoot = Join-Path $Root "dist"
    $BuildDist = Join-Path $DistRoot "_pyinstaller-output"
    $BuildWork = Join-Path $Root "build\tgexporter-portable"
    $PreferredDir = Join-Path $DistRoot "tgexporter-portable"
    $PreferredZip = Join-Path $DistRoot "tgexporter-portable.zip"
    $PortableDir = $PreferredDir
    $PortableZip = $PreferredZip
    $OldDistExe = Join-Path $Root "dist\tgexporter.exe"
    $OldRootExe = Join-Path $Root "tgexporter.exe"

    Remove-Item -LiteralPath $BuildDist -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $BuildWork -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $OldDistExe -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $OldRootExe -Force -ErrorAction SilentlyContinue

    try {
        if (Test-Path -LiteralPath $PreferredDir) {
            Remove-Item -LiteralPath $PreferredDir -Recurse -Force -ErrorAction Stop
        }
        if (Test-Path -LiteralPath $PreferredZip) {
            Remove-Item -LiteralPath $PreferredZip -Force -ErrorAction Stop
        }
    }
    catch {
        $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $PortableDir = Join-Path $DistRoot "tgexporter-portable-$Stamp"
        $PortableZip = Join-Path $DistRoot "tgexporter-portable-$Stamp.zip"
        Write-Warning "Could not replace $PreferredDir because it is in use. Building versioned portable package instead: $PortableDir"
    }

    python -m PyInstaller --clean --noconfirm --distpath $BuildDist --workpath $BuildWork tgexporter.spec
    $BuiltDir = Join-Path $BuildDist "tgexporter-portable"
    if (!(Test-Path $BuiltDir)) {
        throw "Expected portable folder was not generated: $BuiltDir"
    }
    Move-Item -LiteralPath $BuiltDir -Destination $PortableDir
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
