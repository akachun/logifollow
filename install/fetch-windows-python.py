#!/usr/bin/env python3
"""윈도우용 파이썬 설치본을 vendor/ 에 받아둔다.

파이썬이 없는 오프라인 윈도우 머신을 위한 것. 인터넷 되는 머신에서
한 번 실행하고 폴더째 옮기면 된다.

  python install/fetch-windows-python.py          최신 3.13
  python install/fetch-windows-python.py 3.12     계열 지정
"""
import hashlib
import os
import re
import sys
import urllib.request

SERIES = sys.argv[1] if len(sys.argv) > 1 else "3.13"
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dest = os.path.join(root, "vendor")
os.makedirs(dest, exist_ok=True)

index = urllib.request.urlopen("https://www.python.org/ftp/python/").read().decode("utf-8", "replace")
vers = sorted({v for v in re.findall(r'href="(%s\.\d+)/"' % re.escape(SERIES), index)},
              key=lambda s: [int(x) for x in s.split(".")])
if not vers:
    sys.exit("%s 계열을 찾지 못했습니다." % SERIES)

for v in reversed(vers):
    url = "https://www.python.org/ftp/python/%s/python-%s-amd64.exe" % (v, v)
    name = os.path.basename(url)
    path = os.path.join(dest, name)
    try:
        req = urllib.request.Request(url, method="HEAD")
        size = int(urllib.request.urlopen(req, timeout=20).headers["Content-Length"])
    except Exception:
        continue                      # 릴리스 디렉터리는 있는데 설치본이 없는 경우
    if os.path.exists(path) and os.path.getsize(path) == size:
        print("이미 있음: %s" % name)
        break
    print("받는 중: %s (%.1f MB)" % (name, size / 1e6))
    with urllib.request.urlopen(url) as r, open(path, "wb") as out:
        out.write(r.read())
    print("  sha256 %s" % hashlib.sha256(open(path, "rb").read()).hexdigest()[:16])
    break
else:
    sys.exit("설치본을 찾지 못했습니다.")

print("""
오프라인 윈도우 머신에서:
  1) vendor\\%s 실행
     "Add python.exe to PATH" 체크. 관리자 권한 없이 사용자 설치로 충분하다.
  2) PowerShell 에서  .\\install\\windows-setup.ps1
""" % name)
