# 从 git 历史中彻底移除 PII 与大体积二进制（第 2 关的落地脚本）
#
# 为什么需要它：文件"取消跟踪"只影响之后的提交，**历史里那份还在** ——
# 任何克隆过仓库的人都能 checkout 出来。真正的删除只能靠重写历史。
#
# ⚠️ 这是**破坏性**操作，且本仓库根是 C:\zero\workbuddy（还装着 p12306 / videodemo /
#    销售大屏 等项目）→ 重写后**所有**提交的 SHA 都会变。故：
#      · 必须先有整体镜像备份（脚本会检查）
#      · 必须显式传 -Confirm 才真跑（默认只是分析）
#      · **不会**自动 push（强推要你自己决定，见脚本末尾提示）
#
# 用法：
#   .\scripts\purge-pii-history.ps1                 # 只分析（安全，不改任何东西）
#   .\scripts\purge-pii-history.ps1 -Confirm        # 真重写本地历史

param(
    [switch]$Confirm,
    [string]$RepoRoot = "C:\zero\workbuddy",
    [string]$MirrorBackup = "C:\zero\workbuddy\kechengdemo\backups\workbuddy-mirror-20261015.git"
)

$ErrorActionPreference = "Stop"

# 要清除的路径（相对仓库根）
$PurgePaths = @(
    "kechengdemo/analyze_result_real.json",     # 真实客户评估结果（PII）
    "2026-10-07-17-13-39/training-deai.zip"     # 1.5 GB 二进制（仓库 1.4 GiB 的主要来源）
)

Write-Host "=== 仓库：$RepoRoot"
Write-Host "=== 待清除路径："
$PurgePaths | ForEach-Object { Write-Host "    $_" }

# ---- 安全检查 1：必须有镜像备份 ----
if (-not (Test-Path $MirrorBackup)) {
    Write-Host "❌ 未找到镜像备份：$MirrorBackup"
    Write-Host "   先执行：git clone --mirror `"$RepoRoot`" `"$MirrorBackup`""
    exit 2
}
$backupSize = (Get-ChildItem $MirrorBackup -Recurse -File | Measure-Object Length -Sum).Sum / 1GB
Write-Host ("=== ✅ 镜像备份存在（{0:N2} GB）：{1}" -f $backupSize, $MirrorBackup)

# ---- 安全检查 2：工作树必须干净（否则重写会丢未提交改动）----
$dirty = git -C $RepoRoot status --porcelain
if ($dirty) {
    Write-Host "❌ 工作树有未提交改动，先提交或 stash："
    $dirty | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" }
    exit 3
}

# ---- 分析模式：只报告，不改 ----
if (-not $Confirm) {
    Write-Host "=== 分析模式（不修改任何东西）"
    $args = @("-C", $RepoRoot, "filter-repo", "--analyze")
    & git @args
    Write-Host ""
    Write-Host "要真正执行重写，请加 -Confirm："
    Write-Host "    .\scripts\purge-pii-history.ps1 -Confirm"
    exit 0
}

# ---- 真正重写 ----
Write-Host "=== ⚠️ 开始重写历史（SHA 会全部改变）"
$pathArgs = @()
foreach ($p in $PurgePaths) { $pathArgs += @("--path", $p) }
& git -C $RepoRoot filter-repo --invert-paths --force @pathArgs

Write-Host "=== ✅ 本地历史已重写"
Write-Host "=== 校验：这两个路径应查不到任何提交"
foreach ($p in $PurgePaths) {
    $hits = git -C $RepoRoot log --all --oneline -- $p 2>&1
    if ($hits) { Write-Host "    ❌ 仍能查到 $p" } else { Write-Host "    ✅ 已清除 $p" }
}
Write-Host ""
Write-Host "=== 下一步（**需要你确认后手动执行**）："
Write-Host "    1) 回收本地对象：git -C `"$RepoRoot`" reflog expire --expire=now --all; git -C `"$RepoRoot`" gc --prune=now --aggressive"
Write-Host "    2) 强推远端（若需要）：git -C `"$RepoRoot`" push --force --all; git -C `"$RepoRoot`" push --force --tags"
Write-Host "    3) 通知所有克隆过的人**重新克隆**（旧 SHA 已失效）"
Write-Host "    4) 回退方式（万一）：git clone `"$MirrorBackup`" `"<新目录>`""
