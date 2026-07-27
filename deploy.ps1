# 智能部署脚本：推送后拉取，智能判断是否需要等待重载
param([switch]$ForceReload, [switch]$SkipWait)

$PA_URL = "https://gjx.pythonanywhere.com"
$TOKEN = "ce952b9ded0733ed"

Write-Host "=== 1/3 推送代码 ==="
git add -A
$msg = git log --oneline -1 --format="%s" 2>$null
git commit -m "update" 2>$null
$result = git push origin master:master master:main --force 2>&1
Write-Host $result

Write-Host ""
Write-Host "=== 2/3 唤醒PA并拉取 ==="

# 先 ping 唤醒 PA（冷启动最多等 30s）
$wakeOk = $false
for ($i = 0; $i -lt 10; $i++) {
    try {
        $ping = Invoke-RestMethod -Uri "$PA_URL/api/ping" -TimeoutSec 8
        if ($ping.success) { Write-Host "PA已就绪(运行$($ping.uptime_seconds)s)"; $wakeOk = $true; break }
    } catch {
        $code = 0
        try { $code = $_.Exception.Response.StatusCode.value__ } catch {}
        if ($code -eq 404) { Write-Host "PA在线(旧版本)"; $wakeOk = $true; break }
        Write-Host "等待PA ($($i+1)/10)..."; Start-Sleep -Seconds 3
    }
}

if (-not $wakeOk) {
    Write-Host "PA无响应，可能配额耗尽。代码已推送，稍后可手动访问 $PA_URL/api/git-pull?force=1"
    exit 1
}

# 拉取代码
try {
    $pull = Invoke-RestMethod -Uri "$PA_URL/api/git-pull?force=1" -Method POST `
        -Headers @{"X-Deploy-Token"=$TOKEN; "Content-Type"="application/json"} -TimeoutSec 30
    Write-Host "pull: success=$($pull.success)"
    Write-Host "reload: $($pull.reload)"
} catch {
    Write-Host "git-pull失败: $($_.Exception.Message)"
    exit 1
}

Write-Host ""
Write-Host "=== 3/3 验证 ==="

# 判断是否需要等待重载
$needWait = $pull.reload -match '重载已触发|Python' -and -not $SkipWait
if ($ForceReload) { $needWait = $true }

if ($needWait) {
    Write-Host "Python代码已变更，等待新进程就绪(最多30s)..."
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Seconds 3
        try {
            $ping = Invoke-RestMethod -Uri "$PA_URL/api/ping" -TimeoutSec 8
            if ($ping.success -and $ping.uptime_seconds -lt 15) {
                Write-Host "新进程就绪(运行$($ping.uptime_seconds)s)"
                exit 0
            }
        } catch {}
    }
    Write-Host "等待超时，PA可能仍在重载中"
} else {
    Write-Host "静态文件变更，即时生效，无需等待"
    exit 0
}
