#!/usr/bin/env python3
"""
hidpp_probe3.py - Logitech HID++ 2.0 탐색 / ChangeHost (v3)

v2 대비 변경:
  - 옵션 순서 버그 수정. --dev/--raw/--path 를 서브커맨드 앞뒤 아무데나
    써도 인식된다 (2단계 파싱)
  - hostinfo 명령 추가: HostsInfo(0x1815)로 각 호스트 슬롯의 상태를 조회
  - switch 가 대상 채널 상태를 먼저 확인하고 확인을 받는다 (--yes 로 생략)

사용법:
  uv run --python 3.12 --with hidapi hidpp_probe3.py info
  uv run --python 3.12 --with hidapi hidpp_probe3.py hostinfo --dev 4
  uv run --python 3.12 --with hidapi hidpp_probe3.py switch 1 --dev 4
"""

import argparse
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("hidapi가 없습니다.  uv run --with hidapi ... 로 실행하세요")

LOGITECH_VID = 0x046D

REPORT_SHORT = 0x10
REPORT_LONG = 0x11
SHORT_LEN = 7
LONG_LEN = 20
SW_ID = 0x05

FEAT_DEVICE_NAME = 0x0005
FEAT_CHANGE_HOST = 0x1814
FEAT_HOSTS_INFO = 0x1815

IROOT_GET_FEATURE = 0x00
IROOT_PING = 0x01

NAME_GET_COUNT = 0x00
NAME_GET_NAME = 0x01
NAME_GET_TYPE = 0x02

CH_GET_HOST_INFO = 0x00
CH_SET_CURRENT_HOST = 0x01

# 0x1815 는 함수 0~2 만 건드린다. 3번 이후는 setter(이름 변경/슬롯 삭제)일
# 가능성이 있어서 의도적으로 호출하지 않는다.
HI_GET_FEATURE_INFO = 0x00
HI_GET_HOST_INFO = 0x01
HI_GET_HOST_DESC = 0x02

ERR_V2 = 0x8F
ERR_V1 = 0xFF

DEVICE_INDICES = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06]

DEVICE_TYPES = {
    0: "키보드", 1: "리모컨", 2: "숫자패드", 3: "마우스", 4: "터치패드",
    5: "트랙볼", 6: "프리젠터", 7: "리시버", 8: "헤드셋", 9: "웹캠",
    10: "스티어링휠", 11: "조이스틱", 12: "게임패드", 13: "독",
    14: "스피커폰", 15: "마이크", 16: "조명", 17: "컨트롤러",
    18: "페달", 19: "어댑터",
}

ERR_V1_NAMES = {
    0x01: "INVALID_SUBID", 0x02: "INVALID_ADDRESS", 0x03: "INVALID_VALUE",
    0x04: "CONNECT_FAIL(빈 슬롯)", 0x05: "TOO_MANY_DEVICES",
    0x06: "ALREADY_EXISTS", 0x07: "BUSY", 0x08: "UNKNOWN_DEVICE",
    0x09: "RESOURCE_ERROR", 0x0A: "REQUEST_UNAVAILABLE",
    0x0B: "INVALID_PARAM_VALUE", 0x0C: "WRONG_PIN_CODE",
}

BUS_TYPES = {0: "정보없음", 1: "eQUAD", 2: "USB", 3: "BT", 4: "BLE"}


def hexs(b):
    return " ".join("%02X" % x for x in b)


def build(report_id, dev_idx, feat_idx, func, params):
    length = SHORT_LEN if report_id == REPORT_SHORT else LONG_LEN
    pkt = [report_id, dev_idx, feat_idx, ((func & 0x0F) << 4) | SW_ID]
    pkt += list(params)
    pkt += [0] * (length - len(pkt))
    return bytes(pkt[:length])


def transact(dev, dev_idx, feat_idx, func, params,
             report_id=REPORT_SHORT, timeout_ms=700, raw=False):
    req = build(report_id, dev_idx, feat_idx, func, params)
    if raw:
        print("      -> %s" % hexs(req))
    dev.write(req)

    want = ((func & 0x0F) << 4) | SW_ID
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        left = int((deadline - time.time()) * 1000) + 1
        rsp = dev.read(64, timeout_ms=left)
        if not rsp:
            continue
        if raw:
            print("      <- %s" % hexs(rsp))
        if len(rsp) < 4 or rsp[1] != dev_idx:
            continue
        if rsp[2] in (ERR_V2, ERR_V1):
            if len(rsp) >= 6 and rsp[3] == feat_idx and rsp[4] == want:
                return ("error", rsp[5], bytes(rsp))
            continue
        if rsp[2] == feat_idx and rsp[3] == want:
            return ("ok", None, bytes(rsp))
    return ("timeout", None, None)


def get_feature_index(dev, dev_idx, feature_id, raw=False):
    st, err, rsp = transact(dev, dev_idx, 0x00, IROOT_GET_FEATURE,
                            [(feature_id >> 8) & 0xFF, feature_id & 0xFF, 0],
                            raw=raw)
    return rsp[4] if st == "ok" else 0


