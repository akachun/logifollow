#!/bin/sh
# logifollow 를 로그인 시 자동 실행되는 LaunchAgent 로 등록/해제한다.
#   ./install/macos-service.sh install
#   ./install/macos-service.sh uninstall
#   ./install/macos-service.sh status
set -e

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LABEL=dev.logifollow
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/logifollow"
PY="$ROOT/.venv-hid/bin/python"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3)

# macOS 는 Desktop/Documents/Downloads 와 iCloud 를 TCC 로 보호한다.
# 터미널에서 직접 실행하면 터미널의 권한으로 되지만, launchd 가 띄운
# 프로세스에는 그 권한이 없어서 자기 venv 의 pyvenv.cfg 조차 못 읽고
# "Operation not permitted" 로 죽는다. 조용히 빠지는 함정이라 미리 막는다.
check_protected() {
    case "$ROOT/" in
        "$HOME"/Desktop/*|"$HOME"/Documents/*|"$HOME"/Downloads/*|\
        "$HOME"/Library/Mobile\ Documents/*|/Volumes/*)
            echo "이 폴더는 macOS 가 보호하는 위치에 있습니다:"
            echo "    $ROOT"
            echo
            echo "launchd 가 띄운 프로세스는 여기를 읽지 못해서 서비스가 즉시 죽습니다."
            echo "(터미널에서 직접 실행할 때는 터미널의 권한으로 되기 때문에 헷갈립니다)"
            echo
            echo "보호받지 않는 곳으로 옮기고 다시 실행하세요. 예:"
            echo "    mkdir -p ~/workspace"
            echo "    mv \"$ROOT\" ~/workspace/logifollow"
            echo "    cd ~/workspace/logifollow"
            echo "    ./install/setup.sh && ./install/macos-service.sh install"
            echo
            echo "(.venv-hid 는 경로가 바뀌면 깨지므로 setup.sh 를 다시 돌려야 합니다)"
            exit 1
            ;;
    esac
}

# bootout 은 비동기다. 곧바로 bootstrap 하면 "Operation already in progress"
# 로 실패하는데, 그러면 plist 파일만 새것이고 launchd 에 로드된 정의는
# 옛것이 남는다. 정말로 사라질 때까지 기다린다.
unload_wait() {
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    i=0
    while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; do
        i=$((i + 1))
        [ "$i" -gt 20 ] && break
        sleep 0.5
    done
}

loaded_program() {
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
        | awk -F" = " '/^[[:space:]]*program = /{print $2; exit}'
}

case "${1:-install}" in
install)
    check_protected
    [ -x "$PY" ] || { echo "python3 을 찾지 못했습니다. install/setup.sh 를 먼저 실행하세요."; exit 1; }
    "$PY" -c "import hid" 2>/dev/null || { echo "hidapi 가 없습니다. install/setup.sh 를 먼저 실행하세요."; exit 1; }
    mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"
    : > "$LOGDIR/follow.err"      # 지난 실행의 오류가 남아 헷갈리지 않게
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$ROOT/logifollow.py</string>
        <string>follow</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <!-- 리시버를 뽑으면 종료된다. 다시 꽂을 때까지 계속 재시도하되
         너무 자주 돌지 않게 30초 간격을 둔다. -->
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardOutPath</key><string>$LOGDIR/follow.log</string>
    <key>StandardErrorPath</key><string>$LOGDIR/follow.err</string>
</dict>
</plist>
PLISTEOF
    unload_wait
    if ! launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>&1; then
        echo
        echo "등록에 실패했습니다. 잠시 후 다시 시도하세요:"
        echo "    ./install/macos-service.sh install"
        exit 1
    fi

    # 정말로 새 경로가 로드됐는지 확인한다. plist 파일만 바뀌고
    # launchd 는 옛 정의를 들고 있는 경우가 실제로 있었다.
    sleep 1
    LOADED=$(loaded_program)
    if [ "$LOADED" = "$PY" ]; then
        echo "등록했습니다: $PY"
        echo "로그: $LOGDIR/follow.log"
    else
        echo "** 경고: launchd 가 다른 경로를 들고 있습니다 **"
        echo "   기대: $PY"
        echo "   실제: ${LOADED:-(없음)}"
        echo
        echo "   아래로 강제 정리한 뒤 다시 등록하세요:"
        echo "       launchctl bootout gui/\$(id -u)/$LABEL"
        echo "       ./install/macos-service.sh install"
        exit 1
    fi
    ;;
uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "해제했습니다."
    ;;
restart)
    # 등록은 그대로 두고 프로세스만 죽였다 살린다.
    # 리시버를 바꿔 꽂았을 때 가장 빠른 복구 방법이다.
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
        launchctl kickstart -k "gui/$(id -u)/$LABEL" && echo "재시작했습니다."
        echo "확인:  ./install/macos-service.sh status"
    else
        echo "등록돼 있지 않습니다. 먼저:  ./install/macos-service.sh install"
        exit 1
    fi
    ;;
status)
    echo "== 등록 =="
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
        launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
            | grep -E "^[[:space:]]*(state|pid|last exit code|program) " || true
    else
        echo "등록돼 있지 않습니다.  ./install/macos-service.sh install"
    fi

    echo
    echo "== 경로 =="
    if [ -f "$PLIST" ]; then
        PPY=$(grep -m1 "bin/python" "$PLIST" | sed 's/.*<string>//; s|</string>.*||')
        PSC=$(grep -m1 "logifollow.py" "$PLIST" | sed 's/.*<string>//; s|</string>.*||')
        LOADED=$(loaded_program)
        if [ -n "$LOADED" ] && [ "$LOADED" != "$PPY" ]; then
            echo "** plist 파일과 launchd 에 로드된 정의가 다릅니다 **"
            echo "   launchd 가 들고 있는 것: $LOADED"
            echo "   -> ./install/macos-service.sh install 을 다시 실행하세요"
            echo
        fi
        echo "plist 파이썬 : $PPY"
        [ -x "$PPY" ] && echo "               있음" || echo "               ** 없음 — 폴더를 옮겼다면 install 을 다시 실행하세요 **"
        echo "plist 스크립트: $PSC"
        [ -f "$PSC" ] && echo "               있음" || echo "               ** 없음 — 폴더를 옮겼다면 install 을 다시 실행하세요 **"
        echo "지금 폴더     : $ROOT"
    else
        echo "plist 없음"
    fi

    echo
    echo "== 프로세스 =="
    pgrep -fl "logifollow.py follow" || echo "떠 있지 않습니다."

    echo
    echo "== 표준 출력 (마지막 20줄) =="
    if [ -s "$LOGDIR/follow.log" ]; then
        tail -20 "$LOGDIR/follow.log"
    else
        echo "(비어 있음 — 프로세스가 아무것도 찍지 못하고 죽었다는 뜻입니다)"
    fi

    echo
    echo "== 오류 출력 =="
    if [ -s "$LOGDIR/follow.err" ]; then
        echo "(마지막 기록: $(stat -f "%Sm" "$LOGDIR/follow.err") — 지금 도는 중이면 지난 실행의 흔적입니다)"
        tail -20 "$LOGDIR/follow.err"
    else
        echo "(없음)"
    fi
    ;;
*)
    echo "사용법: $0 install|restart|uninstall|status"; exit 1;;
esac
