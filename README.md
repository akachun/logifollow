# logifollow

*Make your Logitech mouse follow the keyboard's Easy-Switch, on every platform —
including hosts that Logi Options+ refuses to handle.*

키보드가 Easy-Switch 로 채널을 옮기면 **마우스도 같이 옮긴다.**
로지텍 Options+ 의 Enhanced Easy-Switch 가 못 하는 조합에서도 동작한다.

| | |
|---|---|
| 데스크톱 | `logifollow.py` + `ble_transport.py` — macOS (Bolt + BLE++ 실기기 확인) / Windows (Bolt + BLE 0xFF43 실기기 확인) / Linux (미확인) |
| 안드로이드 (폰 / 덱스) | `android/` — USB Bolt + BLE++ 직결 하이브리드 앱, [android/README.md](android/README.md) |
| iOS / iPadOS | `ios/` — SwiftUI + CoreBluetooth BLE++ 직결 앱, [ios/README.md](ios/README.md) |
| 프로토콜 근거 | [HANDOFF.md](HANDOFF.md) — 전부 실측 |

## 왜 Options+ 로는 안 되는가

Options+ 는 계정에 등록된 **컴퓨터 이름**으로 채널을 짝짓는다. 그래서 상대편
컴퓨터에 Options+ 가 깔려 있지 않으면 — 예를 들어 안드로이드 폰에 꽂은
리시버 — 그 채널을 "이름 없는 Bolt receiver" 로만 보고 마우스를 안 데려간다.

logifollow 는 이름 대신 기기가 직접 알려주는 **무선 엔드포인트 식별자**
(`0x1815` 의 호스트 디스크립터)를 비교한다. 상대 컴퓨터에 무엇이 깔려
있든 상관없다.

## 무엇이 되고 무엇이 안 되는가

**된다 — 여기서 나갈 때.** 키보드와 마우스가 둘 다 이 컴퓨터에 붙어 있을 때
키보드가 떠나면, 떠나기 직전에 쏘는 `0x1814` 이벤트를 받아 마우스도 같은
곳으로 보낸다.

**안 된다 — 돌아올 때.** 마우스는 자기가 선택한 채널로만 수신한다. 마우스가
다른 컴퓨터에 가 있으면 이쪽에서는 말을 걸 방법이 없다. 프로토콜의 한계다.
돌아올 때는 마우스 버튼으로 수동 전환해야 한다.

**USB Bolt 리시버와 블루투스(BLE++) 직결 모두 된다.**
초기에는 표준 BLE HID(`0x1812` / `0xFF43`) 경로만 시도해 트리거 불가로 결론냈었으나,
로지텍 벤더 커스텀 GATT 서비스(`00010000-0000-1000-8000-011F2000046D`, 특성 `00010001-...`, 18B BLE++ 포맷)를
사용하면 macOS · Android · iOS/iPadOS 모두 OS의 HID 차단을 우회해 `0x1814` 알림 수신 및 마우스 전환이 가능하다.
데스크톱 기본 모드(`--mode auto`)는 USB 리시버와 블루투스 세트가 함께 연결돼 있으면 **동시에 감시**한다.
자세한 실측 내용은 [HANDOFF.md](HANDOFF.md) 참조.

## 바로 받기

빌드 없이 쓰려면 [`packages/`](packages/) 에 최신 산출물이 있다.

| | |
|---|---|
| 데스크톱 | `packages/logifollow-*.zip` — 풀고 `START-HERE.txt` |
| 안드로이드 | `packages/LogiFollow-debug.apk` — `adb install -r` |

zip 에는 hidapi 휠과 윈도우용 파이썬 설치본이 들어 있어 **인터넷 없이도
설치된다.**

## 설치

### 한 파일로 옮기기

```sh
./install/make-bundle.sh --fetch     # 휠 + 윈도우 파이썬을 받고 zip 으로 묶는다
./install/make-bundle.sh             # 이미 받아둔 것으로 묶기만
```

`dist/logifollow-YYYYMMDD.zip` 하나가 나온다 (약 30MB). USB 로 옮겨 풀고
`START-HERE.txt` 대로 하면 **인터넷 없이** 설치가 끝난다. 파이썬이 없는
윈도우 머신도 `vendor/python-3.13.15-amd64.exe` 가 들어 있어 커버된다.

