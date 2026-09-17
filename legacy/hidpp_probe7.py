#!/usr/bin/env python3
"""
hidpp_probe7.py - Logitech HID++ (v7) : 이벤트 추종

v6 대비 변경:
  - IFeatureSet(0x0001) 로 featidx -> feature ID 를 역조회해서
    이벤트에 실제 기능 이름을 붙인다
  - 0x41 알림 디코딩 수정. 상태 바이트는 byte3 이 아니라 payload[0]
  - ChangeHost 이벤트를 별도로 강조 출력
  - follow 명령 추가: 키보드가 채널을 옮기면 마우스를 따라 보낸다

사용법:
  # 1단계 - 가설 검증. F1, F3, F2, F3 순서로 눌러보기
  uv run --python 3.12 --with hidapi hidpp_probe7.py watch

  # 2단계 - 실제 추종
  uv run --python 3.12 --with hidapi hidpp_probe7.py follow           # 자동
  uv run --python 3.12 --with hidapi hidpp_probe7.py follow --to 1    # 고정
  uv run --python 3.12 --with hidapi hidpp_probe7.py follow --dry-run # 연습

검증할 가설:
  ChangeHost 이벤트 payload 가 [출발 host, 도착 host] 라는 것.
  F1 -> "02 00", F2 -> "02 01" 로 나오면 확정.
  둘 다 "02 00" 이면 두 번째 바이트는 패딩이고, --to 로 목적지를
  고정하는 방식으로 가야 한다.
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

NOTIF_CONNECTION = 0x41
NOTIF_NAMES = {0x40: "DISCONNECTION", 0x41: "CONNECTION",
               0x49: "DISCOVERY", 0x4A: "PAIRING_STATUS"}

FEAT_FEATURE_SET = 0x0001
FEAT_DEVICE_NAME = 0x0005
FEAT_CHANGE_HOST = 0x1814

IROOT_GET_FEATURE, IROOT_PING = 0x00, 0x01
FSET_GET_COUNT, FSET_GET_FEATURE_ID = 0x00, 0x01
NAME_GET_COUNT, NAME_GET_NAME, NAME_GET_TYPE = 0x00, 0x01, 0x02
CH_GET_HOST_INFO, CH_SET_CURRENT_HOST = 0x00, 0x01
ERR_V2, ERR_V1 = 0x8F, 0xFF

TYPE_KEYBOARD, TYPE_MOUSE = 0, 3
DEVICE_TYPES = {0: "키보드", 1: "리모컨", 2: "숫자패드", 3: "마우스",
                4: "터치패드", 5: "트랙볼", 6: "프리젠터", 7: "리시버"}

# 0x41 알림 payload[0] 하위 니블의 기기 종류 (HID++ 1.0 계열 값)
NOTIF_KIND = {1: "키보드", 2: "마우스", 3: "숫자패드", 4: "프리젠터",
              8: "트랙볼", 9: "터치패드"}

FEATURE_NAMES = {
    0x0000: "IRoot", 0x0001: "IFeatureSet", 0x0003: "DeviceInformation",
    0x0005: "DeviceNameAndType", 0x0007: "DeviceFriendlyName",
    0x0020: "ConfigChange", 0x0021: "UniqueIdentifier",
    0x1000: "BatteryStatus", 0x1001: "BatteryVoltage",
    0x1004: "UnifiedBattery", 0x1802: "DeviceReset",
    0x1814: "ChangeHost", 0x1815: "HostsInfo",
    0x1B04: "ReprogrammableKeys", 0x1D4B: "WirelessDeviceStatus",
    0x1E00: "EnableHiddenFeatures", 0x2100: "VerticalScrolling",
    0x2110: "SmartShift", 0x2201: "AdjustableDPI",
    0x40A3: "FnInversion", 0x4521: "DisableKeys",
    0x4531: "MultiPlatform", 0x8100: "OnboardProfiles",
}


def hexs(b):
    return " ".join("%02X" % x for x in b)


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def build(report_id, slot, b2, b3, params):
    ln = SHORT_LEN if report_id == REPORT_SHORT else LONG_LEN
    return bytes(([report_id, slot, b2, b3] + list(params) + [0] * ln)[:ln])


def hidpp2(report_id, slot, feat_idx, func, params):
    return build(report_id, slot, feat_idx, ((func & 0x0F) << 4) | SW_ID, params)


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


def transact(h, slot, feat_idx, func, params, report_id=REPORT_SHORT,
             timeout_ms=600):
    h.write(hidpp2(report_id, slot, feat_idx, func, params))
    want = ((func & 0x0F) << 4) | SW_ID
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        r = h.read(64, timeout_ms=int((deadline - time.time()) * 1000) + 1)
        if not r or len(r) < 4 or r[1] != slot:
            continue
        if r[2] in (ERR_V2, ERR_V1):
            return None
        if r[2] == feat_idx and r[3] == want:
            return bytes(r)
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
        self.n_hosts = None
        self.cur_host = None
        self.features = {}   # featidx -> feature id

    @property
    def label(self):
        return "%s(%s)" % (self.name or "?",
                           DEVICE_TYPES.get(self.tcode, "?"))

    def feat_label(self, idx):
        fid = self.features.get(idx)
        if fid is None:
            return "featidx=0x%02X" % idx
        return "0x%04X %s" % (fid, FEATURE_NAMES.get(fid, "?"))


def scan(h, resolve_features=True):
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
                                  report_id=REPORT_LONG)
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
        if d.ch_idx:
            r = transact(h, slot, d.ch_idx, CH_GET_HOST_INFO, [0, 0, 0])
            if r:
                d.n_hosts, d.cur_host = r[4], r[5]

        if resolve_features:
            fs = feature_index(h, slot, FEAT_FEATURE_SET)
            if fs:
                cnt = transact(h, slot, fs, FSET_GET_COUNT, [0, 0, 0])
                n = cnt[4] if cnt else 0
                for i in range(0, min(n, 40) + 1):
                    r = transact(h, slot, fs, FSET_GET_FEATURE_ID, [i, 0, 0],
                                 timeout_ms=300)
                    if r:
                        d.features[i] = (r[4] << 8) | r[5]

        devs[slot] = d
    return devs


# ------------------------------------------------------------------ 디코딩

def decode(r, devs):
    rid, slot, b2, b3 = r[0], r[1], r[2], r[3]
    d = devs.get(slot)
    who = d.label if d else ("리시버" if slot == RECEIVER else "슬롯%d" % slot)
    payload = r[4:]
    out = []

    if b2 in NOTIF_NAMES:
        st = payload[0] if payload else 0
        kind = NOTIF_KIND.get(st & 0x0F, "0x%X" % (st & 0x0F))
        link = "끊김" if st & 0x40 else "연결됨"
        out.append("[알림] %s  %s" % (who, NOTIF_NAMES[b2]))
        out.append("   프로토콜=0x%02X  상태=0x%02X -> 링크 %s, 종류 %s"
                   % (b3, st, link, kind))
        if len(payload) >= 3:
            out.append("   wireless PID = %02X%02X" % (payload[1], payload[2]))
        return out, ("link_down" if st & 0x40 else "link_up")

    if (b3 & 0x0F) == 0x00 and b2 < 0x80:
        func = (b3 >> 4) & 0x0F
        label = d.feat_label(b2) if d else "featidx=0x%02X" % b2
        fid = d.features.get(b2) if d else None
        out.append("[이벤트] %s  %s  func=%d" % (who, label, func))
        out.append("   payload: %s" % hexs(payload))
        if fid == FEAT_CHANGE_HOST:
            out.append("   >>> ChangeHost 이벤트 <<<")
            if len(payload) >= 2:
                out.append("   가설: 출발 host=%d(채널%d) -> 도착 host=%d(채널%d)"
                           % (payload[0], payload[0] + 1,
                              payload[1], payload[1] + 1))
            return out, ("changehost", payload)
        return out, None

    if b2 in (ERR_V1, ERR_V2):
        out.append("[에러] %s  err=0x%02X" % (who, r[5] if len(r) > 5 else 0))
        return out, None

    swid = b3 & 0x0F
    label = d.feat_label(b2) if d else "featidx=0x%02X" % b2
    out.append("[응답] %s  %s func=%d swid=0x%X%s"
               % (who, label, (b3 >> 4) & 0x0F, swid,
                  "  <- 다른 프로그램" if swid != SW_ID else ""))
    out.append("   payload: %s" % hexs(payload))
    return out, None


# ------------------------------------------------------------------ 알림 플래그

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


def prepare(h, quiet=False):
    devs = scan(h)
    if not quiet:
        for s, d in sorted(devs.items()):
            cur = "채널%d" % (d.cur_host + 1) if d.cur_host is not None else "?"
            print("  슬롯 %d = %-34s 현재 %s  ChangeHost=0x%02X  기능 %d개"
                  % (s, d.label, cur, d.ch_idx, len(d.features)))
    cur = get_flags(h)
    if cur is not None:
        want = cur | FLAG_WIRELESS | FLAG_SW_PRESENT
        if want != cur:
            set_flags(h, want)
            if not quiet:
                print("\n알림 플래그 0x%06X -> 0x%06X" % (cur, want))
        elif not quiet:
            print("\n알림 플래그 이미 설정됨 (0x%06X)" % cur)
    return devs, cur


def cmd_watch(args):
    h, meta = open_vendor()
    if not h:
        sys.exit("벤더 인터페이스를 못 찾았습니다.")
    orig = None
    try:
        print("리시버 PID=0x%04X\n기기 확인 중...\n" % meta["product_id"])
        devs, orig = prepare(h)
        print("\n" + "=" * 60)
        print("F1 -> F3 -> F2 -> F3 순서로 눌러보세요.  (Ctrl-C 종료)")
        print("=" * 60 + "\n")
        h.set_nonblocking(1)
        while True:
            r = h.read(64)
            if not r:
                time.sleep(0.01)
                continue
            r = bytes(r)
            print("%s  %s" % (ts(), hexs(r)))
            lines, _ = decode(r, devs)
            for l in lines:
                print("            %s" % l)
            print()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        if orig is not None:
            try:
                h.set_nonblocking(0)
                set_flags(h, orig)
            except Exception:
                pass
        h.close()


def cmd_follow(args):
    h, meta = open_vendor()
    if not h:
        sys.exit("벤더 인터페이스를 못 찾았습니다.")
    orig = None
    try:
        print("리시버 PID=0x%04X\n기기 확인 중...\n" % meta["product_id"])
        devs, orig = prepare(h)

        kbs = [d for d in devs.values() if d.tcode == TYPE_KEYBOARD]
        mice = [d for d in devs.values() if d.tcode == TYPE_MOUSE and d.ch_idx]
        if not kbs:
            sys.exit("\n키보드를 못 찾았습니다. 리시버 채널에 있어야 합니다.")
        if not mice:
            sys.exit("\n마우스를 못 찾았습니다. 리시버 채널에 있어야 합니다.")
        kb, mouse = kbs[0], mice[0]
        print("\n감시 대상 : %s (슬롯 %d)" % (kb.label, kb.slot))
        print("따라갈 기기 : %s (슬롯 %d)" % (mouse.label, mouse.slot))
        if args.to:
            print("목적지 : 채널 %d 고정" % args.to)
        else:
            print("목적지 : 이벤트 payload[1] 에서 읽음 (자동)")
        if args.dry_run:
            print("*** dry-run — 실제로 보내지 않습니다 ***")

        print("\n" + "=" * 60)
        print("키보드 Easy-Switch 키를 누르면 마우스가 따라갑니다. (Ctrl-C 종료)")
        print("=" * 60 + "\n")

        h.set_nonblocking(1)
        while True:
            r = h.read(64)
            if not r:
                time.sleep(0.005)
                continue
            r = bytes(r)
            lines, ev = decode(r, devs)

            interesting = ev is not None or args.verbose
            if interesting:
                print("%s  %s" % (ts(), hexs(r)))
                for l in lines:
                    print("            %s" % l)

            if not ev or r[1] != kb.slot:
                if interesting:
                    print()
                continue

            dest = None
            if isinstance(ev, tuple) and ev[0] == "changehost":
                payload = ev[1]
                dest = args.to - 1 if args.to else (
                    payload[1] if len(payload) >= 2 else None)
            elif ev == "link_down" and args.on_disconnect:
                dest = args.to - 1 if args.to else None

            if dest is None:
                if interesting:
                    print("            -> 목적지를 알 수 없어 아무것도 안 함")
                    print()
                continue

            if mouse.n_hosts and dest >= mouse.n_hosts:
                print("            -> 채널 %d 는 마우스에 없습니다 (최대 %d)"
                      % (dest + 1, mouse.n_hosts))
                print()
                continue

            print("            => 마우스를 채널 %d 로 보냅니다" % (dest + 1))
            if not args.dry_run:
                pkt = hidpp2(REPORT_SHORT, mouse.slot, mouse.ch_idx,
                             CH_SET_CURRENT_HOST, [dest, 0, 0])
                print("            => %s" % hexs(pkt))
                h.set_nonblocking(0)
                h.write(pkt)
                h.set_nonblocking(1)
            print()

    except KeyboardInterrupt:
        print("\n종료합니다.")
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
                     help="목적지 채널 고정 (1-based)")
    pre.add_argument("--dry-run", action="store_true")
    pre.add_argument("--verbose", action="store_true",
                     help="관심 없는 리포트도 전부 출력")
    pre.add_argument("--on-disconnect", action="store_true",
                     help="ChangeHost 이벤트 대신 링크 끊김도 트리거로 사용")
    opts, rest = pre.parse_known_args(argv)

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("watch", help="이벤트 관찰")
    sub.add_parser("follow", help="키보드를 따라 마우스 전환")

    args = p.parse_args(rest)
    for k, v in vars(opts).items():
        setattr(args, k, v)
    return args


def main():
    args = parse_args()
    {"watch": cmd_watch, "follow": cmd_follow}[args.cmd](args)


if __name__ == "__main__":
    main()
