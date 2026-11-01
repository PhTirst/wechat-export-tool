param([string]$OutputPath = "")
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'This backend targets Windows x64.' }
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputPath) { $OutputPath = Join-Path $projectRoot 'bin/wxmori-media.exe' }
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
foreach ($tool in @('git','go')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Required tool missing: $tool" }
}
$sourceRoot = Join-Path $projectRoot '.build/wxmori'
$revision = '055f6992c0a78164b720d47ef48830825fc0b92a'
if (Test-Path -LiteralPath $sourceRoot) {
    throw 'Build directory already exists. Use a fresh checkout, or review and remove .build/wxmori yourself.'
}
New-Item -ItemType Directory -Path (Split-Path -Parent $sourceRoot) -Force | Out-Null
& git clone --no-checkout 'https://github.com/axiomzero-qed/WxMori.git' $sourceRoot
if ($LASTEXITCODE -ne 0) { throw 'Clone failed.' }
& git -C $sourceRoot checkout --detach $revision
if ($LASTEXITCODE -ne 0) { throw 'Pinned revision unavailable.' }
& git -C $sourceRoot apply --check (Join-Path $projectRoot 'patches/wxmori-media.patch')
if ($LASTEXITCODE -ne 0) { throw 'Patch validation failed.' }
& git -C $sourceRoot apply (Join-Path $projectRoot 'patches/wxmori-media.patch')
if ($LASTEXITCODE -ne 0) { throw 'Patch application failed.' }
Push-Location $sourceRoot
$taskPreviousGoOS = $env:GOOS
$taskPreviousGoArch = $env:GOARCH
try {
    $env:GOOS = 'windows'
    $env:GOARCH = 'amd64'
    & go test ./...
    if ($LASTEXITCODE -ne 0) { throw 'Go tests failed.' }
    & go vet ./...
    if ($LASTEXITCODE -ne 0) { throw 'Go vet failed.' }
    New-Item -ItemType Directory -Path (Split-Path -Parent $OutputPath) -Force | Out-Null
    & go build -trimpath -o $OutputPath ./cmd/wxmori
    if ($LASTEXITCODE -ne 0) { throw 'Go build failed.' }
} finally {
    [Environment]::SetEnvironmentVariable('GOOS', $taskPreviousGoOS, 'Process')
    [Environment]::SetEnvironmentVariable('GOARCH', $taskPreviousGoArch, 'Process')
    Pop-Location
}
Write-Output "Capture backend built: $OutputPath"