### 인터넷이 막힌 머신

`vendor/` 에 hidapi 휠을 미리 받아뒀다 (cp39~cp314, macOS arm64/x86_64,
Windows amd64). 설치 스크립트가 **동봉 휠을 먼저 쓰고** 맞는 게 없을 때만
PyPI 로 간다. 그러니 **폴더째 USB 로 옮겨서 그냥 실행하면 된다.**

`python -m venv` 가 쓰는 pip 은 파이썬에 내장돼 있어 인터넷이 필요 없다.

휠을 갱신하거나 리눅스용까지 받으려면 인터넷 되는 머신에서:

```sh
python install/fetch-wheels.py            # macOS + Windows
python install/fetch-wheels.py --linux    # 리눅스까지 (18MB 늘어남)
```

> 파이썬 자체는 대상 머신에 있어야 한다. macOS 는 Xcode Command Line Tools
> 의 `/usr/bin/python3` 이면 되고, 윈도우는 python.org 설치본을 미리
> 받아서 함께 옮겨야 한다.

## 설치 절차

### macOS / Linux

> **바탕화면·문서·다운로드 폴더에 두지 말 것.** macOS 가 이 폴더들을 TCC 로
> 보호해서, launchd 가 띄운 서비스는 자기 venv 조차 못 읽고
> `Operation not permitted` 로 죽는다. 터미널에서 직접 실행하면 터미널의
> 권한으로 되기 때문에 원인을 찾기 어렵다. `~/workspace/logifollow` 처럼
> 홈 아래 평범한 경로에 두면 된다. `macos-service.sh install` 이 이 위치를
> 감지해 미리 막는다.

```sh
./install/setup.sh                    # venv + hidapi
./.venv-hid/bin/python logifollow.py map   # 대응표 확인
./install/macos-service.sh install    # 로그인 시 자동 실행 (macOS)
```

| | |
|---|---|
| 상태·로그 | `./install/macos-service.sh status` |
| 재시작 | `./install/macos-service.sh restart` |
| 해제 | `./install/macos-service.sh uninstall` |

리시버를 뽑았다 꽂거나 다른 포트로 옮긴 뒤 동작이 이상하면 `restart` 가
가장 빠르다.

### Windows

PowerShell 에서 (실행 정책 때문에 `-ExecutionPolicy Bypass` 가 필요할 수 있다):

```powershell
powershell -ExecutionPolicy Bypass -File .\install\windows-setup.ps1
```

| | |
|---|---|
| 상태·로그 | `.\install\windows-setup.ps1 -Status` |
| 해제 | `.\install\windows-setup.ps1 -Uninstall` |

파이썬이 없으면 먼저 `vendor\python-3.13.15-amd64.exe` 를 실행한다
("Add python.exe to PATH" 체크, 사용자 설치면 되고 관리자 권한 불필요).
설치 후 PowerShell 을 새로 열어야 PATH 가 잡힌다.

작업 스케줄러 등록에 권한이 걸리는 환경이면 **시작프로그램 폴더 방식으로
자동 대체**된다. OneDrive 아래 폴더는 피하는 게 좋다 (온디맨드 동기화 때문).

venv 를 만들고 hidapi 를 깔고, 대응표를 보여준 뒤 로그온 시 자동 실행되는
작업을 등록한다. 해제는 `-Uninstall`.

> 윈도우는 HID 상위 컬렉션을 각각 별개의 디바이스 경로로 노출해서
> short(`0x10`) 와 long(`0x11`) 이 다른 핸들로 갈릴 수 있다. `Receiver`
> 클래스가 크기별로 어느 핸들이 통하는지 왕복으로 확인해 기억한다.
> **2026-09-17 실기기 동작 확인됨.**

## 사용

```
python logifollow.py map                채널 대응표 (USB + BLE++ 동시 출력)
python logifollow.py follow             상시 감시 (USB + BLE++ 동시 감시)
python logifollow.py tray               메뉴바(macOS) / 시스템 트레이(Windows) UI + Edge Switch
python logifollow.py edge show|set|test 화면 경계 전환(Edge Switch) 설정 및 감도 테스트
python logifollow.py follow --dry-run   판단만, 전송 안 함
python logifollow.py --mode ble follow  블루투스 직결만 감시
python logifollow.py --mode usb follow  USB 리시버만 감시
python logifollow.py switch 3           마우스를 채널3 으로 (수동)
python logifollow.py --log a.log follow
```

