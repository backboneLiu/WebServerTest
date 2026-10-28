# 一条命令跑完全部检查：ruff + 格式 + mypy strict + pytest
#
# 注意：`$ErrorActionPreference = 'Stop'` 只对 PowerShell cmdlet 生效，
# **对 uv / python 这类外部命令的非零退出码无效**。早期版本因此出现过
# "ruff 报错却仍然打印 ALL GREEN" 的假绿（CI 上尤其危险）。
# 所以每步都显式检查 $LASTEXITCODE，失败立即以同样的退出码终止。
$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host "== $Name ==" -ForegroundColor Cyan
    & uv @Arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $Name (exit $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Invoke-Checked -Name 'ruff' -Arguments @('run', 'ruff', 'check', '.')
Invoke-Checked -Name 'ruff format' -Arguments @('run', 'ruff', 'format', '--check', '.')
Invoke-Checked -Name 'mypy (strict)' -Arguments @('run', 'mypy', 'src')
Invoke-Checked -Name 'pytest' -Arguments @('run', 'pytest')

Write-Host "ALL GREEN" -ForegroundColor Green
