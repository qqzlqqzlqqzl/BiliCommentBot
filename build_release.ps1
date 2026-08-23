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

$buildId = [guid]::NewGuid().ToString("N")
$stagingRoot = Join-Path $projectRoot "build\release-$buildId"
$workPath = Join-Path $stagingRoot "work"
$distPath = Join-Path $stagingRoot "dist"
[void](New-Item -ItemType Directory -Path $workPath -Force)
[void](New-Item -ItemType Directory -Path $distPath -Force)

Push-Location $projectRoot
try {
    & $python -m PyInstaller `
        --noconfirm `
        --workpath $workPath `
        --distpath $distPath `
        BiliCommentReviewer.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 构建失败，退出码 $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$stagedProduct = Join-Path $distPath "BiliCommentReviewer"
$stagedExe = Join-Path $stagedProduct "BiliCommentReviewer.exe"
if (-not (Test-Path -LiteralPath $stagedExe)) {
    throw "构建完成但未找到 EXE: $stagedExe"
}

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $stagedExe).Hash
$releaseDir = Join-Path $projectRoot "release"
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
$archive = Join-Path $releaseDir "BiliCommentReviewer-0.2.0-windows-x64.zip"
$checksumFile = "$archive.sha256"
Move-FileToRecycleBin $archive
Move-FileToRecycleBin $checksumFile
Compress-Archive `
    -LiteralPath $stagedProduct `
    -DestinationPath $archive `
    -CompressionLevel Optimal
$archiveHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash
Set-Content `
    -LiteralPath $checksumFile `
    -Value "$archiveHash  $(Split-Path -Leaf $archive)" `
    -Encoding ascii

$canonicalProduct = Join-Path $projectRoot "dist\BiliCommentReviewer"
$publishProduct = $canonicalProduct
try {
    Move-ToRecycleBin $canonicalProduct
}
catch {
    $publishProduct = Join-Path $projectRoot "dist\BiliCommentReviewer-next"
    Write-Warning "当前正式目录正在被运行中的旧版占用，新 EXE 将发布到: $publishProduct"
    Move-ToRecycleBin $publishProduct
}
[void](New-Item -ItemType Directory -Path (Split-Path -Parent $publishProduct) -Force)
$resolvedStaged = [System.IO.Path]::GetFullPath($stagedProduct)
$resolvedPublish = [System.IO.Path]::GetFullPath($publishProduct)
foreach ($path in @($resolvedStaged, $resolvedPublish)) {
    if (-not $path.StartsWith($projectRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝移动工作区外路径: $path"
    }
}
Move-Item -LiteralPath $resolvedStaged -Destination $resolvedPublish
$exe = Join-Path $resolvedPublish "BiliCommentReviewer.exe"
Move-ToRecycleBin $stagingRoot

Write-Host "Build OK"
Write-Host "EXE: $exe"
Write-Host "SHA256: $hash"
Write-Host "ZIP: $archive"
Write-Host "ZIP SHA256: $archiveHash"
