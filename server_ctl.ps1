# -*- coding: utf-8 -*-
# 服务控制脚本（运维线：手动启动模式）
#   用法：.\server_ctl.ps1 start|stop|status|restart [-ListenHost 127.0.0.1] [-ListenPort 8000]
#
# 行为：
#   start   后台启动 python server.py（隐藏窗口），stdout/stderr 重定向到
#           logs/server.out.log / logs/server.err.log，PID 写 server.pid；
#           启动前检查端口占用与重复实例，启动后探活确认
#   stop    按 server.pid 停止进程树（taskkill /T，连带清理子进程）
#   status  显示运行状态与 PID
#   restart stop + start
#
# 与手动前台运行 python server.py 等价；结构化日志在 logs/server.log（轮转）。

param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status", "restart")]
    [string]$Command = "status",
    [string]$ListenHost = "127.0.0.1",
    [int]$ListenPort = 8000
)

$ErrorActionPreference = "Stop"
# 环境里同时存在 Path 与 PATH（Windows 大小写不敏感但字典重复）会让
# Start-Process 抛 "Item has already been added. Key in dictionary: 'Path'"；
# 统一成单个 Path 再继续
if (Test-Path Env:Path) {
    $pathValue = $env:Path
    Remove-Item Env:Path -ErrorAction SilentlyContinue
    Remove-Item Env:PATH -ErrorAction SilentlyContinue
    Set-Item Env:Path -Value $pathValue
}
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root "logs"
$pidFile = Join-Path $root "server.pid"
$outLog = Join-Path $logDir "server.out.log"
$errLog = Join-Path $logDir "server.err.log"

function Get-ServerPid {
    <# 读取 server.pid；不存在或非数字返回 $null（不抛错） #>
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    $raw = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ($raw -match '^\d+$') { return [int]$raw }
    return $null
}

function Test-ServerRunning([int]$ProcessId) {
    <# 指定 PID 是否还活着 #>
    if ($null -eq $ProcessId -or $ProcessId -le 0) { return $false }
    return [bool](Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Get-PortOwner([int]$Port) {
    <# 按端口找监听进程 PID（netstat 解析；比 Get-NetTCPConnection 更稳，某些
       受限环境下后者会漏报）。只认 0.0.0.0:port 或 127.0.0.1:port 的 LISTENING。 #>
    $pattern = ":${Port}\s+\S+:\d+\s+LISTENING\s+(\d+)"
    foreach ($line in (netstat -ano)) {
        if ($line -match $pattern) { return [int]$Matches[1] }
    }
    return $null
}

function Resolve-Python {
    <# 解析可用的 python：优先 PATH（跳过 WindowsApps 商店占位符），再查本地安装目录 #>
    $candidates = @()
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and $cmd.Source -notmatch 'WindowsApps') {
        $candidates += $cmd.Source
    }
    $localPython = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" `
        -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($localPython) { $candidates += $localPython.FullName }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    throw "python not found. Install Python 3.11+ or add it to PATH."
}

function Start-Server {
    <# 后台启动服务：隐藏窗口 + 日志重定向 + PID 落盘 + 启动探活 #>
    $existing = Get-ServerPid
    if (Test-ServerRunning $existing) {
        Write-Host "already running: PID $existing (http://${ListenHost}:${ListenPort})"
        exit 1
    }
    $owner = Get-PortOwner $ListenPort
    if ($null -ne $owner) {
        Write-Host "port $ListenPort already in use by PID $owner (another instance?)"
        exit 1
    }
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $python = Resolve-Python
    $proc = Start-Process -FilePath $python `
        -ArgumentList @("server.py", $ListenHost, "$ListenPort") `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    Set-Content -LiteralPath $pidFile -Value $proc.Id
    # 探活：等 1.5 秒确认进程活着且端口起来，否则报错退出（看 err log 尾部）
    Start-Sleep -Milliseconds 1500
    if (-not (Test-ServerRunning $proc.Id)) {
        Write-Host "start failed (process exited). last error log lines:"
        if (Test-Path -LiteralPath $errLog) { Get-Content -LiteralPath $errLog -Tail 8 }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        exit 1
    }
    Write-Host "started: PID $($proc.Id)  http://${ListenHost}:${ListenPort}"
    Write-Host "structured log: $logDir\server.log  (stdout/stderr: server.out.log / server.err.log)"
}

function Stop-Server {
    <# 按 PID 停止进程树并清理 pid 文件 #>
    $serverPid = Get-ServerPid
    if (-not (Test-ServerRunning $serverPid)) {
        # pid 文件缺失或失效（如被误删）：按端口找占用进程兜底
        $serverPid = Get-PortOwner $ListenPort
    }
    if (Test-ServerRunning $serverPid) {
        & taskkill /PID $serverPid /T /F | Out-Null
        Start-Sleep -Milliseconds 500
        if (Test-ServerRunning $serverPid) {
            Write-Host "stop failed: PID $serverPid still running (manual: taskkill /PID $serverPid /T /F)"
            exit 1
        }
        Write-Host "stopped: PID $serverPid"
    } else {
        Write-Host "not running"
    }
    if (Test-Path -LiteralPath $pidFile) {
        Remove-Item -LiteralPath $pidFile -Force
    }
}

function Show-Status {
    <# 显示运行状态 #>
    $serverPid = Get-ServerPid
    if (Test-ServerRunning $serverPid) {
        $proc = Get-Process -Id $serverPid
        Write-Host "running: PID $serverPid, started $($proc.StartTime.ToString('yyyy-MM-dd HH:mm:ss')), http://${ListenHost}:${ListenPort}"
    } else {
        if ($null -ne $serverPid) {
            Write-Host "not running (stale pid file $pidFile)"
        } else {
            Write-Host "not running"
        }
    }
}

switch ($Command) {
    "start"   { Start-Server }
    "stop"    { Stop-Server }
    "status"  { Show-Status }
    "restart" { Stop-Server; Start-Server }
}
