# bundle_windows.ps1 — Windows equivalent of bundle_macos.sh.
#
# Walks the import table of step2glb.exe with `dumpbin /dependents`,
# resolves each DLL against the vcpkg-installed bin directory, copies
# the entire transitive closure into bundle/bin/ alongside the EXE, and
# skips Windows system DLLs that the user already has.
#
# Output: bundle/bin/step2glb.exe + every DLL it loads.
#
# On Windows, DLLs in the same directory as the EXE are found
# automatically by the loader — no rpath / install_name tricks needed.
# This is invoked by the GitHub Actions workflow after the cmake build,
# but you can also run it locally if you have a Visual Studio + vcpkg
# environment set up:
#
#     pwsh step2glb/bundle_windows.ps1
#
# Inputs assumed by the script:
#   build/Release/step2glb.exe — produced by `cmake --build build --config Release`
#   $env:VCPKG_INSTALLATION_ROOT — set by run-vcpkg or vcpkg installer

param(
    [string]$BinarySource = "build/Release/step2glb.exe",
    [string]$BuildDir     = "build",
    [string]$OutDir       = "bundle",
    [string]$VcpkgBin     = $null
)

$ErrorActionPreference = "Stop"

# ── Resolve vcpkg's installed bin directory ──────────────────────────────────
# Two modes possible:
#   1. Manifest mode (our case): vcpkg.json in the repo, so CMake's
#      vcpkg toolchain installs OCCT into <BuildDir>/vcpkg_installed/
#      x64-windows/. Per-build, isolated, used by the CI workflow.
#   2. Classic mode: a global `vcpkg install opencascade` populated
#      $VCPKG_INSTALLATION_ROOT/installed/x64-windows/. Used when
#      running the bundle script outside CI.
if (-not $VcpkgBin) {
    $candidates = @(
        (Join-Path $BuildDir "vcpkg_installed\x64-windows\bin"),
        (Join-Path $PWD     "build\vcpkg_installed\x64-windows\bin"),
        (Join-Path (Split-Path -Parent $PWD) "build\vcpkg_installed\x64-windows\bin")
    )
    if ($env:VCPKG_INSTALLATION_ROOT) {
        $candidates += (Join-Path $env:VCPKG_INSTALLATION_ROOT "installed\x64-windows\bin")
    }
    foreach ($c in $candidates) {
        if (Test-Path $c) {
            $VcpkgBin = (Resolve-Path $c).Path
            break
        }
    }
}
if (-not $VcpkgBin -or -not (Test-Path $VcpkgBin)) {
    Write-Error ("vcpkg bin dir not found. Tried: " + ($candidates -join "; "))
}

# ── Reset output ─────────────────────────────────────────────────────────────
if (Test-Path $OutDir) { Remove-Item -Recurse -Force $OutDir }
New-Item -ItemType Directory -Force -Path "$OutDir/bin" | Out-Null

Copy-Item $BinarySource "$OutDir/bin/step2glb.exe"

# ── Find dumpbin (ships with Visual Studio Build Tools) ──────────────────────
$dumpbin = Get-Command dumpbin.exe -ErrorAction SilentlyContinue
if (-not $dumpbin) {
    # Try discovering it via vswhere
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $vsPath = & $vswhere -latest -property installationPath
        $dumpbinPath = Get-ChildItem "$vsPath\VC\Tools\MSVC" -Recurse -Filter dumpbin.exe `
            | Where-Object { $_.FullName -like "*\Hostx64\x64\*" } `
            | Select-Object -First 1
        if ($dumpbinPath) {
            $dumpbin = $dumpbinPath.FullName
        }
    }
}
if (-not $dumpbin) {
    Write-Error "dumpbin.exe not found. Install Visual Studio Build Tools or run from a Developer PowerShell prompt."
}

# ── System DLLs we never bundle (user's Windows already provides them) ───────
$SystemDLLs = @(
    "kernel32.dll", "user32.dll", "gdi32.dll", "advapi32.dll", "shell32.dll",
    "ole32.dll", "oleaut32.dll", "comdlg32.dll", "comctl32.dll", "ws2_32.dll",
    "winmm.dll", "imm32.dll", "msimg32.dll", "version.dll", "iphlpapi.dll",
    "crypt32.dll", "rpcrt4.dll", "secur32.dll", "userenv.dll", "wininet.dll",
    "ntdll.dll", "msvcrt.dll", "uxtheme.dll", "dwmapi.dll", "shlwapi.dll",
    "psapi.dll", "powrprof.dll", "bcrypt.dll", "ncrypt.dll", "dbghelp.dll",
    "setupapi.dll", "cfgmgr32.dll", "windows.storage.dll",
    # MSVC runtime — we ship these too because Blender users may not have
    # the matching VC++ Redistributable installed, but they live with the
    # other system DLLs by name
    "api-ms-win-*"
)

