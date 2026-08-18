param(
    [string]$ExePath = "",
    [switch]$KeepData
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
if (-not $ExePath) {
    $ExePath = Join-Path $projectRoot "dist\BiliCommentReviewer\BiliCommentReviewer.exe"
}
$ExePath = (Resolve-Path -LiteralPath $ExePath).Path
$testRoot = Join-Path (
    [System.IO.Path]::GetTempPath()
) ("BiliCommentReviewer-smoke-" + [guid]::NewGuid().ToString("N"))
[void](New-Item -ItemType Directory -Path $testRoot)
$legacyRoot = Join-Path (
    [System.IO.Path]::GetTempPath()
) ("BiliCommentReviewer-legacy-" + [guid]::NewGuid().ToString("N"))
[void](New-Item -ItemType Directory -Path $legacyRoot)
$process = $null

function Wait-ForRuntime([string]$RuntimePath, [int]$TimeoutSeconds = 30) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while (-not (Test-Path -LiteralPath $RuntimePath)) {
        if ([DateTime]::UtcNow -gt $deadline) {
            throw "等待 runtime.json 超时"
        }
        Start-Sleep -Milliseconds 200
    }
    Get-Content -LiteralPath $RuntimePath -Raw | ConvertFrom-Json
}

function Wait-ForHealth([string]$BaseUrl, [int]$TimeoutSeconds = 30) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            return Invoke-RestMethod -Uri "$BaseUrl/api/health" -TimeoutSec 2
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "等待健康检查超时"
}

