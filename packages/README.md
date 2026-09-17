# packages

바로 내려받아 쓸 수 있는 산출물. 소스에서 재생성할 수 있다.

| 파일 | 용도 |
|---|---|
| `logifollow-*.zip` | 데스크톱 오프라인 설치 번들 (macOS / Windows) |
| `LogiFollow-debug.apk` | 삼성 덱스용 안드로이드 앱 |

## 데스크톱

zip 을 받아 풀고 `START-HERE.txt` 를 보세요. **인터넷이 없어도 됩니다** —
hidapi 휠과 윈도우용 파이썬 설치본이 `vendor/` 에 들어 있습니다.

```sh
# macOS
./install/setup.sh && ./install/macos-service.sh install

# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File .\install\windows-setup.ps1
```

> macOS 에서는 **바탕화면·문서·다운로드 폴더에 두지 마세요.** 그 폴더들은
> TCC 로 보호돼서 자동 실행 서비스가 접근하지 못합니다. 설치 스크립트가
> 감지해 막아주지만, 애초에 `~/workspace/logifollow` 같은 곳에 두는 게 좋습니다.

## 안드로이드

**APK 를 폰에서 직접 받아 설치해도 됩니다.** 같은 키로 서명돼 있어
기존 앱 위에 덮어쓰기 업데이트가 됩니다. 지우고 다시 깔 필요 없습니다.

```sh
adb install -r LogiFollow-debug.apk        # PC 에서 할 경우
```

디버그 서명이므로 폰에서 "출처를 알 수 없는 앱" 설치를 허용해야 할 수 있습니다.
USB 허브로 Logi Bolt 리시버를 폰에 연결한 상태에서 씁니다.

**업데이트 후에는 감시가 자동으로 다시 시작되지 않습니다.** 앱을 설치하면
안드로이드가 프로세스를 종료시키기 때문입니다. 둘 중 하나를 하세요:

- 앱을 열고 **시작** 을 누른다
- 리시버를 뽑았다 다시 꽂는다 (앱이 뜨면서 자동으로 시작)

앱 상태줄에 버전이 보입니다 (`● 감시 중   v0.2`). 갱신이 됐는지 여기서
확인하세요. USB 권한 다이얼로그가 다시 뜰 수 있습니다 — "항상 열기" 를
다시 체크해두면 됩니다.

## 재생성

```sh
./install/make-bundle.sh --fetch    # zip (휠과 파이썬 설치본까지 받아서)
cd android && ./build.sh            # apk
```
