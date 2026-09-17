#!/usr/bin/env python3
"""오프라인 설치용 hidapi 휠을 vendor/ 에 받아둔다.

인터넷이 되는 머신에서 한 번 실행하고, 폴더째 옮겨서 쓰면 된다.
pip 의 --platform 태그 매칭은 macOS x86_64 태그가 파이썬 버전마다
달라서(10_9 / 10_13 / 10_15) 잘 안 맞는다. 그래서 PyPI 목록에서
직접 고른다.

  python install/fetch-wheels.py
"""
import json
import os
import sys
import urllib.request

PKG = "hidapi"
# 우리가 쓸 조합만. 리눅스도 조금 챙겨둔다.
WANTED = ["macosx_11_0_arm64", "macosx_10_9_x86_64", "macosx_10_13_x86_64",
          "macosx_10_15_x86_64", "win_amd64"]
# 리눅스 휠은 하나에 1.6MB 라 기본으로는 안 받는다
if "--linux" in sys.argv:
    WANTED += ["manylinux2014_x86_64", "manylinux_2_28_aarch64"]
MIN_PY = 39      # cp39 이상

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dest = os.path.join(root, "vendor")
os.makedirs(dest, exist_ok=True)

meta = json.load(urllib.request.urlopen("https://pypi.org/pypi/%s/json" % PKG))
version = meta["info"]["version"]
files = meta["releases"][version]

picked = []
for f in files:
    n = f["filename"]
    if not n.endswith(".whl"):
        continue
    if not any(w in n for w in WANTED):
        continue
    try:
        pyver = int(n.split("-")[2].replace("cp", "").replace("t", ""))
    except ValueError:
        continue
    if pyver < MIN_PY:
        continue
    picked.append(f)

print("%s %s — 휠 %d개" % (PKG, version, len(picked)))
total = 0
for f in picked:
    path = os.path.join(dest, f["filename"])
    if os.path.exists(path) and os.path.getsize(path) == f["size"]:
        print("  있음  %s" % f["filename"])
        total += f["size"]
        continue
    with urllib.request.urlopen(f["url"]) as r, open(path, "wb") as out:
        out.write(r.read())
    total += f["size"]
    print("  받음  %s" % f["filename"])

# 소스 배포본도 하나 (휠이 없는 조합의 최후 수단)
for f in files:
    if f["filename"].endswith(".tar.gz"):
        path = os.path.join(dest, f["filename"])
        if not os.path.exists(path):
            with urllib.request.urlopen(f["url"]) as r, open(path, "wb") as out:
                out.write(r.read())
            print("  받음  %s (sdist)" % f["filename"])
        break

print("\nvendor/ 합계 %.1f MB" % (total / 1e6))
print("이 폴더째 오프라인 머신으로 옮기면 install 스크립트가 알아서 씁니다.")