def ping(dev, dev_idx, raw=False):
    st, err, rsp = transact(dev, dev_idx, 0x00, IROOT_PING, [0, 0, 0xAA],
                            raw=raw)
    if st == "ok":
        return ("v2", "HID++ %d.%d" % (rsp[4], rsp[5]))
    if st == "error":
        return ("err", ERR_V1_NAMES.get(err, "0x%02X" % err))
    return (None, None)


def read_device_name(dev, dev_idx, raw=False):
    fi = get_feature_index(dev, dev_idx, FEAT_DEVICE_NAME, raw=raw)
    if not fi:
        return (None, None)
    st, err, rsp = transact(dev, dev_idx, fi, NAME_GET_COUNT, [0, 0, 0], raw=raw)
    if st != "ok":
        return (None, None)
    count = rsp[4]

    name, pos = "", 0
    while pos < count and pos < 64:
        st, err, rsp = transact(dev, dev_idx, fi, NAME_GET_NAME, [pos, 0, 0],
                                report_id=REPORT_LONG, raw=raw)
        if st != "ok":
            break
        name += bytes(rsp[4:20])[:min(16, count - pos)].decode("ascii", "replace")
        pos += 16

    dtype = None
    st, err, rsp = transact(dev, dev_idx, fi, NAME_GET_TYPE, [0, 0, 0], raw=raw)
    if st == "ok":
        dtype = DEVICE_TYPES.get(rsp[4], "알수없음(0x%02X)" % rsp[4])
    return (name.rstrip("\x00").strip(), dtype)


def open_vendor_interfaces():
    seen = {}
    for d in hid.enumerate(LOGITECH_VID, 0):
        if (d.get("usage_page") or 0) >= 0xFF00:
            seen.setdefault(d["path"], d)
    out = []
    for path, meta in seen.items():
        try:
            h = hid.device()
            h.open_path(path)
            h.set_nonblocking(0)
            out.append((h, path, meta))
        except Exception as e:
            print("열기 실패 %s: %s" % (path.decode("utf-8", "replace"), e))
    return out


def cmd_info(args):
    ifaces = open_vendor_interfaces()
    if not ifaces:
        sys.exit("벤더 인터페이스를 못 찾았습니다. 리시버가 꽂혀 있나요?")
    for h, path, meta in ifaces:
        print("\n=== PID=0x%04X  %s" % (meta["product_id"],
                                        meta.get("product_string") or "?"))
        print("    %s\n" % path.decode("utf-8", "replace"))
        try:
            for di in DEVICE_INDICES:
                kind, ver = ping(h, di, raw=args.raw)
                if kind is None:
                    print("  슬롯 %d : 응답 없음" % di)
                    continue
                if kind == "err":
                    print("  슬롯 %d : %s" % (di, ver))
                    continue
                name, dtype = read_device_name(h, di, raw=args.raw)
                ch = get_feature_index(h, di, FEAT_CHANGE_HOST, raw=args.raw)
                hi = get_feature_index(h, di, FEAT_HOSTS_INFO, raw=args.raw)
                print("  슬롯 %d : %s" % (di, ver))
                print("     이름       : %s" % (name or "?"))
                print("     종류       : %s" % (dtype or "?"))
                print("     ChangeHost : %s" % (
                    "featidx=0x%02X" % ch if ch else "미지원"))
                print("     HostsInfo  : %s" % (
                    "featidx=0x%02X" % hi if hi else "미지원"))
                if ch:
                    st, err, rsp = transact(h, di, ch, CH_GET_HOST_INFO,
                                            [0, 0, 0], raw=args.raw)
                    if st == "ok":
                        print("     호스트     : %d개, 현재 %d (채널 %d)"
                              % (rsp[4], rsp[5], rsp[5] + 1))
                print()
        finally:
            h.close()
    print("=" * 52)
    print("다음:  hostinfo --dev <슬롯>  으로 채널별 상태를 확인하세요.")


def pick_device(args):
    ifaces = open_vendor_interfaces()
    if not ifaces:
        sys.exit("벤더 인터페이스를 못 찾았습니다.")
    if args.path:
        keep = [x for x in ifaces
                if x[1].decode("utf-8", "replace") == args.path]
        for h, p, m in ifaces:
            if (h, p, m) not in keep:
                h.close()
        ifaces = keep
        if not ifaces:
            sys.exit("지정한 경로를 못 찾았습니다.")

    for h, path, meta in ifaces:
        cands = [args.dev] if args.dev is not None else DEVICE_INDICES
        for di in cands:
            if ping(h, di)[0] != "v2":
                continue
            idx = get_feature_index(h, di, FEAT_CHANGE_HOST)
            if idx:
                for oh, _, _ in ifaces:
                    if oh is not h:
                        oh.close()
                return h, di, idx
        h.close()
    sys.exit("ChangeHost 대상을 못 찾았습니다. info 를 먼저 실행하세요.")


