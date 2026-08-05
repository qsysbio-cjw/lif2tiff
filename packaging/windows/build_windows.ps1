param([switch]$Clean)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir "..\..")
$Version = "2.0.0-beta.1"
$Venv = Join-Path $ScriptDir ".venv-build"
$Python = Join-Path $Venv "Scripts\python.exe"

Set-Location $ScriptDir
if ($Clean) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
        (Join-Path $ScriptDir "build"), (Join-Path $ScriptDir "dist"), `
        (Join-Path $ScriptDir "release")
}
if (-not (Test-Path $Python)) {
    python -m venv $Venv
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r (Join-Path $Root "packaging\requirements-build.txt")
& $Python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $ScriptDir "dist") `
    --workpath (Join-Path $ScriptDir "build\gui") `
    (Join-Path $ScriptDir "LIF2TIFF-GUI.spec")
& $Python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $ScriptDir "dist") `
    --workpath (Join-Path $ScriptDir "build\cli") `
    (Join-Path $ScriptDir "LIF2TIFF-CLI.spec")

$GuiDir = Join-Path $ScriptDir "dist\LIF2TIFF-GUI"
$GuiExe = Join-Path $GuiDir "LIF2TIFF-GUI.exe"
$CliExe = Join-Path $ScriptDir "dist\lif2tiff.exe"
if (-not (Test-Path $GuiExe) -or -not (Test-Path $CliExe)) {
    throw "Expected GUI or CLI executable was not created."
}

& $CliExe --version
& $CliExe plan (Join-Path $Root "tests\fixtures\public\ome_pr2729\output_collision\Sample.lif") | Out-Null
$env:QT_QPA_PLATFORM = "offscreen"
$Smoke = Start-Process -FilePath $GuiExe -ArgumentList "--smoke-test" -Wait -PassThru
if ($Smoke.ExitCode -ne 0) {
    throw "Packaged GUI smoke test failed with exit code $($Smoke.ExitCode)"
}

$PackageName = "LIF2TIFF-Windows-x64-v$Version"
$Package = Join-Path $ScriptDir "release\$PackageName"
$PackageGui = Join-Path $Package "GUI"
New-Item -ItemType Directory -Force $PackageGui | Out-Null
Copy-Item -Recurse -Force (Join-Path $GuiDir "*") $PackageGui
Copy-Item -Force $CliExe (Join-Path $Package "lif2tiff.exe")
Copy-Item -Force (Join-Path $Root "packaging\RELEASE_README.md") (Join-Path $Package "README.md")
Copy-Item -Force (Join-Path $Root "RELEASE_NOTES.md"), `
    (Join-Path $Root "THIRD_PARTY_NOTICES.md"), (Join-Path $Root "LICENSE") $Package
Copy-Item -Recurse -Force (Join-Path $Root "third_party_licenses") $Package

$Archive = Join-Path $ScriptDir "release\$PackageName.zip"
Compress-Archive -Path $Package -DestinationPath $Archive -Force
$Hash = (Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLower()
"$Hash  $PackageName.zip" | Set-Content -Encoding ascii "$Archive.sha256"
Write-Host "Release: $Archive"
