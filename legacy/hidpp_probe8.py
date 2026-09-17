#!/usr/bin/env python3
"""
hidpp_probe8.py - Logitech HID++ (v8) : 키보드 추종

확정된 사실:
  ChangeHost(0x1814) 는 기기가 채널을 옮길 때 이벤트를 쏜다.
  payload = [출발 host index, 도착 host index]
    F1 -> 02 00   (host2 -> host0 = 채널1)
    F2 -> 02 01   (host2 -> host1 = 채널2)
  이벤트는 링크 끊김 알림보다 40~60ms 먼저 도착한다.

알려진 문제:
  이벤트가 간헐적으로 유실된다. Logi Options+ 가 같은 인터페이스를
  함께 읽으면서 리포트를 먼저 가져가는 것으로 보인다.
  follow 를 쓸 때는 Options+ 를 종료할 것.

사용법:
  uv run --python 3.12 --with hidapi hidpp_probe8.py map
  uv run --python 3.12 --with hidapi hidpp_probe8.py follow --dry-run
  uv run --python 3.12 --with hidapi hidpp_probe8.py follow
  uv run --python 3.12 --with hidapi hidpp_probe8.py follow --log run.txt
"""

import argparse
import sys
import time
import datetime

try:
    import hid
except ImportError:
    sys.exit("hidapi가 없습니다.  uv run --with hidapi ... 로 실행하세요")

LOGITECH_VID = 0x046D
REPORT_SHORT, REPORT_LONG = 0x10, 0x11
SHORT_LEN, LONG_LEN = 7, 20
SW_ID = 0x05
RECEIVER = 0xFF

SUB_SET_REGISTER, SUB_GET_REGISTER = 0x80, 0x81
REG_NOTIFICATIONS = 0x00
FLAG_WIRELESS, FLAG_SW_PRESENT = 0x000100, 0x000800

NOTIF_NAMES = {0x40: "DISCONNECTION", 0x41: "CONNECTION",
               0x49: "DISCOVERY", 0x4A: "PAIRING_STATUS"}

FEAT_FEATURE_SET = 0x0001
FEAT_DEVICE_NAME = 0x0005
FEAT_CHANGE_HOST = 0x1814
FEAT_HOSTS_INFO = 0x1815

IROOT_GET_FEATURE, IROOT_PING = 0x00, 0x01
FSET_GET_COUNT, FSET_GET_FEATURE_ID = 0x00, 0x01
NAME_GET_COUNT, NAME_GET_NAME, NAME_GET_TYPE = 0x00, 0x01, 0x02
CH_GET_HOST_INFO, CH_SET_CURRENT_HOST = 0x00, 0x01
HI_GET_HOST_DESC = 0x02
ERR_V2, ERR_V1 = 0x8F, 0xFF

TYPE_KEYBOARD, TYPE_MOUSE = 0, 3
DEVICE_TYPES = {0: "키보드", 1: "리모컨", 2: "숫자패드", 3: "마우스",
                4: "터치패드", 5: "트랙볼", 6: "프리젠터", 7: "리시버"}
NOTIF_KIND = {1: "키보드", 2: "마우스", 3: "숫자패드", 4: "프리젠터",
              8: "트랙볼", 9: "터치패드"}

FEATURE_NAMES = {
    0x0000: "IRoot", 0x0001: "IFeatureSet", 0x0003: "DeviceInformation",
    0x0005: "DeviceNameAndType", 0x0007: "DeviceFriendlyName",
    0x0020: "ConfigChange", 0x0021: "UniqueIdentifier",
    0x1000: "BatteryStatus", 0x1004: "UnifiedBattery",
    0x1802: "DeviceReset", 0x1814: "ChangeHost", 0x1815: "HostsInfo",
    0x1B04: "ReprogrammableKeys", 0x1D4B: "WirelessDeviceStatus",
    0x1E00: "EnableHiddenFeatures", 0x2110: "SmartShift",
    0x2201: "AdjustableDPI", 0x40A3: "FnInversion",
    0x4521: "DisableKeys", 0x4531: "MultiPlatform",
}