**처음에는 `--dry-run` 으로 한 번 눌러보길 권한다.** 어떤 판단을 내리는지
로그로만 보여준다.

`map` 출력에서 `키보드 채널N -> 마우스 채널M` 이 의도와 맞는지 확인할 것.
"대응 없음" 이 나오면 그 채널은 키보드와 마우스가 서로 다른 리시버에
페어링돼 있다는 뜻이다.

## 하드코딩하지 않은 것들

기기마다, OS 마다 다르기 때문에 전부 실행 시에 찾는다.

- **HID++ 인터페이스와 리포트 경로** — 벤더 페이지 `0xFF00` 핸들을 모두 열고,
  리시버 레지스터 읽기가 통하는 핸들을 short 용으로, 살아있는 슬롯에
  long ping 이 통하는 핸들을 long 용으로 확정한다
- **슬롯 번호** — 재페어링하면 바뀐다. 1~6 을 ping 하고
  `DeviceNameAndType`(`0x0005`) 의 종류 코드로 키보드(0)/마우스(3)를 가린다
- **feature index** — 매번 IRoot 의 `getFeature` 로 조회한다

## 파일

```
logifollow.py      제품 본체 (USB Bolt + BLE++ 하이브리드)
ble_transport.py   BLE++ 커스텀 GATT 백엔드 (macOS CoreBluetooth / Windows·Linux bleak)
ble_gatt_probe.py  macOS CoreBluetooth BLE++ 진단/스니퍼 도구
hidpp_probe9.py    리시버 진단 도구 (map/follow). 개발용
hidpp_bt.py        블루투스 HID(0xFF43) 진단 + 수동 전환
sniff_bt.py        HID 리포트 덤프
legacy/            개발 과정의 probe1~8. 참고용
android/           안드로이드 앱 (USB Bolt + BLE++ 직결, v0.3)
ios/               iOS / iPadOS 앱 (SwiftUI + CoreBluetooth BLE++)
```

## 마우스가 끊기듯 움직인다면

리시버를 USB 3 허브나 독에 꽂지 마세요. **USB 3.x 는 2.4GHz 에 잡음을 뿜어서**
볼트/유니파잉 리시버를 직접 방해합니다. 컴퓨터에 직접 꽂거나, 연장 케이블로
허브에서 떼어놓으세요. 소프트웨어와 무관한 현상입니다
(감시 프로세스는 CPU 0.3% 정도만 씁니다).

리시버를 옮겨도 **페어링은 그대로입니다.** 페어링은 리시버 안에 있고 호스트와
무관합니다.

## 검증 현황

| 플랫폼 | 상태 |
|---|---|
| macOS | **동작 확인** (Bolt 리시버 + BLE++ 직결 동시 감시, LaunchAgent 상시) |
| Windows | **동작 확인** (Bolt 리시버 + BLE 직결 `hidapi` `0xFF43`, 작업 스케줄러, 100% 오프라인 설치) |
| Android / 삼성 덱스 | **동작 확인** (`android/` v0.3 — USB Bolt 리시버 + BLE++ 직결 모두 확인) |
| iOS / iPadOS | **동작 확인** (`ios/` — CoreBluetooth BLE++ 직결, Xcode 빌드 설치) |
| Linux | 미확인 (USB 코드 경로는 macOS 와 동일, BLE++ 는 `bleak`) |

검증한 기기는 **Logitech MX Keys Mini + Signature Plus M750 (BLE++ 직결 및 Bolt 리시버)** 및
**Alto Keys K98M + MX Master 4 (Logi Bolt 리시버 `046D:C548`)** 조합이다. 같은 HID++ 2.0/4.5 를 쓰는
다른 조합에서도 원리상 동작해야 하며, feature index 와 슬롯 번호는 하드코딩하지 않고 매번 조회한다.

## 라이선스

MIT. [LICENSE](LICENSE) 참조.

로지텍과 무관한 개인 프로젝트다. HID++ 는 공개 문서화된 프로토콜이 아니며,
이 저장소의 내용은 전부 실제 기기로 관측해 확인한 것이다.
