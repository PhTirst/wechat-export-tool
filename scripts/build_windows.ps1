param(
    [string]$PythonPath = "python",
    [string]$RuntimeRoot = "",
    [string]$CaptureExe = "",
    [string]$DistPath = "",
    [string]$WorkPath = "",
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Windows x64 is required to build this release.' }
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $RuntimeRoot) { $RuntimeRoot = Join-Path $projectRoot '.build/runtimes' }
if (-not $CaptureExe) { $CaptureExe = Join-Path $projectRoot 'bin/wxmori-media.exe' }
if (-not $DistPath) { $DistPath = Join-Path $projectRoot 'dist/windows' }
if (-not $WorkPath) { $WorkPath = Join-Path $projectRoot '.build/pyinstaller' }
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$CaptureExe = [IO.Path]::GetFullPath($CaptureExe)
$DistPath = [IO.Path]::GetFullPath($DistPath)
$WorkPath = [IO.Path]::GetFullPath($WorkPath)
$bundlePath = Join-Path $DistPath 'WeChatExportTool'
if (Test-Path -LiteralPath $bundlePath) { throw 'Bundle output already exists. Use a fresh -DistPath; no existing folder will be deleted.' }
if (-not (Test-Path -LiteralPath $CaptureExe -PathType Leaf)) { throw 'Build the capture backend first with scripts/build_capture.ps1, or supply -CaptureExe.' }
if (-not (Test-Path -LiteralPath (Join-Path $RuntimeRoot 'runtime-manifest.json') -PathType Leaf)) {
    throw 'Prepare runtimes first: python scripts/prepare_windows_runtime.py --output <RuntimeRoot>.'
}
$support = Join-Path $projectRoot 'packaging/build_support.py'
& $PythonPath -c "import platform,sys; assert sys.platform == 'win32' and platform.machine().lower() in ('amd64','x86_64'); import importlib.metadata as m; assert m.version('pyinstaller') == '6.22.3', 'Install requirements-build.txt'"
if ($LASTEXITCODE -ne 0) { throw 'Use an x64 Python environment with requirements-build.txt installed.' }
& $PythonPath $support validate-runtime --runtime-root $RuntimeRoot
if ($LASTEXITCODE -ne 0) { throw 'Runtime manifest validation failed.' }
if ($ValidateOnly) { Write-Output 'Windows build inputs validated.'; return }
New-Item -ItemType Directory -Path $DistPath, $WorkPath -Force | Out-Null
$taskPreviousMetadata = $env:WECHAT_BUILD_METADATA_ROOT
try {
    $env:WECHAT_BUILD_METADATA_ROOT = Join-Path $WorkPath 'metadata'
    & $PythonPath -m PyInstaller --noconfirm --distpath $DistPath --workpath $WorkPath (Join-Path $projectRoot 'packaging/WeChatExportTool.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    & $PythonPath $support stage --bundle $bundlePath --runtime-root $RuntimeRoot --capture $CaptureExe
    if ($LASTEXITCODE -ne 0) { throw 'Runtime/license staging failed.' }
    $emptyData = Join-Path $WorkPath 'synthetic-empty-account-root'
    New-Item -ItemType Directory -Path $emptyData -Force | Out-Null
    $missingConfig = Join-Path $WorkPath 'synthetic-missing-config.json'
    if (Test-Path -LiteralPath $missingConfig) { throw 'Synthetic smoke config unexpectedly exists.' }
    & (Join-Path $bundlePath 'wechat-export.exe') --version
    if ($LASTEXITCODE -ne 0) { throw 'Packaged CLI version smoke failed.' }
    $accountOutput = & (Join-Path $bundlePath 'wechat-export.exe') --config $missingConfig --data-root $emptyData --json accounts
    if ($LASTEXITCODE -ne 0 -or ($accountOutput -join "`n").Trim() -ne '[]') { throw 'Packaged empty-account smoke failed.' }
    & $PythonPath $support finalize --bundle $bundlePath --destination $DistPath
    if ($LASTEXITCODE -ne 0) { throw 'Release inventory/archive validation failed.' }
} finally {
    [Environment]::SetEnvironmentVariable('WECHAT_BUILD_METADATA_ROOT', $taskPreviousMetadata, 'Process')
}
Write-Output "Windows portable bundle: $bundlePath"