LOG = None


def out(s=""):
    print(s)
    if LOG:
        LOG.write(s + "\n")
        LOG.flush()


def hexs(b):
    return " ".join("%02X" % x for x in b)


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def build(rid, slot, b2, b3, params):
    ln = SHORT_LEN if rid == REPORT_SHORT else LONG_LEN
    return bytes(([rid, slot, b2, b3] + list(params) + [0] * ln)[:ln])


def hidpp2(rid, slot, fidx, func, params):
    return build(rid, slot, fidx, ((func & 0x0F) << 4) | SW_ID, params)


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
            out("열기 실패: %s" % e)
    return None, None


class Stats:
    foreign = 0     # 우리 swid 가 아닌 응답 = 다른 프로그램 존재의 증거


def transact(h, slot, fidx, func, params, rid=REPORT_SHORT, timeout_ms=600):
    h.write(hidpp2(rid, slot, fidx, func, params))
    want = ((func & 0x0F) << 4) | SW_ID
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        r = h.read(64, timeout_ms=int((deadline - time.time()) * 1000) + 1)
        if not r or len(r) < 4:
            continue
        if r[1] != slot:
            continue
        if r[2] in (ERR_V2, ERR_V1):
            return None
        if r[2] == fidx and r[3] == want:
            return bytes(r)
        if (r[3] & 0x0F) not in (0x00, SW_ID):
            Stats.foreign += 1
    return None


def feature_index(h, slot, fid):
    r = transact(h, slot, 0x00, IROOT_GET_FEATURE,
                 [(fid >> 8) & 0xFF, fid & 0xFF, 0])
    return r[4] if r else 0


class DevInfo:
    def __init__(self, slot):
        self.slot = slot
        self.name = None
        self.tcode = None
        self.ch_idx = 0
        self.hi_idx = 0
        self.n_hosts = None
        self.cur_host = None
        self.features = {}
        self.desc = {}

    @property
    def label(self):
        return "%s(%s)" % (self.name or "?", DEVICE_TYPES.get(self.tcode, "?"))

    def feat_label(self, idx):
        fid = self.features.get(idx)
        return ("0x%04X %s" % (fid, FEATURE_NAMES.get(fid, "?"))
                if fid is not None else "featidx=0x%02X" % idx)


def wake(h):
    """자고 있는 기기를 깨우려고 가볍게 두드린다."""
    for slot in range(1, 7):
        h.write(hidpp2(REPORT_SHORT, slot, 0x00, IROOT_PING, [0, 0, 0xAA]))
        time.sleep(0.02)
    deadline = time.time() + 0.4
    while time.time() < deadline:
        if not h.read(64, timeout_ms=50):
            break


