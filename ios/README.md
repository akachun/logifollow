# LogiFollow for iOS / iPadOS

로지텍 커스텀 BLE++ GATT 서비스(`00010000-0000-1000-8000-011F2000046D`)를 사용해,
iPhone / iPad에 블루투스로 연결된 키보드의 Easy-Switch 전환을 감지하고 마우스를 함께 전환하는 앱입니다.

## 빌드 및 설치 방법 (본인 또는 타인 기기 모두 가능)

1. `ios/LogiFollow.xcodeproj`를 **Xcode**로 엽니다.
2. 좌측 프로젝트 → `LogiFollow` 타깃 → **Signing & Capabilities** 탭:
   - **Team**에 본인 Apple ID(무료 계정도 가능)를 선택합니다.
   - 필요 시 **Bundle Identifier**를 고유한 값(예: `dev.logifollow.본인이름`)으로 변경합니다.
3. 테스트할 iPhone 또는 iPad를 Mac에 USB로 연결합니다.
   - *(기기에 로그인된 Apple ID가 개발자 계정과 달라도 설치 가능합니다.)*
   - 기기에서 `설정 > 개인정보 보호 및 보안 > 개발자 모드` 활성화.
4. Xcode 상단 타깃에서 연결된 기기를 선택하고 **Run (Cmd+R)** 실행.
5. 최초 실행 시 기기에서 `설정 > 일반 > VPN 및 기기 관리` → 개발자 앱 **신뢰** 선택.

> 무료 Apple ID는 서명이 7일간 유효하며, 유료 Apple Developer 계정($99/년)은 1년 유지 및 TestFlight 원격 배포가 가능합니다.

## 사용 방법

1. iOS/iPadOS `설정 > Bluetooth`에서 MX Keys Mini와 Logi M750을 먼저 연결합니다.
2. `LogiFollow` 앱을 열고 **시작**을 탭합니다 (블루투스 권한 허용).
3. 키보드/마우스 상태와 채널 대응표가 뜨면, 먼저 **시험 모드**를 켜고 Easy-Switch 키를 눌러 로그를 확인합니다.
4. 시험 모드를 끄고 실제 전환을 사용합니다. (`UIBackgroundModes: bluetooth-central`이 포함되어 백그라운드에서도 동작합니다.)
