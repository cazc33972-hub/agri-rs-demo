<#
.SYNOPSIS
  启动 / 停止本项目的演示服务（独立进程，不会被终端关闭带走）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\serve.ps1
  powershell -ExecutionPolicy Bypass -File tools\serve.ps1 -Stop
#>
param(
    [int]$Port = 8000,
    [switch]$Stop
)

$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root '.server.pid'
$logDir = Join-Path $root 'reports'
$deps = Join-Path $root '.deps'

function Get-ServerProcess {
    if (-not (Test-Path $pidFile)) { return $null }
    $serverPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $serverPid) { return $null }
    return Get-Process -Id $serverPid -ErrorAction SilentlyContinue
}

if ($Stop) {
    $proc = Get-ServerProcess
    if ($proc) {
        Stop-Process -Id $proc.Id -Force
        Write-Host ("已停止演示服务 (PID {0})" -f $proc.Id)
    } else {
        Write-Host '没有正在运行的演示服务。'
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    return
}

$existing = Get-ServerProcess
if ($existing) {
    Write-Host ("演示服务已在运行 (PID {0})：http://127.0.0.1:{1}" -f $existing.Id, $Port)
    return
}

New-Item -ItemType Directory -Force $logDir | Out-Null
$env:PYTHONPATH = $deps
$env:PYTHONIOENCODING = 'utf-8'

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'D:\PYTH\python.exe' }
if (-not (Test-Path $python)) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw '找不到 Python 解释器，请先装好依赖。' }

$proc = Start-Process -FilePath $python `
    -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port") `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir 'uvicorn.out.log') `
    -RedirectStandardError (Join-Path $logDir 'uvicorn.err.log') `
    -PassThru

$proc.Id | Out-File -Encoding ascii $pidFile
Start-Sleep -Seconds 4

try {
    $r = Invoke-WebRequest -UseBasicParsing ("http://127.0.0.1:{0}/api/health" -f $Port) -TimeoutSec 20
    Write-Host ("演示服务已启动 (PID {0})，健康检查 {1}" -f $proc.Id, $r.StatusCode)
    Write-Host ("打开：http://127.0.0.1:{0}" -f $Port)
} catch {
    Write-Host ("启动失败，请查看 " + (Join-Path $logDir 'uvicorn.err.log'))
    Write-Host $_.Exception.Message
}