def scan_once(h, resolve=True):
    devs = {}
    for slot in range(1, 7):
        if not transact(h, slot, 0x00, IROOT_PING, [0, 0, 0xAA]):
            continue
        d = DevInfo(slot)
        ni = feature_index(h, slot, FEAT_DEVICE_NAME)
        if ni:
            c = transact(h, slot, ni, NAME_GET_COUNT, [0, 0, 0])
            if c:
                count, txt, pos = c[4], "", 0
                while pos < count and pos < 64:
                    nr = transact(h, slot, ni, NAME_GET_NAME, [pos, 0, 0],
                                  rid=REPORT_LONG)
                    if not nr:
                        break
                    txt += bytes(nr[4:20])[:min(16, count - pos)] \
                        .decode("ascii", "replace")
                    pos += 16
                d.name = txt.strip()
            t = transact(h, slot, ni, NAME_GET_TYPE, [0, 0, 0])
            if t:
                d.tcode = t[4]
        d.ch_idx = feature_index(h, slot, FEAT_CHANGE_HOST)
        d.hi_idx = feature_index(h, slot, FEAT_HOSTS_INFO)
        if d.ch_idx:
            r = transact(h, slot, d.ch_idx, CH_GET_HOST_INFO, [0, 0, 0])
            if r:
                d.n_hosts, d.cur_host = r[4], r[5]
        if d.hi_idx and d.n_hosts:
            for host in range(d.n_hosts):
                r = transact(h, slot, d.hi_idx, HI_GET_HOST_DESC,
                             [host, 0, 0], rid=REPORT_LONG)
                if r:
                    d.desc[host] = bytes(r[4:])[1:9].rstrip(b"\x00")
        if resolve:
            fs = feature_index(h, slot, FEAT_FEATURE_SET)
            if fs:
                cnt = transact(h, slot, fs, FSET_GET_COUNT, [0, 0, 0])
                for i in range((cnt[4] if cnt else 0) + 1):
                    r = transact(h, slot, fs, FSET_GET_FEATURE_ID, [i, 0, 0],
                                 timeout_ms=250)
                    if r:
                        d.features[i] = (r[4] << 8) | r[5]
        devs[slot] = d
    return devs


def scan(h, tries=3, resolve=True):
    """한 번에 실패하는 경우가 잦아서 깨우고 재시도한다."""
    for n in range(tries):
        wake(h)
        devs = scan_once(h, resolve=resolve)
        if devs:
            return devs
        if n < tries - 1:
            out("  기기를 못 찾았습니다. 재시도 %d/%d..." % (n + 2, tries))
            time.sleep(0.6)
    return {}


def get_flags(h):
    h.write(build(REPORT_SHORT, RECEIVER, SUB_GET_REGISTER,
                  REG_NOTIFICATIONS, [0, 0, 0]))
    deadline = time.time() + 0.7
    while time.time() < deadline:
        r = h.read(64, timeout_ms=int((deadline - time.time()) * 1000) + 1)
        if r and len(r) >= 7 and r[1] == RECEIVER and r[2] == SUB_GET_REGISTER:
            return (r[4] << 16) | (r[5] << 8) | r[6]
    return None


def set_flags(h, flags):
    h.write(build(REPORT_SHORT, RECEIVER, SUB_SET_REGISTER, REG_NOTIFICATIONS,
                  [(flags >> 16) & 0xFF, (flags >> 8) & 0xFF, flags & 0xFF]))
    deadline = time.time() + 0.7
    while time.time() < deadline:
        r = h.read(64, timeout_ms=int((deadline - time.time()) * 1000) + 1)
        if r and len(r) >= 4 and r[1] == RECEIVER and r[2] == SUB_SET_REGISTER:
            return True
    return False


def build_host_map(kb, mouse):
    """키보드 host index -> 마우스 host index.

    디스크립터가 같으면 같은 컴퓨터로 본다. 못 찾으면 None.
    """
    mapping = {}
    for kh, kd in kb.desc.items():
        match = [mh for mh, md in mouse.desc.items() if md == kd]
        mapping[kh] = match[0] if len(match) == 1 else None
    return mapping


