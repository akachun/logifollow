#!/usr/bin/env python3
"""
hidpp_probe5.py - Logitech HID++ 2.0 (v5)

v4 대비 변경:
  - 슬롯 하드코딩 제거. DeviceNameAndType(0x0005)의 종류 코드로
    마우스/키보드를 런타임에 식별한다
  - map 명령 추가: 모든 기기 × 모든 채널의 호스트 디스크립터를
    표로 뽑고, 같은 호스트를 가리키는 채널끼리 묶어준다
  - switchall 명령 추가: 여러 기기를 한 채널로 동시 전환.
    --strict 를 주면 대상 채널이 같은 호스트인지 먼저 검증한다

사용법:
  uv run --python 3.12 --with hidapi hidpp_probe5.py map
  uv run --python 3.12 --with hidapi hidpp_probe5.py switchall 1 --strict
  uv run --python 3.12 --with hidapi hidpp_probe5.py switch 1 --dev 4

주의:
  전환한 기기는 리시버에서 사라진다. 다시 조작하려면 해당 기기의
  Easy-Switch 버튼/키로 리시버 채널에 되돌려놓아야 한다.
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

FEAT_DEVICE_NAME = 0x0005
FEAT_CHANGE_HOST = 0x1814
FEAT_HOSTS_INFO = 0x1815

IROOT_GET_FEATURE, IROOT_PING = 0x00, 0x01
NAME_GET_COUNT, NAME_GET_NAME, NAME_GET_TYPE = 0x00, 0x01, 0x02
CH_GET_HOST_INFO, CH_SET_CURRENT_HOST = 0x00, 0x01
HI_GET_FEATURE_INFO, HI_GET_HOST_INFO, HI_GET_HOST_DESC = 0x00, 0x01, 0x02

ERR_V2, ERR_V1 = 0x8F, 0xFF
DEVICE_INDICES = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06]

TYPE_KEYBOARD, TYPE_MOUSE = 0, 3
DEVICE_TYPES = {
    0: "키보드", 1: "리모컨", 2: "숫자패드", 3: "마우스", 4: "터치패드",
    5: "트랙볼", 6: "프리젠터", 7: "리시버", 8: "헤드셋", 9: "웹캠",
    13: "독", 17: "컨트롤러",
}
ERR_V1_NAMES = {
    0x04: "CONNECT_FAIL(빈 슬롯)", 0x08: "UNKNOWN_DEVICE",
    0x09: "RESOURCE_ERROR", 0x0A: "REQUEST_UNAVAILABLE",
}


def hexs(b):
    return " ".join("%02X" % x for x in b)


def build(report_id, slot, feat_idx, func, params):
    ln = SHORT_LEN if report_id == REPORT_SHORT else LONG_LEN
    pkt = [report_id, slot, feat_idx, ((func & 0x0F) << 4) | SW_ID]
    pkt += list(params) + [0] * ln
    return bytes(pkt[:ln])


def transact(dev, slot, feat_idx, func, params, report_id=REPORT_SHORT,
             timeout_ms=700, raw=False):
    req = build(report_id, slot, feat_idx, func, params)
    if raw:
        print("      -> %s" % hexs(req))
    dev.write(req)
    want = ((func & 0x0F) << 4) | SW_ID
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        left = int((deadline - time.time()) * 1000) + 1
        rsp = dev.read(64, timeout_ms=left)
        if not rsp or len(rsp) < 4:
            continue
        if raw:
            print("      <- %s" % hexs(rsp))
        if rsp[1] != slot:
            continue
        if rsp[2] in (ERR_V2, ERR_V1):
            if len(rsp) >= 6 and rsp[3] == feat_idx and rsp[4] == want:
                return ("error", rsp[5], bytes(rsp))
            continue
        if rsp[2] == feat_idx and rsp[3] == want:
            return ("ok", None, bytes(rsp))
    return ("timeout", None, None)


def ping(dev, slot, tries=3, raw=False):
    for n in range(tries):
        st, err, rsp = transact(dev, slot, 0x00, IROOT_PING, [0, 0, 0xAA],
                                raw=raw)
        if st == "ok":
            return ("v2", "HID++ %d.%d" % (rsp[4], rsp[5]))
        if st == "error":
            return ("err", ERR_V1_NAMES.get(err, "0x%02X" % err))
        if n < tries - 1:
            time.sleep(0.15)
    return (None, None)


def feature_index(dev, slot, fid, raw=False):
    st, err, rsp = transact(dev, slot, 0x00, IROOT_GET_FEATURE,
                            [(fid >> 8) & 0xFF, fid & 0xFF, 0], raw=raw)
    return rsp[4] if st == "ok" else 0


def device_name(dev, slot, raw=False):
    fi = feature_index(dev, slot, FEAT_DEVICE_NAME, raw=raw)
    if not fi:
        return (None, None)
    st, err, rsp = transact(dev, slot, fi, NAME_GET_COUNT, [0, 0, 0], raw=raw)
    if st != "ok":
        return (None, None)
    count = rsp[4]
    name, pos = "", 0
    while pos < count and pos < 64:
        st, err, rsp = transact(dev, slot, fi, NAME_GET_NAME, [pos, 0, 0],
                                report_id=REPORT_LONG, raw=raw)
        if st != "ok":
            break
        name += bytes(rsp[4:20])[:min(16, count - pos)].decode("ascii", "replace")
        pos += 16
    tcode = None
    st, err, rsp = transact(dev, slot, fi, NAME_GET_TYPE, [0, 0, 0], raw=raw)
    if st == "ok":
        tcode = rsp[4]
    return (name.rstrip("\x00").strip(), tcode)


class Dev:
    """리시버에 붙은 기기 하나."""

    def __init__(self, slot, name, tcode, ch_idx, hi_idx, n_hosts, cur_host):
        self.slot = slot
        self.name = name
        self.tcode = tcode
        self.ch_idx = ch_idx
        self.hi_idx = hi_idx
        self.n_hosts = n_hosts
        self.cur_host = cur_host
        self.desc = {}   # host index -> bytes (호스트 식별자 추정)

    @property
    def type_name(self):
        return DEVICE_TYPES.get(self.tcode, "알수없음(%s)" % self.tcode)

    def __repr__(self):
        return "슬롯%d %s (%s)" % (self.slot, self.name or "?", self.type_name)


def scan(h, raw=False):
    """슬롯 하드코딩 없이 리시버에 붙은 기기를 전부 찾는다."""
    found = []
    for slot in DEVICE_INDICES:
        kind, _ = ping(h, slot, raw=raw)
        if kind != "v2":
            continue
        name, tcode = device_name(h, slot, raw=raw)
        ch = feature_index(h, slot, FEAT_CHANGE_HOST, raw=raw)
        hi = feature_index(h, slot, FEAT_HOSTS_INFO, raw=raw)
        nb = cur = None
        if ch:
            st, err, rsp = transact(h, slot, ch, CH_GET_HOST_INFO, [0, 0, 0],
                                    raw=raw)
            if st == "ok":
                nb, cur = rsp[4], rsp[5]
        found.append(Dev(slot, name, tcode, ch, hi, nb, cur))
    return found


def read_descriptors(h, d, raw=False):
    """각 채널의 호스트 디스크립터를 읽는다.

    응답 payload[0] 은 hostIndex 에코라서 비교 키에서 제외한다.
    나머지 바이트가 호스트를 식별하는 값으로 보인다.
    """
    if not d.hi_idx or not d.n_hosts:
        return
    for host in range(d.n_hosts):
        st, err, rsp = transact(h, d.slot, d.hi_idx, HI_GET_HOST_DESC,
                                [host, 0, 0], report_id=REPORT_LONG, raw=raw)
        if st != "ok":
            continue
        payload = bytes(rsp[4:])
        key = payload[1:9].rstrip(b"\x00")
        d.desc[host] = key


def open_vendor():
    seen = {}
    for x in hid.enumerate(LOGITECH_VID, 0):
        if (x.get("usage_page") or 0) >= 0xFF00:
            seen.setdefault(x["path"], x)
    for path, meta in seen.items():
        try:
            h = hid.device()
            h.open_path(path)
            h.set_nonblocking(0)
            return h, meta
        except Exception as e:
            print("열기 실패: %s" % e)
    return None, None


def cmd_map(args):
    h, meta = open_vendor()
    if not h:
        sys.exit("벤더 인터페이스를 못 찾았습니다.")
    try:
        devs = scan(h, raw=args.raw)
        if not devs:
            sys.exit("응답하는 기기가 없습니다. 기기를 깨우고 다시 시도하세요.")

        print("리시버 PID=0x%04X\n" % meta["product_id"])
        for d in devs:
            read_descriptors(h, d, raw=args.raw)
            cur = "채널 %d" % (d.cur_host + 1) if d.cur_host is not None else "?"
            print("  %s  현재 %s  ChangeHost=0x%02X HostsInfo=0x%02X"
                  % (d, cur, d.ch_idx, d.hi_idx))
        print()

        # 채널별 디스크립터 표
        maxh = max((d.n_hosts or 0) for d in devs)
        width = max(len(str(d)) for d in devs) + 2
        print("호스트 디스크립터 (같으면 같은 컴퓨터로 추정)\n")
        header = " " * width + "".join("채널%-20d" % (c + 1) for c in range(maxh))
        print(header)
        for d in devs:
            row = str(d).ljust(width)
            for c in range(maxh):
                k = d.desc.get(c)
                row += (hexs(k) if k else "-").ljust(25)
            print(row)
        print()

        # 같은 디스크립터끼리 묶기
        groups = {}
        for d in devs:
            for c, k in d.desc.items():
                groups.setdefault(bytes(k), []).append((d, c))
        print("묶음:")
        for k, members in groups.items():
            tag = ", ".join("%s ch%d" % (m[0].name or "?", m[1] + 1)
                            for m in members)
            print("  %s\n     %s" % (hexs(k), tag))
        print()

        # 모든 기기가 공유하는 채널 찾기
        common = []
        for c in range(maxh):
            keys = [d.desc.get(c) for d in devs if d.desc.get(c) is not None]
            if len(keys) == len(devs) and len(set(map(bytes, keys))) == 1:
                common.append(c + 1)
        if common:
            print("모든 기기가 같은 호스트를 가리키는 채널: %s"
                  % ", ".join(str(c) for c in common))
            print("-> 이 채널들은 switchall --strict 로 안전하게 동시 전환됩니다.")
        else:
            print("모든 기기가 일치하는 채널이 없습니다.")
            print("-> 기기별 채널 배치가 서로 다르거나, 디스크립터 해석이")
            print("   제 추정과 다를 수 있습니다. 위 원시 바이트를 보세요.")
    finally:
        h.close()


def confirm(prompt):
    for _ in range(3):
        try:
            a = input(prompt).strip().lower()
        except EOFError:
            return False
        if a in ("y", "yes"):
            return True
        if a in ("n", "no", ""):
            return False
        print("y 또는 n 으로 답해주세요.")
    return False


def cmd_switchall(args):
    target0 = args.channel - 1
    h, meta = open_vendor()
    if not h:
        sys.exit("벤더 인터페이스를 못 찾았습니다.")
    try:
        devs = scan(h, raw=args.raw)
        if not devs:
            sys.exit("응답하는 기기가 없습니다.")

        devs = [d for d in devs if d.ch_idx]
        for d in devs:
            if d.n_hosts and target0 >= d.n_hosts:
                sys.exit("%s 에는 채널 %d 가 없습니다 (최대 %d)."
                         % (d, args.channel, d.n_hosts))

        if args.strict:
            for d in devs:
                read_descriptors(h, d, raw=args.raw)
            keys = [d.desc.get(target0) for d in devs]
            if any(k is None for k in keys):
                sys.exit("디스크립터를 못 읽은 기기가 있습니다. --strict 중단.")
            if len(set(map(bytes, keys))) != 1:
                print("채널 %d 가 기기마다 다른 호스트를 가리킵니다:"
                      % args.channel)
                for d in devs:
                    print("  %s -> %s" % (d, hexs(d.desc[target0])))
                sys.exit("--strict 이므로 중단합니다.")
            print("확인: 모든 기기의 채널 %d 가 동일한 호스트입니다.\n"
                  % args.channel)

        print("전환 대상:")
        for d in devs:
            cur = "채널 %d" % (d.cur_host + 1) if d.cur_host is not None else "?"
            print("  %s  (현재 %s)" % (d, cur))
        print()

        if not args.yes and not confirm("채널 %d 로 전부 보낼까요? [y/N] "
                                        % args.channel):
            print("취소했습니다.")
            return

        # 마우스를 마지막에 보낸다. 먼저 보내면 커서를 잃어서
        # 뭔가 잘못됐을 때 대응이 어려워진다.
        devs.sort(key=lambda d: 1 if d.tcode == TYPE_MOUSE else 0)

        for d in devs:
            if d.cur_host == target0:
                print("  %s : 이미 채널 %d" % (d, args.channel))
                continue
            st, err, rsp = transact(h, d.slot, d.ch_idx, CH_SET_CURRENT_HOST,
                                    [target0, 0, 0], timeout_ms=400,
                                    raw=args.raw)
            if st == "ok":
                print("  %s : 수락됨" % d)
            elif st == "timeout":
                print("  %s : 응답 없음 (넘어갔다면 정상)" % d)
            else:
                print("  %s : 에러 0x%02X" % (d, err))
            time.sleep(0.25)
    finally:
        h.close()


def cmd_switch(args):
    h, meta = open_vendor()
    if not h:
        sys.exit("벤더 인터페이스를 못 찾았습니다.")
    try:
        devs = scan(h, raw=args.raw)
        if args.dev is not None:
            devs = [d for d in devs if d.slot == args.dev]
        if not devs:
            sys.exit("대상 기기가 없습니다. map 을 먼저 실행하세요.")
        d = devs[0]
        print("대상 : %s (현재 채널 %s)"
              % (d, d.cur_host + 1 if d.cur_host is not None else "?"))
        if d.cur_host == args.channel - 1:
            print("이미 그 채널에 있습니다.")
            return
        if not args.yes and not confirm("채널 %d 로 보낼까요? [y/N] "
                                        % args.channel):
            print("취소했습니다.")
            return
        st, err, rsp = transact(h, d.slot, d.ch_idx, CH_SET_CURRENT_HOST,
                                [args.channel - 1, 0, 0], timeout_ms=400,
                                raw=args.raw)
        print({"ok": "수락됨", "timeout": "응답 없음 (넘어갔다면 정상)"}
              .get(st, "에러 %s" % err))
    finally:
        h.close()


def parse_args(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--raw", action="store_true")
    pre.add_argument("--dev", type=lambda x: int(x, 0), default=None)
    pre.add_argument("--yes", action="store_true")
    pre.add_argument("--strict", action="store_true")
    opts, rest = pre.parse_known_args(argv)

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="공통 옵션: --dev N  --raw  --yes  --strict")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("map", help="기기 × 채널 호스트 디스크립터 표")
    sa = sub.add_parser("switchall", help="모든 기기를 한 채널로")
    sa.add_argument("channel", type=int)
    sw = sub.add_parser("switch", help="한 기기만 전환")
    sw.add_argument("channel", type=int)

    args = p.parse_args(rest)
    for k, v in vars(opts).items():
        setattr(args, k, v)
    return args


def main():
    args = parse_args()
    {"map": cmd_map, "switchall": cmd_switchall, "switch": cmd_switch}[args.cmd](args)


if __name__ == "__main__":
    main()
