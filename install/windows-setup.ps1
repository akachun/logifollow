# logifollow 윈도우 설치 / 진단 / 해제
#
#   powershell -ExecutionPolicy Bypass -File .\install\windows-setup.ps1
#   ... -Status       상태와 로그 보기
#   ... -Uninstall    자동 실행 해제
#
# 인터넷이 없어도 됩니다. vendor\ 의 동봉 휠을 먼저 씁니다.
# 파이썬이 없으면 vendor\python-3.13.15-amd64.exe 를 먼저 실행하세요
# ("Add python.exe to PATH" 체크. 사용자 설치면 되고 관리자 권한은 불필요).

param([switch]$Uninstall, [switch]$Status)

$Root     = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$TaskName = "logifollow"
$Venv     = Join-Path $Root ".venv-hid"
$Py       = Join-Path $Venv "Scripts\python.exe"
$Pyw      = Join-Path $Venv "Scripts\pythonw.exe"
$Script   = Join-Path $Root "logifollow.py"
$LogDir   = Join-Path $env:LOCALAPPDATA "logifollow"
$LogFile  = Join-Path $LogDir "follow.log"
$StartupCmd = Join-Path ([Environment]::GetFolderPath("Startup")) "logifollow.cmd"

# ---------- 해제 ----------
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Test-Path $StartupCmd) { Remove-Item $StartupCmd -Force }
    Get-Process pythonw, python -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($Venv) } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Host "해제했습니다."
    exit 0
}

# ---------- 상태 ----------
if ($Status) {
    Write-Host "== 등록 =="
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host ("작업 상태     : " + $task.State)
        Write-Host ("마지막 실행   : " + $info.LastRunTime)
        Write-Host ("마지막 결과   : " + $info.LastTaskResult + "  (0 = 정상)")
        $act = $task.Actions[0]
        Write-Host ("실행 파일     : " + $act.Execute)
        if (Test-Path $act.Execute) { Write-Host "                있음" }
        else { Write-Host "                ** 없음 - 폴더를 옮겼다면 다시 설치하세요 **" }
    } elseif (Test-Path $StartupCmd) {
        Write-Host "작업 스케줄러 대신 시작프로그램 방식으로 등록돼 있습니다:"
        Write-Host "  $StartupCmd"
    } else {
        Write-Host "등록돼 있지 않습니다."
    }

    Write-Host ""
    Write-Host "== 프로세스 =="
    $procs = Get-Process pythonw, python -ErrorAction SilentlyContinue |
             Where-Object { $_.Path -and $_.Path.StartsWith($Venv) }
    if ($procs) { $procs | ForEach-Object { Write-Host ("PID " + $_.Id + "  " + $_.Path) } }
    else { Write-Host "떠 있지 않습니다." }

    Write-Host ""
    Write-Host "== 로그 (마지막 20줄) =="
    if (Test-Path $LogFile) {
        if ((Get-Item $LogFile).Length -gt 0) { Get-Content $LogFile -Encoding UTF8 -Tail 20 }
        else { Write-Host "(비어 있음 - 프로세스가 아무것도 찍지 못하고 죽었다는 뜻입니다)" }
    } else { Write-Host "(로그 파일 없음)" }
    exit 0
}

# ---------- 설치 ----------
# OneDrive 동기화 폴더는 파일 온디맨드 때문에 말썽이 날 수 있다
if ($Root -like "*\OneDrive\*") {
    Write-Host "경고: 이 폴더가 OneDrive 아래에 있습니다."
    Write-Host "      동기화/온디맨드 때문에 문제가 생길 수 있으니"
    Write-Host "      C:\Users\<이름>\logifollow 같은 곳으로 옮기길 권합니다."
    Write-Host ""
}

# 파이썬 찾기
$PyExe = $null
$c = Get-Command python -ErrorAction SilentlyContinue
if ($c) { $PyExe = $c.Source }
if (-not $PyExe) {
    $c = Get-Command py -ErrorAction SilentlyContinue
    if ($c) { $PyExe = (& py -3 -c "import sys;print(sys.executable)") }
}
if (-not $PyExe) {
    Write-Host "파이썬을 찾지 못했습니다."
    Write-Host "  vendor\python-3.13.15-amd64.exe 를 실행해 설치하세요."
    Write-Host "  설치 화면에서 'Add python.exe to PATH' 를 반드시 체크하세요."
    Write-Host "  설치 후 PowerShell 을 새로 열고 다시 실행하세요."
    exit 1
}
Write-Host "파이썬: $PyExe"