def decode(r, devs):
    slot, b2, b3 = r[1], r[2], r[3]
    d = devs.get(slot)
    who = d.label if d else ("리시버" if slot == RECEIVER else "슬롯%d" % slot)
    payload = r[4:]
    lines = []

    if b2 in NOTIF_NAMES:
        st = payload[0] if payload else 0
        lines.append("[알림] %s  %s  링크 %s (%s)"
                     % (who, NOTIF_NAMES[b2],
                        "끊김" if st & 0x40 else "연결됨",
                        NOTIF_KIND.get(st & 0x0F, "?")))
        return lines, ("link_down" if st & 0x40 else "link_up", None)

    if (b3 & 0x0F) == 0x00 and b2 < 0x80:
        fid = d.features.get(b2) if d else None
        lines.append("[이벤트] %s  %s"
                     % (who, d.feat_label(b2) if d else "featidx=0x%02X" % b2))
        lines.append("   payload: %s" % hexs(payload[:8]))
        if fid == FEAT_CHANGE_HOST or (fid is None and len(payload) >= 2):
            if fid == FEAT_CHANGE_HOST:
                lines.append("   >>> ChangeHost: host %d -> host %d (채널 %d -> %d)"
                             % (payload[0], payload[1],
                                payload[0] + 1, payload[1] + 1))
                return lines, ("changehost", payload)
        return lines, None

    if b2 in (ERR_V1, ERR_V2):
        return ["[에러] %s err=0x%02X" % (who, r[5] if len(r) > 5 else 0)], None

    swid = b3 & 0x0F
    if swid != SW_ID:
        Stats.foreign += 1
    lines.append("[응답] %s  %s swid=0x%X%s"
                 % (who, d.feat_label(b2) if d else "featidx=0x%02X" % b2,
                    swid, "  <- 다른 프로그램" if swid != SW_ID else ""))
    return lines, None


def show_devices(devs):
    for s, d in sorted(devs.items()):
        cur = "채널%d" % (d.cur_host + 1) if d.cur_host is not None else "?"
        out("  슬롯 %d = %-32s 현재 %s  ChangeHost=0x%02X 기능 %d개"
            % (s, d.label, cur, d.ch_idx, len(d.features)))
        for host in sorted(d.desc):
            out("           채널%d 디스크립터 %s" % (host + 1, hexs(d.desc[host])))


def cmd_map(args):
    h, meta = open_vendor()
    if not h:
        sys.exit("벤더 인터페이스를 못 찾았습니다.")
    try:
        out("리시버 PID=0x%04X\n" % meta["product_id"])
        devs = scan(h)
        if not devs:
            sys.exit("기기를 못 찾았습니다. Options+ 종료 후 다시 시도하세요.")
        show_devices(devs)

        kbs = [d for d in devs.values() if d.tcode == TYPE_KEYBOARD]
        mice = [d for d in devs.values() if d.tcode == TYPE_MOUSE]
        if kbs and mice:
            m = build_host_map(kbs[0], mice[0])
            out("\n키보드 -> 마우스 채널 매핑")
            for kh in sorted(m):
                mh = m[kh]
                out("  키보드 채널%d  ->  %s"
                    % (kh + 1,
                       "마우스 채널%d" % (mh + 1) if mh is not None
                       else "대응 없음"))
        if Stats.foreign:
            out("\n다른 프로그램의 트래픽 %d건 감지. Options+ 종료를 권합니다."
                % Stats.foreign)
    finally:
        h.close()


