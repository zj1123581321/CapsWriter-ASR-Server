param(
    [Parameter(Mandatory = $true)]
    [string]$Ref,
    [string]$TaskName = $env:DEPLOY_TASK_NAME,
    [string]$Port = $env:DEPLOY_PORT,
    [string]$ModelType = $env:CW_MODEL_TYPE,
    [int]$HealthTimeout = 300,
    [int]$HealthInterval = 2
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repo '.venv\Scripts\python.exe'

if (-not $TaskName) { throw '请设置 -TaskName 或 DEPLOY_TASK_NAME' }
if (-not $Port -or $Port -notmatch '^\d+$' -or [int]$Port -lt 1 -or [int]$Port -gt 65535) {
    throw '请设置有效的 -Port 或 DEPLOY_PORT'
}
if ($ModelType -notin @('qwen_asr', 'fun_asr_nano', 'sensevoice', 'paraformer')) {
    throw "Windows 不支持 CW_MODEL_TYPE: $ModelType"
}
if ($HealthTimeout -lt 1 -or $HealthInterval -lt 1) { throw '健康检查超时和间隔必须为正整数' }

& git -C $repo fetch --tags origin
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& git -C $repo checkout --detach $Ref
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$expectedGitSha = (& git -C $repo rev-parse --short HEAD).Trim()
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path $python)) {
    & python -m venv (Join-Path $repo '.venv')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $python -m pip install -r (Join-Path $repo 'requirements-server.txt')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$scheduledTask = Get-ScheduledTask -TaskName $TaskName
if ($scheduledTask.State -eq 'Running') { Stop-ScheduledTask -TaskName $TaskName }
Start-ScheduledTask -TaskName $TaskName

$healthUrl = "http://127.0.0.1:$Port/health"
$lastStatus = $null
$lastPayload = $null
for ($elapsed = 0; $elapsed -lt $HealthTimeout; $elapsed += $HealthInterval) {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 5 -SkipHttpErrorCheck
    } catch {
        $response = $null
    }
    if ($null -ne $response) {
        $lastStatus = [int]$response.StatusCode
        $lastPayload = $null
        if ($response.Content) { $lastPayload = $response.Content | ConvertFrom-Json }
        if ($lastStatus -eq 200) { break }
    }
    Start-Sleep -Seconds $HealthInterval
}

$actualGitSha = if ($null -ne $lastPayload) { [string]$lastPayload.git_sha } else { '' }
$summary = if ($null -ne $lastPayload) {
    ConvertTo-Json -Compress -InputObject @{
        status = $lastPayload.status
        git_sha = $lastPayload.git_sha
        model = $lastPayload.model
        worker_alive = $lastPayload.worker_alive
    }
} else {
    '{"status":null,"git_sha":null,"model":null,"worker_alive":null}'
}

if ($lastStatus -ne 200) {
    Write-Output "/health 白名单字段: $summary"
    throw "健康检查超时：期望 git_sha=$expectedGitSha 实际=$actualGitSha，最后 HTTP 状态=$lastStatus"
}
if ($actualGitSha -ne $expectedGitSha) {
    Write-Output "/health 白名单字段: $summary"
    throw "健康检查 git_sha 不一致：期望=$expectedGitSha 实际=$actualGitSha"
}

Write-Output "更新完成：task=$TaskName model=$ModelType port=$Port git_sha=$actualGitSha"
