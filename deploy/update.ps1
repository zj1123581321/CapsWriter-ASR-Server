param(
    [Parameter(Mandatory = $true)]
    [string]$Ref,
    [Parameter(Mandatory = $true)]
    [string]$Python,
    [string]$TaskName = $env:DEPLOY_TASK_NAME,
    [string]$Port = $env:DEPLOY_PORT,
    [string]$ModelType = $env:CW_MODEL_TYPE,
    [int]$HealthTimeout = 300,
    [int]$HealthInterval = 2
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonCommand = Get-Command -Name $Python -ErrorAction SilentlyContinue
if (-not (Test-Path -LiteralPath $Python -PathType Leaf) -and $null -eq $pythonCommand) {
    throw "-Python 指向的解释器不存在: $Python"
}

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
$refCommit = & git -C $repo rev-parse --verify "$($Ref)^{commit}"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$refCommit = $refCommit.Trim()

if ($ModelType -in @('qwen_asr', 'fun_asr_nano')) {
    $buildInfoPath = 'core/server/engines/llama_build_info.py'
    & git -C $repo cat-file -e "$($refCommit):$buildInfoPath" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Output "llama 预检跳过：$Ref 无 llama_build_info.py"
    } else {
        $buildInfoLines = & git -C $repo show "$($refCommit):$buildInfoPath"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        $buildInfoText = $buildInfoLines -join "`n"
        $buildMatch = [regex]::Match($buildInfoText, '(?m)^\s*LLAMA_BUILD\s*=\s*"([^"]+)"\s*$')
        if (-not $buildMatch.Success) { throw "无法从 $($Ref):$buildInfoPath 解析 LLAMA_BUILD" }
        $llamaBuild = $buildMatch.Groups[1].Value
        $llamaLibDir = Join-Path $repo "core/server/engines/llama/bin/$llamaBuild"
        $requiredLlamaFiles = @('ggml.dll', 'ggml-base.dll', 'llama.dll')
        $missingLlamaFiles = @($requiredLlamaFiles | Where-Object {
            -not (Test-Path -LiteralPath (Join-Path $llamaLibDir $_) -PathType Leaf)
        })
        if ($missingLlamaFiles.Count -gt 0) {
            $releaseAsset = "llama-$llamaBuild-bin-win-vulkan-x64.zip"
            throw "llama 预检失败：$llamaLibDir 缺少文件：$($missingLlamaFiles -join ', ')；请从 llama.cpp release 获取资产：$releaseAsset"
        }
    }
}

& git -C $repo checkout --detach $Ref
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$expectedGitSha = (& git -C $repo rev-parse --short HEAD).Trim()
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Python -m pip install -r (Join-Path $repo 'requirements-server.txt')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$scheduledTask = Get-ScheduledTask -TaskName $TaskName
if ($scheduledTask.State -eq 'Running') { Stop-ScheduledTask -TaskName $TaskName }
Start-ScheduledTask -TaskName $TaskName

$healthUrl = "http://127.0.0.1:$Port/health"
$lastStatus = $null
$lastPayload = $null
$healthTimer = [Diagnostics.Stopwatch]::StartNew()
while ($healthTimer.Elapsed.TotalSeconds -lt $HealthTimeout) {
    $remainingSeconds = [Math]::Ceiling($HealthTimeout - $healthTimer.Elapsed.TotalSeconds)
    $requestTimeout = [Math]::Max(1, [Math]::Min(5, [int]$remainingSeconds))
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -TimeoutSec $requestTimeout -SkipHttpErrorCheck
    } catch {
        $response = $null
    }
    if ($null -ne $response) {
        $lastStatus = [int]$response.StatusCode
        $lastPayload = $null
        if ($response.Content) { $lastPayload = $response.Content | ConvertFrom-Json }
        if ($lastStatus -eq 200 -and $lastPayload.git_sha -eq $expectedGitSha) { break }
    }
    $remainingMilliseconds = [int][Math]::Max(
        0,
        ($HealthTimeout - $healthTimer.Elapsed.TotalSeconds) * 1000
    )
    if ($remainingMilliseconds -gt 0) {
        Start-Sleep -Milliseconds ([Math]::Min($HealthInterval * 1000, $remainingMilliseconds))
    }
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
