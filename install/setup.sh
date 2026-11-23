#!/bin/sh
# macOS / Linux 준비: venv 를 만들고 hidapi 를 설치한다.
#
# 인터넷이 없어도 된다. vendor/ 에 받아둔 휠을 먼저 쓰고, 거기 맞는 게
# 없을 때만 PyPI 로 간다. 오프라인 머신에는 폴더째 복사해서 쓰면 된다.
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

PY=$(command -v python3) || { echo "python3 이 필요합니다."; exit 1; }
echo "파이썬: $PY ($("$PY" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))'))"

[ -d .venv-hid ] || "$PY" -m venv .venv-hid
VPY="$ROOT/.venv-hid/bin/python"

if "$VPY" -c "import hid" 2>/dev/null; then
    echo "hidapi 이미 설치됨"
else
    # 1순위: 동봉한 휠 (인터넷 불필요)
    if [ -d vendor ] && "$VPY" -m pip install --no-index --find-links vendor \
            --disable-pip-version-check -q hidapi 2>/dev/null; then
        echo "hidapi 설치 완료 (vendor/ 의 동봉 휠 사용 — 오프라인)"
    # 2순위: PyPI
    elif "$VPY" -m pip install --disable-pip-version-check -q hidapi 2>/dev/null; then
        echo "hidapi 설치 완료 (PyPI)"
    else
        echo
        echo "hidapi 를 설치하지 못했습니다."
        echo "  이 파이썬($("$VPY" -c 'import sys;print("cp%d%d"%sys.version_info[:2])')) 용 휠이"
        echo "  vendor/ 에 없고 PyPI 에도 닿지 못했습니다."
        echo
        echo "  인터넷 되는 머신에서:  python install/fetch-wheels.py"
        echo "  그다음 폴더째 이 머신으로 옮기고 다시 실행하세요."
        echo
        echo "  vendor/ 에 있는 것:"
        ls vendor/*.whl 2>/dev/null | sed 's|.*/|    |' | sed 's/hidapi-[0-9.]*-//' || echo "    (없음)"
        exit 1
    fi
fi

"$VPY" -c "import hid; print('hidapi 확인 완료')"

# BLE++ 직결 및 Edge Switch / 메뉴바 UI 의존성
if [ "$(uname -s)" = "Darwin" ]; then
    if ! "$VPY" -c "import CoreBluetooth, dispatch, Quartz, AppKit" 2>/dev/null; then
        "$VPY" -m pip install --disable-pip-version-check -q \
            pyobjc-framework-CoreBluetooth pyobjc-framework-libdispatch \
            pyobjc-framework-Quartz pyobjc-framework-Cocoa 2>/dev/null \
            && echo "macOS BLE++ / Edge Switch / 메뉴바 UI 의존성 설치 완료" \
            || echo "참고: PyObjC 일부 미설치 (USB 리시버 모드는 정상 동작)"
    fi
else
    if ! "$VPY" -c "import bleak" 2>/dev/null; then
        "$VPY" -m pip install --disable-pip-version-check -q bleak 2>/dev/null \
            && echo "BLE++(bleak) 설치 완료" \
            || echo "참고: BLE++(bleak) 미설치 (USB 리시버 모드는 정상 동작)"
    fi
fi

echo
echo "대응표 확인:  ./.venv-hid/bin/python logifollow.py map"
echo "상시 실행  :  ./install/macos-service.sh install"
