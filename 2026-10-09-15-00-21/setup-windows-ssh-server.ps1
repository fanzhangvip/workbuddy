<#
    setup-windows-ssh-server.ps1
    为 Windows 配置 OpenSSH Server —— 面向 AI Agent / Codex 的自动化部署通道

    用法（必须以管理员身份运行）：
      powershell -ExecutionPolicy Bypass -File setup-windows-ssh-server.ps1

    可选参数：
      -PublicKey "ssh-ed25519 AAAAC3Nz..."   自动配置免密登录
      -Shell     "C:\Git\bin\bash.exe"       指定默认 shell（默认自动探测 Git Bash）

    说明：
      Windows 家庭版无法作为 RDP 服务端，但完全支持 OpenSSH Server。
      本脚本会把 SSH 默认 shell 设为 Git Bash，使 AI Agent 生成的 bash 命令可直接执行。
#>

param(
    [string]$PublicKey = "",
    [string]$Shell     = ""
)

$ErrorActionPreference = "Continue"

function step($n, $m) { Write-Host ""; Write-Host "[$n] $m" -ForegroundColor Cyan }
function ok($m)       { Write-Host "     OK    $m" -ForegroundColor Green }
function warn($m)     { Write-Host "     注意  $m" -ForegroundColor Yellow }
function fail($m)     { Write-Host "     失败  $m" -ForegroundColor Red }

# ---------------------------------------------------------------- 0 权限检查
step 0 "检查管理员权限"
$wid = [Security.Principal.WindowsIdentity]::GetCurrent()
$wpr = New-Object Security.Principal.WindowsPrincipal($wid)
if (-not $wpr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    fail "本脚本必须由『以管理员身份运行』的 PowerShell 执行"
    Write-Host "     右键开始菜单 -> 终端(管理员) / Windows PowerShell(管理员)" -ForegroundColor Yellow
    exit 1
}
ok "管理员权限确认"

# ---------------------------------------------------------- 1 安装 OpenSSH Server
step 1 "安装 OpenSSH Server 可选功能"
$capName = "OpenSSH.Server~~~~0.0.1.0"
$sshdExe = "$env:WINDIR\System32\OpenSSH\sshd.exe"

$cap = Get-WindowsCapability -Online -Name $capName -ErrorAction SilentlyContinue
if ($cap -and $cap.State -eq "Installed") {
    ok "已安装，跳过"
} else {
    Write-Host "     正在从 Windows Update 下载安装（约 1-3 分钟，请勿关闭窗口）..."
    try {
        Add-WindowsCapability -Online -Name $capName -ErrorAction Stop | Out-Null
        ok "安装完成"
    } catch {
        warn "在线安装失败：$($_.Exception.Message)"
    }
}

if (-not (Test-Path $sshdExe)) {
    fail "未找到 $sshdExe —— 自动安装未成功"
    Write-Host "     手动方案 A：设置 > 应用 > 可选功能 > 添加功能 > 搜索『OpenSSH 服务器』" -ForegroundColor Yellow
    Write-Host "     手动方案 B：下载 OpenSSH-Win64.zip (github.com/PowerShell/Win32-OpenSSH/releases)" -ForegroundColor Yellow
    Write-Host "                 解压到 C:\Program Files\OpenSSH 后运行其中的 install-sshd.ps1" -ForegroundColor Yellow
    exit 1
}
ok "sshd.exe 已就位：$sshdExe"

# ---------------------------------------------------------------- 2 配置服务
step 2 "配置 sshd 服务"
Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service sshd -ErrorAction SilentlyContinue
$svcStatus = (Get-Service sshd).Status
if ($svcStatus -eq "Running") { ok "sshd 服务运行中，启动类型 = 自动" }
else { warn "sshd 状态 = $svcStatus，可稍后执行 Start-Service sshd" }

Set-Service -Name ssh-agent -StartupType Automatic -ErrorAction SilentlyContinue
ok "ssh-agent 启动类型 = 自动"

# ---------------------------------------------------------------- 3 防火墙
step 3 "开放防火墙 22 端口"
$rule = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue
if ($rule) {
    ok "入站规则已存在"
} else {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    ok "已创建入站规则 OpenSSH-Server-In-TCP"
}