function Is-SystemDLL($name) {
    foreach ($pat in $SystemDLLs) {
        if ($name -like $pat) { return $true }
    }
    return $false
}

# ── Recursive dependency walk ────────────────────────────────────────────────
# Multi-source DLL resolution. vcpkg's installed bin holds OCCT and most
# third-party libs, but the MSVC C++ runtime (vcruntime140.dll, msvcp140.dll,
# vcruntime140_1.dll) lives in C:\Windows\System32 instead. Without these
# bundled, end users without VC++ Redistributable installed get a missing-DLL
# dialog when step2glb.exe tries to launch.
$winDir = if ($env:WINDIR) { $env:WINDIR } else { "C:\Windows" }
$DllSources = @($VcpkgBin, "$winDir\System32")

function Resolve-DLL($name) {
    foreach ($d in $DllSources) {
        $candidate = Join-Path $d $name
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

$Bundled = @{}

function Walk-Dependencies($exeOrDll) {
    $output = & $dumpbin /nologo /dependents $exeOrDll 2>$null
    foreach ($line in $output) {
        if ($line -match "^\s+(\S+\.dll)\s*$") {
            $dep = $matches[1].Trim()
            if (Is-SystemDLL $dep) { continue }
            if ($Bundled.ContainsKey($dep)) { continue }

            $src = Resolve-DLL $dep
            if (-not $src) {
                Write-Warning "  ! Could not resolve $dep in any DLL source"
                continue
            }

            $dest = Join-Path "$OutDir/bin" $dep
            Copy-Item $src $dest
            $Bundled[$dep] = $true
            Write-Host "  + $dep  (from $(Split-Path -Leaf (Split-Path $src)))"

            # Recurse into the DLL we just copied
            Walk-Dependencies $dest
        }
    }
}

# OCCT plugin DLLs are loaded via LoadLibrary at runtime, keyed on the
# document format string (e.g. NewDocument("BinXCAF", …) → TKBinXCAF.dll),
# so they're invisible to dumpbin's static dependency analysis. Without
# them present alongside the EXE, OCCT's CDF layer fails at runtime with
# "Cannot find driver" the moment we open a document. step2glb.cpp uses
# BinXCAF, so TKBin / TKBinL / TKBinXCAF are mandatory; we also ship the
# XML format drivers in case a future code path opts into them.
function Add-RuntimePlugin($name) {
    if ($Bundled.ContainsKey($name)) { return }
    $src = Resolve-DLL $name
    if (-not $src) {
        Write-Warning "  ! OCCT plugin $name not found — some file formats may fail at runtime"
        return
    }
    $dest = Join-Path "$OutDir/bin" $name
    Copy-Item $src $dest
    $Bundled[$name] = $true
    Write-Host "  + $name  (OCCT runtime plugin)"
    Walk-Dependencies $dest
}

Write-Host "── bundle_windows.ps1 ──────────────────────────────────────────────"
Write-Host "  source:    $BinarySource"
Write-Host "  vcpkg bin: $VcpkgBin"
Write-Host "  out:       $OutDir/bin"
Write-Host ""
Write-Host "Resolving dependencies of step2glb.exe..."
Walk-Dependencies "$OutDir/bin/step2glb.exe"

Write-Host ""
Write-Host "Adding OCCT runtime-loaded plugins..."
foreach ($plugin in @("TKBin.dll", "TKBinL.dll", "TKBinXCAF.dll",
                      "TKXml.dll", "TKXmlL.dll", "TKXmlXCAF.dll")) {
    Add-RuntimePlugin $plugin
}

$totalSize = (Get-ChildItem "$OutDir/bin" -File | Measure-Object -Property Length -Sum).Sum
$mb = [math]::Round($totalSize / 1MB, 1)
Write-Host ""
Write-Host "── Bundle ready ────────────────────────────────────────────────────"
Write-Host "  $($Bundled.Count) DLLs bundled, total $mb MB"