if (-not (Test-Path $Py)) {
    Write-Host "venv 를 만듭니다..."
    & $PyExe -m venv $Venv
    if (-not (Test-Path $Py)) { Write-Host "venv 생성 실패"; exit 1 }
}

# hidapi: 동봉 휠 -> PyPI 순
& $Py -c "import hid" 2>$null
$HaveHid = ($LASTEXITCODE -eq 0)
if (-not $HaveHid) {
    $Vendor = Join-Path $Root "vendor"
    if (Test-Path $Vendor) {
        & $Py -m pip install --no-index --find-links $Vendor --disable-pip-version-check -q hidapi 2>$null
        if ($LASTEXITCODE -eq 0) { Write-Host "hidapi 설치 완료 (동봉 휠 - 오프라인)"; $HaveHid = $true }
    }
}
if (-not $HaveHid) {
    & $Py -m pip install --disable-pip-version-check -q hidapi 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "hidapi 설치 완료 (PyPI)"; $HaveHid = $true }
}
if (-not $HaveHid) {
    $tag = & $Py -c "import sys;v=sys.version_info;print('cp'+str(v[0])+str(v[1]))"
    Write-Host ""
    Write-Host "hidapi 를 설치하지 못했습니다."
    Write-Host "  이 파이썬($tag) 용 휠이 vendor\ 에 없고 PyPI 에도 닿지 못했습니다."
    Write-Host "  vendor\ 에 있는 윈도우용 휠:"
    Get-ChildItem (Join-Path $Root "vendor") -Filter "*win_amd64*.whl" -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host ("    " + $_.Name) }
    Write-Host "  인터넷 되는 머신에서 python install/fetch-wheels.py 로 보충하세요."
    exit 1
}

# 먼저 눈으로 확인 - 여기서 채널 표가 나오면 윈도우에서도 프로토콜이 통한 것이다
Write-Host ""
Write-Host "채널 대응표를 확인합니다..."
& $Py $Script map
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "대응표를 읽지 못했습니다. 자동 실행 등록은 건너뜁니다."
    Write-Host "리시버가 꽂혀 있는지, 키보드와 마우스가 이 리시버 채널에 있는지 확인하고"
    Write-Host "다음으로 자세히 보세요:   .\.venv-hid\Scripts\python.exe logifollow.py map --raw"
    exit 1
}

# 기존에 실행 중이던 이전 프로세스가 있으면 정리한다
Get-Process pythonw, python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($Venv) } |
    Stop-Process -Force -ErrorAction SilentlyContinue

# 로그온 시 자동 실행 (시스템 트레이 + USB/BLE + Edge Switch 통합 모드)
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Arg = '"' + $Script + '" --log "' + $LogFile + '" tray'
$registered = $false
try {
    $Action  = New-ScheduledTaskAction -Execute $Pyw -Argument $Arg -WorkingDirectory $Root
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
        -Settings $Settings -Force -ErrorAction Stop | Out-Null
    $registered = $true
    Write-Host ""
    Write-Host "등록했습니다: 작업 스케줄러 > $TaskName"
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host "백그라운드 감시를 바로 시작했습니다."
} catch {
    # 작업 스케줄러 등록에 권한이 필요한 환경이 있다. 시작프로그램으로 대체한다.
    Write-Host ""
    Write-Host "작업 스케줄러 등록 실패 - 시작프로그램 방식으로 대체합니다."
    Write-Host ("  사유: " + $_.Exception.Message)
    $cmd = '@echo off' + "`r`n" + 'start "" "' + $Pyw + '" ' + $Arg + "`r`n"
    Set-Content -Path $StartupCmd -Value $cmd -Encoding Default
    Write-Host "  만들었습니다: $StartupCmd"
    Start-Process -FilePath $Pyw -ArgumentList $Arg -WorkingDirectory $Root
    Write-Host "백그라운드 감시를 바로 시작했습니다."
    $registered = $true
}

Write-Host "로그: $LogFile"
Write-Host ""
Write-Host "상태 확인:   .\install\windows-setup.ps1 -Status"
Write-Host "해제:        .\install\windows-setup.ps1 -Uninstall"
