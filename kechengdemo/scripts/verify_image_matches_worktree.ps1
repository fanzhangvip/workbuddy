# -*- coding: utf-8 -*-
# 核对「容器里跑的是不是工作区最新的代码」—— 逐文件比 SHA256，并比前端产物名。
#
# 为什么需要它：改了代码只重启容器（`docker compose restart`）**不会**带上新代码 ——
# 必须 `up -d --build` 重新构建镜像。这个脚本让「镜像是不是新的」变成一条可复跑的检查，
# 而不是靠印象回答。
#
# 用法：
#     .\scripts\verify_image_matches_worktree.ps1
param(
    [string]$Container = "weiban-scoring-app",
    [string]$RepoRoot  = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"

$hostHashes = @{}
Get-ChildItem "$RepoRoot\backend\*.py", "$RepoRoot\*.py" -File | ForEach-Object {
    $rel = if ($_.DirectoryName -like '*\backend') { "backend/$($_.Name)" } else { $_.Name }
    $hostHashes[$rel] = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
}

$raw = docker exec $Container sh -c 'cd /app && for f in *.py backend/*.py; do echo "$f $(sha256sum $f | cut -c1-64)"; done'
$diff = @(); $same = 0; $extra = @()
foreach ($line in ($raw -split "`n")) {
    if (-not $line.Trim()) { continue }
    $parts = $line.Trim() -split '\s+'
    $rel, $hash = $parts[0], $parts[1]
    if (-not $hostHashes.ContainsKey($rel)) { $extra += $rel; continue }
    if ($hostHashes[$rel] -eq $hash) { $same++ }
    else { $diff += "$rel  容器=$($hash.Substring(0,8))  工作区=$($hostHashes[$rel].Substring(0,8))" }
}

Write-Host "=== 后端 Python ==="
Write-Host "一致 $same 个 | 不一致 $($diff.Count) 个 | 容器多出 $($extra.Count) 个"
$diff  | ForEach-Object { Write-Host "  ✗ $_" -ForegroundColor Red }
$extra | ForEach-Object { Write-Host "  ? 仅容器有：$_" -ForegroundColor Yellow }

$localJs  = (Get-ChildItem "$RepoRoot\frontend\dist\assets\*.js" -ErrorAction SilentlyContinue |
             Sort-Object Name | Select-Object -Last 1).Name
$remoteJs = (docker exec $Container sh -c 'ls /app/frontend/dist/assets/*.js | xargs -n1 basename' | Select-Object -Last 1)
Write-Host "=== 前端产物 ==="
Write-Host "工作区 $localJs | 容器 $remoteJs"

if ($diff.Count -eq 0 -and $localJs -eq $remoteJs) {
    Write-Host "`n✅ 容器跑的就是工作区最新代码（后端逐文件一致 + 前端产物同名）" -ForegroundColor Green
    exit 0
}
Write-Host "`n❌ 不一致 —— 需要重新构建：docker compose --profile https --profile ops up -d --build" -ForegroundColor Red
exit 1