# ---------------------------------------------------------------- 4 默认 shell
step 4 "设置 SSH 默认 shell"
if ($Shell -eq "") {
    $cands = @(
        "C:\Git\bin\bash.exe",
        "C:\Program Files\Git\bin\bash.exe",
        "C:\msys64\usr\bin\bash.exe",
        "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe"
    )
    foreach ($c in $cands) {
        if (Test-Path $c) { $Shell = $c; break }
    }
}
if ($Shell -eq "") {
    $Shell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    warn "未探测到 bash，回退为 PowerShell（注意：PowerShell 5.1 不支持 && 连接符）"
} else {
    ok "已探测到 bash：$Shell"
}

if (-not (Test-Path "HKLM:\SOFTWARE\OpenSSH")) {
    New-Item -Path "HKLM:\SOFTWARE\OpenSSH" -Force | Out-Null
}
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name "DefaultShell" -Value $Shell -PropertyType String -Force | Out-Null
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name "DefaultShellCommandOption" -Value "-c" -PropertyType String -Force | Out-Null
ok "默认 shell = $Shell"

# ---------------------------------------------------------------- 5 免密登录
step 5 "配置密钥免密登录"
if ($PublicKey -eq "") {
    warn "未传 -PublicKey 参数，跳过公钥配置"
    Write-Host "     之后可重新运行并带上公钥，例如：" -ForegroundColor Yellow
    Write-Host "       -PublicKey `"ssh-ed25519 AAAAC3Nz... your@machine`"" -ForegroundColor Yellow
} else {
    $userIsAdmin = $false
    try {
        $admins = (Get-LocalGroupMember -Group "Administrators" -ErrorAction Stop) | ForEach-Object { $_.Name }
        if ($admins -contains "$env:COMPUTERNAME\$env:USERNAME") { $userIsAdmin = $true }
    } catch { }

    if ($userIsAdmin) {
        $akPath = "$env:ProgramData\ssh\administrators_authorized_keys"
        ok "当前用户属于管理员组 -> 使用 administrators_authorized_keys"
    } else {
        $sshDir = "$env:USERPROFILE\.ssh"
        if (-not (Test-Path $sshDir)) { New-Item -ItemType Directory -Path $sshDir -Force | Out-Null }
        $akPath = "$sshDir\authorized_keys"
        ok "使用用户级 authorized_keys"
    }

    $existing = @()
    if (Test-Path $akPath) { $existing = Get-Content $akPath -ErrorAction SilentlyContinue }
    if ($existing -notcontains $PublicKey) {
        Add-Content -Path $akPath -Value $PublicKey -Encoding Ascii
        ok "公钥已写入：$akPath"
    } else {
        ok "公钥已存在，未重复写入"
    }

    if ($userIsAdmin) {
        icacls $akPath /inheritance:r | Out-Null
        icacls $akPath /grant "SYSTEM:F" | Out-Null
        icacls $akPath /grant "BUILTIN\Administrators:F" | Out-Null
        ok "已修正权限（仅 SYSTEM + Administrators，否则 sshd 会拒绝该文件）"
    }
}

# ---------------------------------------------------------------- 6 重启与验证
step 6 "重启 sshd 并验证"
Restart-Service sshd -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

$listen = Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue
if ($listen) { ok "22 端口正在监听" } else { fail "22 端口未监听，请检查服务状态" }

Write-Host ""
Write-Host " 本机 IPv4 地址：" -ForegroundColor Cyan
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.PrefixOrigin -ne "WellKnown" } |
    ForEach-Object { Write-Host "     $($_.IPAddress)   <-- $($_.InterfaceAlias)" }

Write-Host ""
Write-Host "==================== 配置完成 ====================" -ForegroundColor Green
Write-Host " 从其他机器连接："
Write-Host "     ssh $env:USERNAME@<上面列出的 IP>"
Write-Host ""
Write-Host " 自检（在远程机器上执行）："
Write-Host "     ssh $env:USERNAME@<IP> 'uname -a; pwd; docker --version'"
Write-Host "==================================================" -ForegroundColor Green
