#!/usr/bin/env python3
"""
hidpp_direct.py - 블루투스 직결 기기에 HID++ 가 통하는지 확인

목적:
  리시버 없이, 블루투스로 맥에 직접 붙은 로지텍 기기에도
  HID++ 명령을 보낼 수 있는지 확인한다. 된다면 "돌아오는 방향"
  전환도 직접 구현할 수 있다.

  리시버 경유 기기는 device index 1~6 을 쓰지만,
  직결 기기는 device index 0xFF 를 쓴다.

실행 위치:
  마우스와 키보드가 블루투스로 붙어 있는 맥. (리시버가 꽂힌
  맥이 아니라, 채널1 쪽 맥)

사용법:
  uv run --python 3.12 --with hidapi hidpp_direct.py probe
  uv run --python 3.12 --with hidapi hidpp_direct.py switch 3

주의:
  switch 는 실제로 기기를 옮긴다. 맥북 내장 키보드/트랙패드가
  있는 상태에서 실행할 것.
"""

import argparse
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("hidapi가 없습니다.  uv run --with hidapi ... 로 실행하세요")

LOGITECH_VID = 0x046D
REPORT_SHORT, REPORT_LONG = 0x10, 0x11
SHORT_LEN, LONG_LEN = 7, 20
SW_ID = 0x05

DIRECT = 0xFF                      # 직결 기기
VIA_RECEIVER = list(range(1, 7))   # 리시버 슬롯

FEAT_DEVICE_NAME = 0x0005
FEAT_CHANGE_HOST = 0x1814
FEAT_HOSTS_INFO = 0x1815
IROOT_GET_FEATURE, IROOT_PING = 0x00, 0x01
NAME_GET_COUNT, NAME_GET_NAME, NAME_GET_TYPE = 0x00, 0x01, 0x02
CH_GET_HOST_INFO, CH_SET_CURRENT_HOST = 0x00, 0x01
HI_GET_HOST_DESC = 0x02
ERR_V2, ERR_V1 = 0x8F, 0xFF

DEVICE_TYPES = {0: "키보드", 1: "리모컨", 2: "숫자패드", 3: "마우스",
                4: "터치패드", 5: "트랙볼", 6: "프리젠터", 7: "리시버"}

DISCOVER_MS = 2500
NORMAL_MS = 700


def hexs(b):
    return " ".join("%02X" % x for x in b)


def build(rid, idx, fidx, func, params):
    ln = SHORT_LEN if rid == REPORT_SHORT else LONG_LEN
    return bytes(([rid, idx, fidx,
                   ((func & 0x0F) << 4) | SW_ID] + list(params) + [0] * ln)[:ln])


def transact(h, idx, fidx, func, params, rid=REPORT_SHORT, timeout_ms=NORMAL_MS,
             raw=False):
    req = build(rid, idx, fidx, func, params)
    if raw:
        print("        -> %s" % hexs(req))
    try:
        h.write(req)
    except Exception as e:
        if raw:
            print("        write 실패: %s" % e)
        return None, "write_fail"
    want = ((func & 0x0F) << 4) | SW_ID
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        r = h.read(64, timeout_ms=int((deadline - time.time()) * 1000) + 1)
        if not r or len(r) < 4:
            continue
        if raw:
            print("        <- %s" % hexs(r))
        if r[1] != idx:
            continue
        if r[2] in (ERR_V2, ERR_V1):
            return None, "err_0x%02X" % (r[5] if len(r) > 5 else 0)
        if r[2] == fidx and r[3] == want:
            return bytes(r), None
    return None, "timeout"


def feature_index(h, idx, fid, raw=False):
    r, _ = transact(h, idx, 0x00, IROOT_GET_FEATURE,
                    [(fid >> 8) & 0xFF, fid & 0xFF, 0], raw=raw)
    return r[4] if r else 0


