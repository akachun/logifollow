#!/usr/bin/env python3
"""logifollow_app.py - 단독 실행파일(PyInstaller .app / .exe) 진입점.

인자 없이 더블클릭으로 실행하면 자동으로 메뉴바/시스템 트레이(tray) 모드로
시작하고 OS 표준 위치에 로그를 남긴다.
CLI 인자를 넘기면 기존 logifollow.py 와 동일하게 동작한다.
"""

import multiprocessing
import os
import sys

import logifollow


def _default_log_path():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/logifollow/follow.log")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "logifollow", "follow.log")
    return os.path.expanduser("~/.local/state/logifollow/follow.log")


def main():
    multiprocessing.freeze_support()
    # 인자 없이 실행된 경우(더블클릭/앱 런처) -> 트레이 모드로 자동 시작
    if len(sys.argv) <= 1:
        sys.argv = [sys.argv[0], "--log", _default_log_path(), "tray"]
    return logifollow.main()


if __name__ == "__main__":
    sys.exit(main() or 0)
