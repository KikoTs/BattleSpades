param([string]$OutputDirectory = "$PSScriptRoot\..\..\out\retail-mousefix")
$ErrorActionPreference = 'Stop'
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'Visual Studio C++ x86 build tools are required to rebuild the loader.' }
$build = Join-Path $OutputDirectory 'build'
New-Item -ItemType Directory -Path $build -Force | Out-Null
$bootstrap = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'bootstrap.py'))
$header = 'static const char kBootstrap[] = R"AOSBOOT(' + $bootstrap + ')AOSBOOT";'
[IO.File]::WriteAllText((Join-Path $build 'generated_bootstrap.h'), $header, [Text.UTF8Encoding]::new($false))
$devcmd = Join-Path $vs 'Common7\Tools\VsDevCmd.bat'
$buildCmd = Join-Path $build 'compile.cmd'
$content = @"
@echo off
call "$devcmd" -no_logo -arch=x86 -host_arch=x64
if errorlevel 1 exit /b %errorlevel%
cd /d "$build"
cl /nologo /LD /MT /O2 /W4 /WX /EHsc /std:c++17 /I"$build" /I"$PSScriptRoot" "$PSScriptRoot\loader.cpp" /link /DEF:"$PSScriptRoot\winmm.def" /OUT:"$OutputDirectory\winmm.dll" /DYNAMICBASE /NXCOMPAT /MACHINE:X86
exit /b %errorlevel%
"@
[IO.File]::WriteAllText($buildCmd, $content, [Text.ASCIIEncoding]::new())
& $buildCmd
if ($LASTEXITCODE -ne 0) { throw 'Loader compilation failed.' }
foreach ($scriptName in @('aosfix_runtime.py', 'aos_mousefix.py', 'aos_equipmentfix.py', 'aos_uifix.py', 'aos_movementfix.py', 'aos_networkfix.py', 'aos_steam_bridge.py')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $scriptName) -Destination $OutputDirectory
}
Write-Output "Built $OutputDirectory\winmm.dll and Python runtime files"
