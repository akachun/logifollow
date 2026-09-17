#!/usr/bin/env python3
"""
hidpp_bt_diag.py - 블루투스 직결 기기에 HID++ 가 왜 안 통하는지 진단

추측하지 말고 기기가 선언한 리포트 서술자를 직접 읽는다.
그러면 다음이 확정된다:
  - 0xFF43 컬렉션이 실제로 쓰는 리포트 ID
  - 그 ID 가 Output 인지 Feature 인지
  - 각 리포트의 길이

그리고 세 가지를 실제로 시도한다:
  1) 수동 읽기 — 입력 리포트가 오기는 하는가 (권한 문제 판별)
  2) Output 리포트로 HID++ ping
  3) Feature 리포트로 HID++ ping

사용법:
  uv run --python 3.12 --with hidapi hidpp_bt_diag.py
  uv run --python 3.12 --with hidapi hidpp_bt_diag.py --pid 0xB02C
  uv run --python 3.12 --with hidapi hidpp_bt_diag.py --descriptor-only
"""

import argparse
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("hidapi가 없습니다.  uv run --with hidapi ... 로 실행하세요")

LOGITECH_VID = 0x046D
SW_ID = 0x05
IROOT_PING = 0x01

USAGE_PAGE_NAMES = {
    0x0001: "Generic Desktop", 0x000C: "Consumer", 0x000D: "Digitizer",
    0xFF00: "Logitech 벤더(리시버)", 0xFF0C: "Logitech 벤더",
    0xFF43: "Logitech 벤더(BLE)",
}


def hexs(b):
    return " ".join("%02X" % x for x in b)


# ------------------------------------------------------------ 서술자 파서

def parse_descriptor(data):
    """HID 리포트 서술자에서 리포트 ID 별 방향/크기를 뽑는다."""
    reports = {}          # report_id -> {"in":bits,"out":bits,"feat":bits,"page":page}
    page = 0
    report_id = 0
    size = 0
    count = 0
    i = 0
    n = len(data)

    while i < n:
        b = data[i]
        i += 1
        if b == 0xFE:                      # long item, 건너뛴다
            if i + 1 < n:
                ln = data[i]
                i += 2 + ln
            continue
        item_size = b & 0x03
        if item_size == 3:
            item_size = 4
        tag = b & 0xFC
        val = 0
        for k in range(item_size):
            if i + k < n:
                val |= data[i + k] << (8 * k)
        i += item_size

        if tag in (0x04, 0x06):            # Usage Page
            page = val
        elif tag == 0x84:                  # Report ID
            report_id = val
        elif tag == 0x74:                  # Report Size
            size = val
        elif tag == 0x94:                  # Report Count
            count = val
        elif tag in (0x80, 0x90, 0xB0):    # Input / Output / Feature
            key = {0x80: "in", 0x90: "out", 0xB0: "feat"}[tag]
            r = reports.setdefault(report_id,
                                   {"in": 0, "out": 0, "feat": 0, "pages": set()})
            r[key] += size * count
            r["pages"].add(page)
    return reports


