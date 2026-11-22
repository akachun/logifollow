#!/bin/sh
# 다른 머신으로 통째로 옮길 zip 하나를 만든다.
#
#   ./install/make-bundle.sh            있는 것으로 묶기만
#   ./install/make-bundle.sh --fetch    휠 + 윈도우 파이썬을 먼저 받고 묶기
#
# 만들어진 zip 을 USB 로 옮겨 풀면 인터넷 없이 설치가 끝난다.
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

PY="$ROOT/.venv-hid/bin/python"
[ -x "$PY" ] || PY=$(command -v python3)

if [ "$1" = "--fetch" ]; then
    echo "== 의존성 내려받기 =="
    "$PY" install/fetch-wheels.py
    "$PY" install/fetch-windows-python.py
    echo
fi

STAMP=$(date +%Y%m%d)
NAME="logifollow-$STAMP"
OUT="$ROOT/dist"
STAGE="$OUT/$NAME"
rm -rf "$STAGE" "$OUT/$NAME.zip"
mkdir -p "$STAGE"

# 본체와 도구
cp logifollow.py ble_transport.py ble_gatt_probe.py hidpp_probe9.py hidpp_bt.py sniff_bt.py README.md HANDOFF.md "$STAGE/"
cp -R install "$STAGE/"
cp -R vendor "$STAGE/"

# 안드로이드는 소스와 APK 만. 빌드 산출물과 머신별 경로는 뺀다.
mkdir -p "$STAGE/android"
cp -R android/app android/gradle android/*.kts android/gradle.properties \
      android/gradlew android/gradlew.bat android/build.sh android/README.md \
      "$STAGE/android/" 2>/dev/null || true
cp android/LogiFollow-debug.apk "$STAGE/android/" 2>/dev/null || true
rm -rf "$STAGE/android/app/build" "$STAGE/android/.gradle" "$STAGE/android/local.properties"

# PowerShell 5.1 은 BOM 없는 .ps1 을 ANSI(한국어 윈도우면 CP949)로 읽어서
# 한글이 깨지고 따옴표 짝이 무너진다. 묶을 때마다 BOM+CRLF 를 보장한다.
for ps in "$STAGE"/install/*.ps1; do
    [ -f "$ps" ] || continue
    "$PY" - "$ps" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
b = p.read_bytes()
if b[:3] == b"\xef\xbb\xbf":
    b = b[3:]
text = b.decode("utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
p.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
print("  BOM+CRLF 보장: %s" % p.name)
PYEOF
done

# 캐시 정리
find "$STAGE" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name "*.pyc" -delete 2>/dev/null || true

# 윈도우 메모장이 CP949 로 읽어 한글이 깨지지 않게 BOM + CRLF 로 만든다
printf '\xef\xbb\xbf' > "$STAGE/START-HERE.txt"
cat >> "$STAGE/START-HERE.txt" <<'TXTEOF'
logifollow - 키보드를 따라 마우스 채널을 옮긴다

인터넷 없이 설치할 수 있습니다. vendor/ 에 필요한 것이 다 들어 있습니다.

[ macOS ]
  !! 이 폴더를 바탕화면(Desktop)/문서/다운로드 안에 두지 마세요 !!
     macOS 가 그 폴더들을 보호해서, 자동 실행되는 서비스가 접근하지 못하고
     즉시 죽습니다. 터미널에서 직접 실행할 때는 되기 때문에 헷갈립니다.
     ~/workspace/logifollow 처럼 홈 아래 평범한 곳에 두세요.

  터미널에서 이 폴더로 이동한 뒤:
      ./install/setup.sh
      ./.venv-hid/bin/python logifollow.py map      <- 채널 대응표 확인
      ./install/macos-service.sh install            <- 로그인 시 자동 실행

[ Windows ]
  파이썬이 없으면 먼저:
      vendor\python-3.13.15-amd64.exe
      ("Add python.exe to PATH" 체크. 사용자 설치로 충분, 관리자 권한 불필요)

  그다음 PowerShell 에서 이 폴더로 이동해:
      .\install\windows-setup.ps1

[ 확인 ]
  map 출력의 "키보드 채널N -> 마우스 채널M" 이 의도와 맞는지 보세요.
  "대응 없음" 이 나오면 그 채널은 키보드와 마우스가 서로 다른 리시버에
  페어링돼 있다는 뜻입니다.

[ 전송 방식 ]
  USB Logi Bolt 리시버와 블루투스 직결(BLE++ 커스텀 GATT)을 모두 지원합니다.
  기본값(--mode auto)은 둘 다 연결돼 있으면 동시에 감시합니다.
  특정 방식만 쓰려면 --mode usb 또는 --mode ble 옵션을 주세요.

자세한 내용: README.md
TXTEOF
# CRLF 로 변환
perl -pi -e 's/(?<!\r)\n/\r\n/' "$STAGE/START-HERE.txt" 2>/dev/null || true

cd "$OUT"
zip -qr "$NAME.zip" "$NAME"
rm -rf "$STAGE"
cd "$ROOT"

echo "만들었습니다: dist/$NAME.zip  ($(du -h "$OUT/$NAME.zip" | cut -f1))"
echo "이 파일 하나만 옮기면 됩니다. 풀고 START-HERE.txt 를 보세요."
