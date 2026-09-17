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

```sh
adb install -r LogiFollow-debug.apk
```

디버그 서명이므로 폰에서 "출처를 알 수 없는 앱" 설치를 허용해야 할 수 있습니다.
USB 허브로 Logi Bolt 리시버를 폰에 연결한 상태에서 씁니다.

## 재생성

```sh
./install/make-bundle.sh --fetch    # zip (휠과 파이썬 설치본까지 받아서)
cd android && ./build.sh            # apk
```
