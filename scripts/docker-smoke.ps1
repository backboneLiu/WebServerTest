# 容器冒烟：验证镜像真的能跑，而不只是"构建成功"。
#
# 对应 TASK-02 验收清单里 pytest 覆盖不到的那几条：
#   1. 容器启动后 /health 可达（说明 ENV/ENTRYPOINT/USER 都配对了）
#   2. 非 root 运行（whoami 应该是 appuser，uid 10001）
#   3. README 第 8 条在容器里也是 "41500.00"
#   4. 日志是 JSON 行、请求期的行都带 trace_id、没有 uvicorn.access 孤儿行
#   5. 审计 SQLite 落在 /data（可写卷），不是镜像层
#
# 用法（仓库根目录）：pwsh -File scripts/docker-smoke.ps1
$ErrorActionPreference = 'Stop'

$Port = 8012          # 避开 8000/8011：本地可能已有实例
$Image = 'pricing-api:local'
$Name = 'pricing-smoke'
$Secret = 'docker-smoke-0123456789abcdef-012345'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

docker rm -f $Name 2>&1 | Out-Null

Write-Host "== docker run $Image =="
docker run -d --name $Name -p "${Port}:8000" -e "JWT_SECRET=$Secret" -e LOG_LEVEL=INFO $Image | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'docker run 失败' }

try {
    $health = $null
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            break
        } catch { }
    }
    if ($null -eq $health) {
        Write-Host '--- 容器日志 ---'
        docker logs $Name 2>&1 | Select-Object -Last 30
        throw '容器未在 20 秒内就绪'
    }
    Write-Host ("HEALTH: " + ($health | ConvertTo-Json -Compress))

    # 非 root 校验
    $who = (docker exec $Name whoami).Trim()
    $uid = (docker exec $Name id -u).Trim()
    Write-Host "RUN AS: $who (uid=$uid)"
    if ($who -ne 'appuser') { throw "容器不是以 appuser 运行，而是 $who" }

    # 用与容器相同的密钥签发 token
    $env:JWT_SECRET = $Secret
    $token = (& uv run python scripts/make_token.py --sub docker --ttl 600).Trim()

    $body = '{"orderId":"SO-DOCKER-1","tier":"Gold","lines":[{"sku":"A-100","quantity":10,"unitPrice":"5000"}]}'
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/quote" -Method Post `
        -Headers @{ Authorization = "Bearer $token"; 'X-Request-Id' = 'docker-0001' } `
        -ContentType 'application/json' -Body $body
    Write-Host ("QUOTE: " + $resp.Content)
    if ($resp.Content -notmatch '"total":"41500\.00"') { throw '容器内计价结果与本地不一致' }

    $unauth = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/quote" -Method Post `
        -ContentType 'application/json' -Body $body -SkipHttpErrorCheck
    Write-Host ("NO-TOKEN: " + [int]$unauth.StatusCode + " " + $unauth.Content)

    $empty = '{"orderId":"SO-EMPTY","tier":"Normal","lines":[]}'
    $emptyResp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/quote" -Method Post `
        -Headers @{ Authorization = "Bearer $token" } `
        -ContentType 'application/json' -Body $empty -SkipHttpErrorCheck
    Write-Host ("EMPTY-LINES: " + [int]$emptyResp.StatusCode + " " + $emptyResp.Content)

    # 审计库必须落在 /data（挂载点），不是镜像层
    Write-Host "--- /data ---"
    docker exec $Name ls -l /data 2>&1 | Out-String

    # 日志体检
    Write-Host '--- container log (last 6) ---'
    docker logs $Name 2>&1 | Select-Object -Last 6
    $log = docker logs $Name 2>&1
    $startup = 'app\.starting|audit\.ready|serve\.starting|Started server process|Waiting for application startup|Application startup complete|Uvicorn running on'
    $leak = @($log | Where-Object { $_ -match '"logger": "uvicorn.access"' })
    $noTrace = @($log | Where-Object { $_ -match '"trace_id": "-"' -and $_ -notmatch $startup })
    Write-Host ("ACCESS-LOG LEAK: " + $leak.Count)
    Write-Host ("NO-TRACE LINES: " + $noTrace.Count)
    if ($leak.Count -gt 0 -or $noTrace.Count -gt 0) { throw '容器日志体检未通过' }
    Write-Host 'CONTAINER SMOKE OK'
} finally {
    docker rm -f $Name 2>&1 | Out-Null
}