def show_descriptor(h, label):
    try:
        raw = h.get_report_descriptor()
    except Exception as e:
        print("    서술자 읽기 실패: %s" % e)
        return {}
    if not raw:
        print("    서술자가 비었습니다.")
        return {}
    data = bytes(raw)
    print("    서술자 %d바이트" % len(data))
    reports = parse_descriptor(data)
    if not reports:
        print("    리포트 ID를 못 찾았습니다.")
        return {}
    print("    %-10s %-8s %-8s %-8s %s"
          % ("리포트ID", "Input", "Output", "Feature", "usage page"))
    for rid in sorted(reports):
        r = reports[rid]
        pages = ", ".join(
            "0x%04X %s" % (p, USAGE_PAGE_NAMES.get(p, ""))
            for p in sorted(r["pages"]))
        print("    0x%02X       %-8s %-8s %-8s %s"
              % (rid,
                 "%dB" % (r["in"] // 8) if r["in"] else "-",
                 "%dB" % (r["out"] // 8) if r["out"] else "-",
                 "%dB" % (r["feat"] // 8) if r["feat"] else "-",
                 pages))
    return reports


# ------------------------------------------------------------ 시도들

def ping_packet(report_id, length):
    pkt = [report_id, 0xFF, 0x00, (IROOT_PING << 4) | SW_ID, 0x00, 0x00, 0xAA]
    pkt += [0] * length
    return bytes(pkt[:length])


def try_passive_read(h, seconds=4):
    """입력 리포트가 오기는 하는지. 안 오면 권한/전달 문제."""
    print("\n  [1] 수동 읽기 %d초 — 마우스를 움직여보세요" % seconds)
    h.set_nonblocking(1)
    got = []
    end = time.time() + seconds
    while time.time() < end:
        r = h.read(64)
        if r:
            got.append(bytes(r))
            if len(got) <= 3:
                print("      <- %s" % hexs(r))
        else:
            time.sleep(0.01)
    h.set_nonblocking(0)
    if got:
        print("      입력 리포트 %d개 수신. 읽기 경로는 살아 있습니다." % len(got))
    else:
        print("      아무것도 못 받았습니다.")
        print("      -> 권한(입력 모니터링) 또는 macOS 가 전달을 막는 경우")
    return bool(got)


def try_output(h, rid, length):
    pkt = ping_packet(rid, length)
    print("      -> write   %s" % hexs(pkt))
    try:
        n = h.write(pkt)
    except Exception as e:
        print("         실패: %s" % e)
        return False
    if n < 0:
        print("         write 반환 %d" % n)
        return False
    end = time.time() + 1.0
    while time.time() < end:
        r = h.read(64, timeout_ms=200)
        if r and len(r) >= 4 and r[1] == 0xFF:
            print("      <- %s   *** 응답 ***" % hexs(r))
            return True
        if r:
            print("      <- %s" % hexs(r))
    print("         응답 없음")
    return False


def try_feature(h, rid, length):
    pkt = ping_packet(rid, length)
    print("      -> feature %s" % hexs(pkt))
    try:
        n = h.send_feature_report(pkt)
    except Exception as e:
        print("         send_feature 실패: %s" % e)
        return False
    if n < 0:
        print("         send_feature 반환 %d" % n)
        return False
    try:
        r = h.get_feature_report(rid, 64)
    except Exception as e:
        print("         get_feature 실패: %s" % e)
        return False
    if r:
        print("      <- %s" % hexs(r))
        if len(r) >= 4 and r[1] == 0xFF:
            print("         *** 응답 ***")
            return True
    else:
        print("         get_feature 빈 응답")
    return False


def diagnose(path, pid, prod, args):
    print("\n" + "=" * 60)
    print("PID=0x%04X  %s" % (pid, prod))
    print("path: %s" % path.decode("utf-8", "replace"))
    print("=" * 60)

    try:
        h = hid.device()
        h.open_path(path)
        h.set_nonblocking(0)
    except Exception as e:
        print("  열기 실패: %s" % e)
        print("  -> 키보드류라면 시스템 설정 > 개인정보 보호 및 보안 >")
        print("     입력 모니터링 에서 터미널을 허용해야 합니다.")
        return

    try:
        print("\n  [0] 리포트 서술자")
        reports = show_descriptor(h, prod)
        if args.descriptor_only:
            return

        try_passive_read(h)

        # HID++ 후보: 흔히 쓰는 0x10/0x11 과, 서술자에서 나온 것들
        candidates = []
        for rid in (0x10, 0x11):
            r = reports.get(rid)
            if r:
                ln = max(r["out"], r["feat"], r["in"]) // 8 + 1
                candidates.append((rid, max(ln, 7)))
            else:
                candidates.append((rid, 7 if rid == 0x10 else 20))
        for rid, r in sorted(reports.items()):
            if rid in (0x10, 0x11) or rid == 0:
                continue
            if r["out"] or r["feat"]:
                ln = max(r["out"], r["feat"]) // 8 + 1
                if 6 <= ln <= 33:
                    candidates.append((rid, ln))

        print("\n  [2] Output 리포트로 HID++ ping")
        ok_out = False
        for rid, ln in candidates:
            print("    리포트 ID 0x%02X (%d바이트)" % (rid, ln))
            if try_output(h, rid, ln):
                ok_out = True
                break

        print("\n  [3] Feature 리포트로 HID++ ping")
        ok_feat = False
        if not ok_out:
            for rid, ln in candidates:
                print("    리포트 ID 0x%02X (%d바이트)" % (rid, ln))
                if try_feature(h, rid, ln):
                    ok_feat = True
                    break

        print("\n  판정")
        if ok_out:
            print("    Output 리포트로 HID++ 통신 가능")
        elif ok_feat:
            print("    Feature 리포트로 HID++ 통신 가능")
        else:
            print("    어느 방식으로도 응답이 없습니다.")
    finally:
        h.close()


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pid", type=lambda x: int(x, 0),
                   help="특정 PID만 검사 (예: 0xB02C)")
    p.add_argument("--descriptor-only", action="store_true",
                   help="서술자만 덤프하고 끝낸다")
    args = p.parse_args()

    seen = {}
    for d in hid.enumerate(LOGITECH_VID, 0):
        if args.pid and d["product_id"] != args.pid:
            continue
        if (d.get("usage_page") or 0) < 0xFF00:
            continue
        seen.setdefault(d["path"],
                        (d["product_id"], d.get("product_string") or "?"))

    if not seen:
        sys.exit("벤더 usage page 인터페이스를 못 찾았습니다.")

    print("검사 대상 %d개" % len(seen))
    for path, (pid, prod) in seen.items():
        diagnose(path, pid, prod, args)


if __name__ == "__main__":
    main()