def cmd_follow(args):
    h, meta = open_vendor()
    if not h:
        sys.exit("벤더 인터페이스를 못 찾았습니다.")
    orig = None
    try:
        out("리시버 PID=0x%04X\n" % meta["product_id"])
        devs = scan(h)
        if not devs:
            sys.exit("기기를 못 찾았습니다. Options+ 종료 후 다시 시도하세요.")
        show_devices(devs)

        kbs = [d for d in devs.values() if d.tcode == TYPE_KEYBOARD]
        mice = [d for d in devs.values() if d.tcode == TYPE_MOUSE and d.ch_idx]
        if not kbs or not mice:
            sys.exit("\n키보드와 마우스가 모두 리시버 채널에 있어야 합니다.")
        kb, mouse = kbs[0], mice[0]
        hostmap = build_host_map(kb, mouse)

        orig = get_flags(h)
        if orig is not None:
            want = orig | FLAG_WIRELESS | FLAG_SW_PRESENT
            if want != orig:
                set_flags(h, want)

        out("\n감시   : %s (슬롯 %d)" % (kb.label, kb.slot))
        out("따라감 : %s (슬롯 %d)" % (mouse.label, mouse.slot))
        out("매핑   : %s" % ", ".join(
            "kb%d->%s" % (k + 1, "ms%d" % (v + 1) if v is not None else "?")
            for k, v in sorted(hostmap.items())))
        if args.dry_run:
            out("*** dry-run — 실제로 보내지 않습니다 ***")
        if Stats.foreign:
            out("경고: 다른 프로그램 트래픽 %d건. 이벤트가 유실될 수 있습니다."
                % Stats.foreign)

        out("\n" + "=" * 60)
        out("키보드 Easy-Switch 키를 누르세요.  (Ctrl-C 종료)")
        out("=" * 60 + "\n")

        h.set_nonblocking(1)
        last_change = 0.0
        while True:
            r = h.read(64)
            if not r:
                time.sleep(0.005)
                continue
            r = bytes(r)
            lines, ev = decode(r, devs)
            if ev or args.verbose:
                out("%s  %s" % (ts(), hexs(r)))
                for l in lines:
                    out("            %s" % l)

            if not ev or r[1] != kb.slot:
                continue

            kind = ev[0]
            dest_m = None

            if kind == "changehost":
                last_change = time.time()
                kb_dest = ev[1][1]
                dest_m = hostmap.get(kb_dest)
                if dest_m is None:
                    if args.strict:
                        out("            -> 채널 대응을 못 찾아 중단 (--strict)")
                        out("")
                        continue
                    dest_m = kb_dest
                    out("            -> 대응 불명. 같은 번호(%d)로 진행" % (dest_m + 1))

            elif kind == "link_down" and args.fallback:
                if time.time() - last_change < 1.0:
                    continue          # 방금 ChangeHost 로 처리했다
                if args.to is None:
                    out("            -> 링크 끊김이지만 --to 가 없어 무시")
                    out("")
                    continue
                dest_m = args.to - 1
                out("            -> fallback: 채널 %d 로" % args.to)
            else:
                if ev and args.verbose:
                    out("")
                continue

            if mouse.n_hosts and dest_m >= mouse.n_hosts:
                out("            -> 채널 %d 는 마우스에 없습니다" % (dest_m + 1))
                out("")
                continue

            pkt = hidpp2(REPORT_SHORT, mouse.slot, mouse.ch_idx,
                         CH_SET_CURRENT_HOST, [dest_m, 0, 0])
            out("            => 마우스 -> 채널 %d   %s" % (dest_m + 1, hexs(pkt)))
            if not args.dry_run:
                h.set_nonblocking(0)
                h.write(pkt)
                h.set_nonblocking(1)
            out("")

    except KeyboardInterrupt:
        out("\n종료합니다.")
    finally:
        if orig is not None:
            try:
                h.set_nonblocking(0)
                set_flags(h, orig)
            except Exception:
                pass
        h.close()


def parse_args(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--to", type=int, default=None,
                     help="fallback 목적지 채널 (1-based)")
    pre.add_argument("--fallback", action="store_true",
                     help="ChangeHost 이벤트가 없을 때 링크 끊김도 트리거로 사용"
                          " (절전과 구분 못 하니 주의)")
    pre.add_argument("--strict", action="store_true",
                     help="채널 대응을 못 찾으면 전환하지 않음")
    pre.add_argument("--dry-run", action="store_true")
    pre.add_argument("--verbose", action="store_true")
    pre.add_argument("--log", help="출력을 파일로도 남긴다")
    opts, rest = pre.parse_known_args(argv)

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("map", help="기기/채널/매핑 확인")
    sub.add_parser("follow", help="키보드를 따라 마우스 전환")

    args = p.parse_args(rest)
    for k, v in vars(opts).items():
        setattr(args, k, v)
    return args


def main():
    global LOG
    args = parse_args()
    if args.log:
        LOG = open(args.log, "a", encoding="utf-8")
        LOG.write("\n===== %s =====\n" % datetime.datetime.now())
    try:
        {"map": cmd_map, "follow": cmd_follow}[args.cmd](args)
    finally:
        if LOG:
            LOG.close()


if __name__ == "__main__":
    main()