def read_name(h, idx, raw=False):
    ni = feature_index(h, idx, FEAT_DEVICE_NAME, raw=raw)
    if not ni:
        return None, None
    c, _ = transact(h, idx, ni, NAME_GET_COUNT, [0, 0, 0], raw=raw)
    if not c:
        return None, None
    count, txt, pos = c[4], "", 0
    while pos < count and pos < 64:
        r, _ = transact(h, idx, ni, NAME_GET_NAME, [pos, 0, 0],
                        rid=REPORT_LONG, raw=raw)
        if not r:
            break
        txt += bytes(r[4:20])[:min(16, count - pos)].decode("ascii", "replace")
        pos += 16
    t, _ = transact(h, idx, ni, NAME_GET_TYPE, [0, 0, 0], raw=raw)
    return txt.strip(), (t[4] if t else None)


def all_logitech():
    """로지텍 HID 인터페이스 전부. 벤더 usage page 만 후보로 표시."""
    everything = hid.enumerate(LOGITECH_VID, 0)
    by_path = {}
    for d in everything:
        by_path.setdefault(d["path"], []).append(d)
    return everything, by_path


def cmd_probe(args):
    everything, by_path = all_logitech()
    if not everything:
        sys.exit("로지텍 HID 기기가 안 보입니다.\n"
                 "블루투스로 페어링돼 있고 연결된 상태인지 확인하세요.")

    print("로지텍 HID 인터페이스 %d개, 고유 경로 %d개\n"
          % (len(everything), len(by_path)))

    candidates = []
    for path, entries in by_path.items():
        pages = sorted({e.get("usage_page") or 0 for e in entries})
        vendor = [p for p in pages if p >= 0xFF00]
        pid = entries[0]["product_id"]
        prod = entries[0].get("product_string") or "?"
        mark = "*" if vendor else " "
        print("%s PID=0x%04X  %s" % (mark, pid, prod))
        print("    usage_page: %s" % ", ".join("0x%04X" % p for p in pages))
        print("    path: %s" % path.decode("utf-8", "replace"))
        if vendor:
            candidates.append((path, pid, prod))
        print()

    if not candidates:
        print("=" * 56)
        print("벤더 usage page(0xFF00+) 인터페이스가 하나도 없습니다.")
        print("즉 macOS 가 이 기기들의 HID++ 통로를 노출하지 않습니다.")
        print("-> 블루투스 직결로는 전환 명령을 보낼 수 없습니다.")
        return

    print("=" * 56)
    print("벤더 인터페이스 %d개 발견. HID++ 응답을 확인합니다.\n" % len(candidates))

    found = []
    for path, pid, prod in candidates:
        print("--- PID=0x%04X %s" % (pid, prod))
        try:
            h = hid.device()
            h.open_path(path)
            h.set_nonblocking(0)
        except Exception as e:
            print("    열기 실패: %s\n" % e)
            continue
        try:
            for idx in [DIRECT] + VIA_RECEIVER:
                tag = "직결(0xFF)" if idx == DIRECT else "슬롯%d" % idx
                r, err = transact(h, idx, 0x00, IROOT_PING, [0, 0, 0xAA],
                                  timeout_ms=DISCOVER_MS if idx == DIRECT
                                  else NORMAL_MS, raw=args.raw)
                if not r:
                    print("    %-12s %s" % (tag, err))
                    continue
                ver = "HID++ %d.%d" % (r[4], r[5])
                name, tcode = read_name(h, idx, raw=args.raw)
                ch = feature_index(h, idx, FEAT_CHANGE_HOST, raw=args.raw)
                hi = feature_index(h, idx, FEAT_HOSTS_INFO, raw=args.raw)
                print("    %-12s %s  %s (%s)"
                      % (tag, ver, name or "?", DEVICE_TYPES.get(tcode, "?")))
                print("                 ChangeHost=%s  HostsInfo=%s"
                      % ("0x%02X" % ch if ch else "없음",
                         "0x%02X" % hi if hi else "없음"))
                if ch:
                    hr, _ = transact(h, idx, ch, CH_GET_HOST_INFO, [0, 0, 0],
                                     raw=args.raw)
                    if hr:
                        print("                 호스트 %d개, 현재 채널 %d"
                              % (hr[4], hr[5] + 1))
                    found.append((path, idx, ch, name, hr[4] if hr else None))
                if hi:
                    for host in range(3):
                        dr, _ = transact(h, idx, hi, HI_GET_HOST_DESC,
                                         [host, 0, 0], rid=REPORT_LONG,
                                         raw=args.raw)
                        if dr:
                            print("                 채널%d %s"
                                  % (host + 1, hexs(bytes(dr[4:])[1:9])))
        finally:
            h.close()
        print()

    print("=" * 56)
    if found:
        print("블루투스 직결로도 HID++ 가 통합니다. 전환 가능:")
        for path, idx, ch, name, nb in found:
            print("  %s  (device index 0x%02X, ChangeHost 0x%02X)"
                  % (name or "?", idx, ch))
        print("\n다음:  hidpp_direct.py switch <채널>")
    else:
        print("벤더 인터페이스는 열렸지만 HID++ 응답이 없습니다.")
        print("--raw 로 원시 패킷을 보면 어디서 막히는지 알 수 있습니다.")


