param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$venvRoot = Join-Path $projectRoot ".venv-build"
$python = Join-Path $venvRoot "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    python -m venv $venvRoot
}

if (-not $SkipInstall) {
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $projectRoot "requirements.txt")
    & $python -m pip install "pyinstaller==6.22.1"
}

function Move-ToRecycleBin([string]$Target) {
    if (-not (Test-Path -LiteralPath $Target)) {
        return
    }
    $resolved = (Resolve-Path -LiteralPath $Target).Path
    if (-not $resolved.StartsWith($projectRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理工作区外路径: $resolved"
    }
    Add-Type -AssemblyName Microsoft.VisualBasic
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
        $resolved,
        [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
        [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
    )
}

function Move-FileToRecycleBin([string]$Target) {
    if (-not (Test-Path -LiteralPath $Target)) {
        return
    }
    $resolved = (Resolve-Path -LiteralPath $Target).Path
    if (-not $resolved.StartsWith($projectRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理工作区外路径: $resolved"
    }
    Add-Type -AssemblyName Microsoft.VisualBasic
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
        $resolved,
        [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
        [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
    )
}

Move-ToRecycleBin (Join-Path $projectRoot "build\BiliCommentReviewer")
Move-ToRecycleBin (Join-Path $projectRoot "dist\BiliCommentReviewer")

Push-Location $projectRoot
try {
    & $python -m PyInstaller --noconfirm BiliCommentReviewer.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 构建失败，退出码 $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$exe = Join-Path $projectRoot "dist\BiliCommentReviewer\BiliCommentReviewer.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "构建完成但未找到 EXE: $exe"
}

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash
$releaseDir = Join-Path $projectRoot "release"
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
$archive = Join-Path $releaseDir "BiliCommentReviewer-0.1.0-windows-x64.zip"
$checksumFile = "$archive.sha256"
Move-FileToRecycleBin $archive
Move-FileToRecycleBin $checksumFile
Compress-Archive `
    -LiteralPath (Join-Path $projectRoot "dist\BiliCommentReviewer") `
    -DestinationPath $archive `
    -CompressionLevel Optimal
$archiveHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash
Set-Content `
    -LiteralPath $checksumFile `
    -Value "$archiveHash  $(Split-Path -Leaf $archive)" `
    -Encoding ascii

Write-Host "Build OK"
Write-Host "EXE: $exe"
Write-Host "SHA256: $hash"
Write-Host "ZIP: $archive"
Write-Host "ZIP SHA256: $archiveHash"
