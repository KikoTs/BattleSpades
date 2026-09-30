param([string]$OutputDirectory = "$PSScriptRoot\..\..\out", [switch]$Rebuild)
$ErrorActionPreference = 'Stop'
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$runtimeDirectory = Join-Path $OutputDirectory 'retail-mousefix'
if ($Rebuild -or -not (Test-Path -LiteralPath (Join-Path $runtimeDirectory 'winmm.dll'))) {
    & (Join-Path $PSScriptRoot 'build.ps1') -OutputDirectory $runtimeDirectory
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$runtimeFiles = @('winmm.dll', 'aosfix_runtime.py', 'aos_mousefix.py', 'aos_equipmentfix.py', 'aos_uifix.py', 'aos_movementfix.py')
foreach ($name in $runtimeFiles | Where-Object { $_ -like '*.py' }) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $runtimeDirectory $name) -Force
}
$sourceFiles = @('loader.cpp','generated_exports.h','generated_stubs.inc','winmm.def',
    'bootstrap.py','aosfix_runtime.py','aos_mousefix.py','aos_equipmentfix.py','aos_uifix.py','aos_movementfix.py',
    'build.ps1','package.ps1','test_fixes.py','retail_bytecode.py','smoke_mouse.py','smoke_winmm.py',
    'test_movement.py','smoke_movement.py','MOVEMENT.md',
    'README.md','PROVENANCE.md','VALIDATION.md','LICENSE-Revival.txt')
$archivePath = Join-Path $OutputDirectory 'AoS-Retail-Fixes.zip'
$candidatePath = Join-Path $OutputDirectory ('AoS-Retail-Fixes-' + [Guid]::NewGuid().ToString('N') + '.zip')
$archive = [IO.Compression.ZipFile]::Open($candidatePath, [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($name in $runtimeFiles) {
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, (Join-Path $runtimeDirectory $name), $name) | Out-Null
    }
    foreach ($name in $sourceFiles) {
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, (Join-Path $PSScriptRoot $name), ('source/' + $name)) | Out-Null
    }
    foreach ($name in @('README.md','PROVENANCE.md','LICENSE-Revival.txt','VALIDATION.md')) {
        $entryName = if ($name -eq 'README.md') { 'README.txt' } else { $name }
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, (Join-Path $PSScriptRoot $name), $entryName) | Out-Null
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $runtimeDirectory $entryName) -Force
    }
    $hashes = foreach ($name in $runtimeFiles) {
        $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $runtimeDirectory $name)).Hash.ToLowerInvariant()
        "$digest  $name"
    }
    $writer = [IO.StreamWriter]::new($archive.CreateEntry('SHA256SUMS.txt').Open())
    try { $writer.WriteLine(($hashes -join "`n")) } finally { $writer.Dispose() }
} finally {
    $archive.Dispose()
}
Move-Item -LiteralPath $candidatePath -Destination $archivePath -Force
Write-Output "Created $archivePath"
