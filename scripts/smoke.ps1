# 本地验收脚本（对应 TASK-02 验收清单的前 5 条）。
#
# 为什么还需要它：pytest 里的 TestClient 走的是进程内 ASGI 调用，
# 不经过真实 socket、不读 PORT、也不会触发 uvicorn 的 logging 配置。
# 这个脚本把「环境变量 → 真实进程 → 真实 HTTP 请求 → 日志里捞得到」整条链路补上。
#
# 用法（仓库根目录）：pwsh -File scripts/smoke.ps1
$ErrorActionPreference = 'Stop'

# 刻意避开 8000：本地可能正跑着一个开发实例。
$Port = 8011
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

New-Item -ItemType Directory -Force -Path 'data' | Out-Null

# uv 的进程树是 uv.exe -> python.exe -> python.exe，Stop-Process 只能杀最外层，
# 子进程会变成孤儿继续占着端口 → 下一轮新进程绑不上，请求悄悄打到旧代码上。
# 所以启动前先查端口，结束时按端口反查真正的监听者再杀。
function Stop-PortListeners([int]$Port) {
    for ($i = 0; $i -lt 10; $i++) {
        $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if (-not $conns) { return }
        foreach ($c in $conns) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 300
    }
}

Stop-PortListeners $Port
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "端口 $Port 仍被占用，拒绝启动（否则请求会打到旧进程上）"
}

$env:JWT_SECRET = 'smoke-secret-0123456789abcdef-012345'
$env:DATABASE_URL = 'sqlite:///./data/smoke.db'
$env:LOG_LEVEL = 'INFO'
$env:PORT = "$Port"

$proc = Start-Process -FilePath 'uv' `
    -ArgumentList @('run', 'python', '-m', 'pricing_api.serve') `
    -PassThru -NoNewWindow `
    -RedirectStandardOutput 'data/uvicorn.out.log' `
    -RedirectStandardError 'data/uvicorn.err.log'

try {
    $health = $null
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            break
        } catch { }
    }
    if ($null -eq $health) { throw '服务未在 20 秒内就绪' }
    Write-Host ("HEALTH: " + ($health | ConvertTo-Json -Compress))

    $token = (& uv run python scripts/make_token.py --sub smoke --ttl 600).Trim()
    Write-Host ("TOKEN LENGTH: " + $token.Length)

    $body = '{"orderId":"SO-SMOKE-1","tier":"Gold","lines":[{"sku":"A-100","quantity":10,"unitPrice":"5000"}]}'
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/quote" -Method Post `
        -Headers @{ Authorization = "Bearer $token"; 'X-Request-Id' = 'smoke-0001' } `
        -ContentType 'application/json' -Body $body
    Write-Host ("QUOTE: " + $resp.Content)
    Write-Host ("ECHO X-Request-Id: " + $resp.Headers['X-Request-Id'])

    # -SkipHttpErrorCheck：不让 4xx 变成异常，否则拿不到 body（PS7 会把 Response 释放掉）
    $unauth = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/quote" -Method Post `
        -ContentType 'application/json' -Body $body -SkipHttpErrorCheck
    Write-Host ("NO-TOKEN STATUS: " + [int]$unauth.StatusCode + " BODY: " + $unauth.Content)

    $empty = '{"orderId":"SO-EMPTY","tier":"Normal","lines":[]}'
    $emptyResp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/quote" -Method Post `
        -Headers @{ Authorization = "Bearer $token" } `
        -ContentType 'application/json' -Body $empty -SkipHttpErrorCheck
    Write-Host ("EMPTY-LINES STATUS: " + [int]$emptyResp.StatusCode + " BODY: " + $emptyResp.Content)
} finally {
    Stop-PortListeners $Port
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
}

Write-Host '--- server log (last 6 lines) ---'
Get-Content 'data/uvicorn.out.log' -ErrorAction SilentlyContinue | Select-Object -Last 6

# 启动期日志天然没有请求上下文（trace_id 为 "-" 是合理的），只校验请求期的行。
$startup = 'app\.starting|audit\.ready|serve\.starting|Started server process|Waiting for application startup|Application startup complete|Uvicorn running on'
$log = Get-Content 'data/uvicorn.out.log' -ErrorAction SilentlyContinue
$leak = @($log | Where-Object { $_ -match '"logger": "uvicorn.access"' })
$noTrace = @($log | Where-Object { $_ -match '"trace_id": "-"' -and $_ -notmatch $startup })
Write-Host ("ACCESS-LOG LEAK: " + $leak.Count + " 行")
Write-Host ("NO-TRACE LINES: " + $noTrace.Count + " 行")
if ($leak.Count -gt 0 -or $noTrace.Count -gt 0) { Write-Host 'LOG CHECK FAILED'; exit 1 }
Write-Host 'LOG CHECK OK'