function Post-Json([string]$Url, [hashtable]$Body) {
    Invoke-RestMethod `
        -Uri $Url `
        -Method Post `
        -ContentType "application/json" `
        -Body ($Body | ConvertTo-Json -Depth 8) `
        -TimeoutSec 10
}

function Move-TestDataToRecycleBin([string]$Target) {
    if (-not (Test-Path -LiteralPath $Target)) {
        return
    }
    $resolved = (Resolve-Path -LiteralPath $Target).Path
    $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if (-not $resolved.StartsWith(
        $tempRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "拒绝清理临时目录之外的路径: $resolved"
    }
    Add-Type -AssemblyName Microsoft.VisualBasic
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
        $resolved,
        [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
        [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
    )
}

try {
    $env:BILI_PRODUCT_DATA_DIR = $testRoot
    $env:BILI_DISABLE_BROWSER = "1"
    # 故意给父进程一个调试上限，验证发布入口会清除它。
    $env:BILI_REVIEW_HARD_LIMIT = "110"
    $process = Start-Process `
        -FilePath $ExePath `
        -PassThru `
        -WindowStyle Hidden

    $runtime = Wait-ForRuntime (Join-Path $testRoot "runtime.json")
    $baseUrl = "http://127.0.0.1:$($runtime.port)"
    $health = Wait-ForHealth $baseUrl
    if (-not $health.ok -or -not $health.product_mode) {
        throw "health/product mode 校验失败"
    }

    $page = Invoke-WebRequest -Uri "$baseUrl/" -TimeoutSec 10
    if ($page.StatusCode -ne 200) {
        throw "首页不是 HTTP 200"
    }
    if ($page.Content -notmatch "const REVIEW_HARD_LIMIT = Number\(50000\)") {
        throw "发布 EXE 仍带调试读取上限"
    }
    if ($page.Content -notmatch '<option value="50000">') {
        throw "页面缺少 50000 档位"
    }
    if ($page.Content -notmatch '<option value="500" selected>最近 500 条（默认）</option>') {
        throw "页面默认读取数量不是 500"
    }
    if (
        $page.Content -notmatch '连续 3 页没有新增待生成评论' -or
        $page.Content -notmatch 'review_time_range: reviewTimeRange' -or
        $page.Content -notmatch '\.\.\.reviewPreferencesPayload\(\)' -or
        $page.Content -notmatch '/api/review/preferences'
    ) {
        throw "页面缺少三页停止、时间范围持久化或发送门禁参数"
    }
    if (
        $page.Content -notmatch 'id="btn-review-select-all"' -or
        $page.Content -notmatch 'id="btn-review-clear-all"' -or
        $page.Content -notmatch '/api/review/approve-bulk' -or
        $page.Content -notmatch '独立 instructions'
    ) {
        throw "页面缺少批量勾选或提示词生效范围说明"
    }
    if (
        $page.Content -match 'id="cfg-bilibili-uid"' -or
        $page.Content -match 'data-tab="tab-auth"' -or
        $page.Content -match 'id="cfg-auth-enabled"'
    ) {
        throw "页面仍展示手填 UID 或未经验证的安全功能"
    }
    $socketClient = Invoke-WebRequest `
        -Uri "$baseUrl/static/socket.io.min.js" `
        -TimeoutSec 10
    if (
        $socketClient.StatusCode -ne 200 -or
        $socketClient.Content -notmatch "Socket.IO v4.7.2"
    ) {
        throw "本地 Socket.IO 客户端未随 EXE 提供"
    }

    $accounts = Invoke-RestMethod -Uri "$baseUrl/api/accounts" -TimeoutSec 10
    $firstId = [string]$accounts.current_account_id
    $defaultConfig = Invoke-RestMethod -Uri "$baseUrl/api/config" -TimeoutSec 10
    if (
        [int]$defaultConfig.config.bilibili.check_interval -ne 600 -or
        [bool]$defaultConfig.config.bilibili.auto_start_monitor -ne $false -or
        [double]$defaultConfig.config.rate_limit.min_request_interval -ne 10 -or
        [int]$defaultConfig.config.rate_limit.max_retries -ne 3 -or
        [int]$defaultConfig.config.rate_limit.retry_delay -ne 20 -or
        [int]$defaultConfig.config.reply.max_process -ne 500
    ) {
        throw "发布 EXE 的默认参数未恢复为 500/600/10/3/20"
    }
    $preferences = Post-Json "$baseUrl/api/review/preferences" @{
        limit = 100
        review_time_range = "24h"
        review_since = ""
    }
    if (-not $preferences.ok) {
        throw "审核读取偏好保存失败"
    }
    $preferenceConfig = Invoke-RestMethod `
        -Uri "$baseUrl/api/config" `
        -TimeoutSec 10
    if (
        [int]$preferenceConfig.config.reply.max_process -ne 100 -or
        [string]$preferenceConfig.config.reply.review_time_range -ne "24h" -or
        [string]$preferenceConfig.config.reply.review_since -ne ""
    ) {
        throw "审核读取数量或24小时时间范围没有按账号持久化"
    }
    try {
        Post-Json "$baseUrl/api/review/send" @{
            comment_ids = @("smoke-not-sent")
            review_time_range = "invalid-range"
            review_since = ""
        }
        throw "发送接口接受了无效时间范围"
    }
    catch {
        if (
            -not $_.Exception.Response -or
            [int]$_.Exception.Response.StatusCode -ne 400
        ) {
            throw
        }
    }
    if (
        [string]$defaultConfig.config.ark.system_prompt -notmatch "默认不要使用" -or
        [string]$defaultConfig.config.ark.system_prompt -notmatch "哈哈哈"
    ) {
        throw "发布 EXE 的默认豆包提示词缺少已确认约束"
    }
    $saved = Post-Json "$baseUrl/api/config" @{
        bilibili = @{
            uid = "smoke-account-1"
            cookie = "SESSDATA=smoke-secret"
        }
        ark = @{ api_key = "smoke-ark-secret" }
        reply = @{ enabled = $false }
    }
    if (-not $saved.ok) {
        throw "账号 1 配置保存失败"
    }
    $monitorStart = Post-Json "$baseUrl/api/bot/start" @{}
    if (-not $monitorStart.ok) {
        throw "草稿监控启动失败"
    }
    $monitorConfig = Invoke-RestMethod -Uri "$baseUrl/api/config" -TimeoutSec 10
    $monitorStatus = Invoke-RestMethod -Uri "$baseUrl/api/bot/status" -TimeoutSec 10
    if (
        [bool]$monitorConfig.config.bilibili.auto_start_monitor -ne $true -or
        -not [bool]$monitorStatus.running
    ) {
        throw "草稿监控开启状态没有持久化"
    }
    $monitorStop = Post-Json "$baseUrl/api/bot/stop" @{}
    if (-not $monitorStop.ok) {
        throw "草稿监控停止失败"
    }
    $monitorConfig = Invoke-RestMethod -Uri "$baseUrl/api/config" -TimeoutSec 10
    if ([bool]$monitorConfig.config.bilibili.auto_start_monitor -ne $false) {
        throw "草稿监控关闭状态没有持久化"
    }

    $created = Post-Json "$baseUrl/api/accounts" @{ name = "烟测账号 2" }
    $secondId = [string]$created.account.id
    $saved = Post-Json "$baseUrl/api/config" @{
        bilibili = @{ uid = "smoke-account-2" }
    }
    if (-not $saved.ok) {
        throw "账号 2 配置保存失败"
    }
    $config2 = Invoke-RestMethod -Uri "$baseUrl/api/config" -TimeoutSec 10
    if ([string]$config2.config.bilibili.uid -ne "smoke-account-2") {
        throw "账号 2 配置串号"
    }

    $selected = Post-Json "$baseUrl/api/accounts/select" @{
        account_id = $firstId
    }
    if (-not $selected.ok) {
        throw "切回账号 1 失败"
    }
    $config1 = Invoke-RestMethod -Uri "$baseUrl/api/config" -TimeoutSec 10
    if ([string]$config1.config.bilibili.uid -ne "smoke-account-1") {
        throw "账号 1 配置串号"
    }
    if (
        $config1.config.bilibili.PSObject.Properties.Name -contains "cookie" -or
        $config1.config.ark.PSObject.Properties.Name -contains "api_key"
    ) {
        throw "保存的凭据被重新暴露给浏览器"
    }

    Set-Content `
        -LiteralPath (Join-Path $legacyRoot "config.toml") `
        -Value "[bilibili]`nuid = `"smoke-legacy`"" `
        -Encoding utf8
    Set-Content `
        -LiteralPath (Join-Path $legacyRoot "history.json") `
        -Value "[]" `
        -Encoding utf8
    $imported = Post-Json "$baseUrl/api/accounts/import" @{
        name = "烟测导入账号"
        source_dir = $legacyRoot
    }
    if ($imported.account.imported_files.Count -ne 2) {
        throw "旧账号导入文件数量不正确"
    }
    $legacyConfig = Invoke-RestMethod -Uri "$baseUrl/api/config" -TimeoutSec 10
    if ([string]$legacyConfig.config.bilibili.uid -ne "smoke-legacy") {
        throw "旧账号导入后配置不正确"
    }

    $secondProcess = Start-Process `
        -FilePath $ExePath `
        -PassThru `
        -WindowStyle Hidden
    if (-not $secondProcess.WaitForExit(10000)) {
        throw "第二次启动未及时退出，单实例失效"
    }
    if ($process.HasExited) {
        throw "第二次启动误杀首个实例"
    }

    $accountRoot = Join-Path $testRoot "accounts"
    $configPath1 = Join-Path (
        Join-Path $accountRoot $firstId
    ) "config.toml"
    $configPath2 = Join-Path (
        Join-Path $accountRoot $secondId
    ) "config.toml"
    if (
        -not (Test-Path -LiteralPath $configPath1) -or
        -not (Test-Path -LiteralPath $configPath2)
    ) {
        throw "账号配置文件未隔离落盘"
    }
    $manifest = Get-Content `
        -LiteralPath (Join-Path $testRoot "accounts.json") `
        -Raw | ConvertFrom-Json
    if ($manifest.accounts.Count -ne 3) {
        throw "账号清单数量不正确"
    }

    [pscustomobject]@{
        Result = "PASS"
        ExePid = $runtime.pid
        Port = $runtime.port
        ReleaseHardLimit = 50000
        ReviewPreferenceLimit = $preferenceConfig.config.reply.max_process
        ReviewPreferenceRange = $preferenceConfig.config.reply.review_time_range
        SendRangeGate = $true
        MonitorPreferencePersisted = $true
        AccountCount = $manifest.accounts.Count
        LocalSocketClient = $true
        SecretsRedacted = $true
        LegacyImportFiles = $imported.account.imported_files.Count
        FirstAccountUid = $config1.config.bilibili.uid
        SecondAccountUid = $config2.config.bilibili.uid
        SecondLaunchExited = $secondProcess.HasExited
        TestRoot = $testRoot
    } | Format-List
}
finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
        [void]$process.WaitForExit(5000)
    }
    Remove-Item Env:BILI_PRODUCT_DATA_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:BILI_DISABLE_BROWSER -ErrorAction SilentlyContinue
    Remove-Item Env:BILI_REVIEW_HARD_LIMIT -ErrorAction SilentlyContinue
    if (-not $KeepData) {
        Move-TestDataToRecycleBin $testRoot
        Move-TestDataToRecycleBin $legacyRoot
    }
}