def read_host_slots(h, di, raw=False):
    """HostsInfo(0x1815)로 슬롯별 상태를 읽는다.

    응답 바이트 해석은 확정이 아니라 추정이다. 원시 바이트를 같이
    출력하니 실제 값과 대조해서 바로잡으면 된다.
    """
    hi = get_feature_index(h, di, FEAT_HOSTS_INFO, raw=raw)
    if not hi:
        return None
    out = {"featidx": hi, "slots": []}

    st, err, rsp = transact(h, di, hi, HI_GET_FEATURE_INFO, [0, 0, 0], raw=raw)
    out["feature_info"] = bytes(rsp) if st == "ok" else None

    for host in range(0, 4):
        st, err, rsp = transact(h, di, hi, HI_GET_HOST_INFO, [host, 0, 0],
                                raw=raw)
        if st != "ok":
            break
        out["slots"].append((host, bytes(rsp)))
    return out


def cmd_hostinfo(args):
    h, di, ch_idx = pick_device(args)
    try:
        name, dtype = read_device_name(h, di)
        print("대상 : 슬롯 %d  %s (%s)" % (di, name or "?", dtype or "?"))

        st, err, rsp = transact(h, di, ch_idx, CH_GET_HOST_INFO, [0, 0, 0],
                                raw=args.raw)
        if st == "ok":
            print("ChangeHost : 호스트 %d개, 현재 채널 %d"
                  % (rsp[4], rsp[5] + 1))

        info = read_host_slots(h, di, raw=args.raw)
        if not info:
            print("\nHostsInfo(0x1815) 미지원입니다.")
            return

        print("\nHostsInfo featidx=0x%02X" % info["featidx"])
        if info["feature_info"]:
            print("  getFeatureInfo : %s" % hexs(info["feature_info"]))
        print()
        for host, rsp in info["slots"]:
            payload = rsp[4:]
            # 추정 레이아웃: [hostIndex, status, busType, numPages,
            #                 nameLen, nameMaxLen, ...]
            status = payload[1] if len(payload) > 1 else None
            bus = payload[2] if len(payload) > 2 else None
            guess = "페어링됨" if status else "빈 슬롯"
            print("  채널 %d (host %d)" % (host + 1, host))
            print("     원시 : %s" % hexs(rsp))
            print("     추정 : status=%s(%s) bus=%s(%s)"
                  % (status, guess, bus, BUS_TYPES.get(bus, "?")))
            print()

        print("주의: 위 '추정'은 확정된 해석이 아닙니다. 원시 바이트를")
        print("      실제 상황(어느 채널에 뭐가 붙어 있는지)과 대조해보세요.")
    finally:
        h.close()


def cmd_switch(args):
    target0 = args.channel - 1
    if target0 < 0:
        sys.exit("채널은 1부터 시작합니다.")
    h, di, idx = pick_device(args)
    try:
        name, dtype = read_device_name(h, di)
        print("대상 : 슬롯 %d  %s (%s)" % (di, name or "?", dtype or "?"))

        st, err, rsp = transact(h, di, idx, CH_GET_HOST_INFO, [0, 0, 0])
        if st == "ok":
            nb, cur = rsp[4], rsp[5]
            print("현재 : 채널 %d / 전체 %d개" % (cur + 1, nb))
            if target0 >= nb:
                sys.exit("채널 %d 는 존재하지 않습니다 (최대 %d)."
                         % (args.channel, nb))
            if target0 == cur:
                print("이미 채널 %d 에 있습니다." % args.channel)
                return

        if not args.yes:
            print("\n채널 %d 로 보냅니다. 그 채널이 비어 있으면 마우스가"
                  % args.channel)
            print("페어링 대기 상태로 빠집니다 (바닥 버튼으로 복구 가능).")
            try:
                if input("진행할까요? [y/N] ").strip().lower() != "y":
                    print("취소했습니다.")
                    return
            except EOFError:
                print("취소했습니다.")
                return

        st, err, rsp = transact(h, di, idx, CH_SET_CURRENT_HOST,
                                [target0, 0, 0], timeout_ms=400, raw=args.raw)
        if st == "ok":
            print("수락됨: %s" % hexs(rsp))
        elif st == "timeout":
            print("응답 없음 — 기기가 이미 넘어갔다면 정상입니다.")
        else:
            print("에러 0x%02X — %s" % (err, hexs(rsp)))
    finally:
        h.close()


def parse_args(argv=None):
    """옵션이 서브커맨드 앞이든 뒤든 먹히도록 2단계로 파싱한다."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--raw", action="store_true")
    pre.add_argument("--path")
    pre.add_argument("--dev", type=lambda x: int(x, 0), default=None)
    pre.add_argument("--yes", action="store_true")
    opts, rest = pre.parse_known_args(argv)

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="공통 옵션: --dev N  --path PATH  --raw  --yes")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="슬롯별 기기 이름/종류/기능")
    sub.add_parser("hostinfo", help="채널별 페어링 상태 (0x1815)")
    sw = sub.add_parser("switch", help="지정 채널로 전환")
    sw.add_argument("channel", type=int, help="채널 번호 (1-based)")

    args = p.parse_args(rest)
    for k, v in vars(opts).items():
        setattr(args, k, v)
    return args


def main():
    args = parse_args()
    {"info": cmd_info, "hostinfo": cmd_hostinfo,
     "switch": cmd_switch}[args.cmd](args)


if __name__ == "__main__":
    main()
