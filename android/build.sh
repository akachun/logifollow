#!/bin/sh
# 이 맥에 맞춰 굳혀 둔 빌드 스크립트.
# JDK 와 SDK 를 PATH 에 안 넣어도 되도록 여기서만 설정한다.
set -e
export JAVA_HOME=/opt/homebrew/opt/openjdk@17
export ANDROID_HOME="$HOME/Library/Android/sdk"
export PATH="$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$PATH"
cd "$(dirname "$0")"
./gradlew "${@:-:app:assembleDebug}"
echo
echo "APK: $(pwd)/app/build/outputs/apk/debug/app-debug.apk"
echo "설치: adb install -r app/build/outputs/apk/debug/app-debug.apk"