def cmd_switch(args):
    target0 = args.channel - 1
    everything, by_path = all_logitech()
    targets = []
    for path, entries in by_path.items():
        if not any((e.get("usage_page") or 0) >= 0xFF00 for e in entries):
            continue
        try:
            h = hid.device()
            h.open_path(path)
            h.set_nonblocking(0)
        except Exception:
            continue
        for idx in [DIRECT] + VIA_RECEIVER:
            r, _ = transact(h, idx, 0x00, IROOT_PING, [0, 0, 0xAA],
                            timeout_ms=DISCOVER_MS if idx == DIRECT
                            else NORMAL_MS)
            if not r:
                continue
            ch = feature_index(h, idx, FEAT_CHANGE_HOST)
            if not ch:
                continue
            name, tcode = read_name(h, idx)
            targets.append((h, idx, ch, name, tcode))
        if not any(t[0] is h for t in targets):
            h.close()

    if not targets:
        sys.exit("전환 가능한 기기를 못 찾았습니다. probe 를 먼저 실행하세요.")

    if args.type == "mouse":
        targets = [t for t in targets if t[4] == 3]
    elif args.type == "keyboard":
        targets = [t for t in targets if t[4] == 0]
    if not targets:
        sys.exit("해당 종류의 기기가 없습니다.")

    for h, idx, ch, name, tcode in targets:
        print("%s (%s) -> 채널 %d"
              % (name or "?", DEVICE_TYPES.get(tcode, "?"), args.channel))
        if args.dry_run:
            print("   dry-run: %s"
                  % hexs(build(REPORT_SHORT, idx, ch, CH_SET_CURRENT_HOST,
                               [target0, 0, 0])))
            continue
        r, err = transact(h, idx, ch, CH_SET_CURRENT_HOST, [target0, 0, 0],
                          timeout_ms=400, raw=args.raw)
        print("   %s" % ("수락됨" if r else
                         "응답 없음 (넘어갔다면 정상)" if err == "timeout"
                         else err))
        time.sleep(0.25)

    for h, _, _, _, _ in targets:
        try:
            h.close()
        except Exception:
            pass


def parse_args(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--raw", action="store_true")
    pre.add_argument("--dry-run", action="store_true")
    pre.add_argument("--type", choices=["mouse", "keyboard", "all"],
                     default="all")
    opts, rest = pre.parse_known_args(argv)
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="직결 HID++ 가능 여부 확인")
    sw = sub.add_parser("switch", help="채널 전환")
    sw.add_argument("channel", type=int)
    args = p.parse_args(rest)
    for k, v in vars(opts).items():
        setattr(args, k, v)
    return args


def main():
    args = parse_args()
    {"probe": cmd_probe, "switch": cmd_switch}[args.cmd](args)


if __name__ == "__main__":
    main()
