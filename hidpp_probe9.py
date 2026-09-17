#!/usr/bin/env python3
"""
hidpp_probe9.py - Logitech HID++ (v9) : 키보드 추종

v8 대비 변경:
  - 발견 단계 타임아웃을 크게 늘리고, 필요한 기기 종류가 모두 나올
    때까지 재시도한다. 마우스 절전 때문에 누락되던 문제
  - 채널 번호 매핑이 아니라 디스크립터 자체로 목적지를 정한다.
    "키보드가 간 그 호스트와 같은 디스크립터를 가진 마우스 채널"
  - 마우스가 이미 같은 호스트에 있으면 아무것도 안 한다
  - 재연결 알림을 받으면 기기 정보를 갱신한다

확정된 프로토콜:
  ChangeHost(0x1814) 이벤트  payload = [출발 host, 도착 host]
  HostsInfo(0x1815) func 2   payload[1:9] = 호스트 식별자
    -> 서로 다른 기기라도 같은 컴퓨터면 같은 값이 나온다

사용법:
  uv run --python 3.12 --with hidapi hidpp_probe9.py map
  uv run --python 3.12 --with hidapi hidpp_probe9.py follow --dry-run
  uv run --python 3.12 --with hidapi hidpp_probe9.py follow --log run.txt
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
NOTIF_NAMES = {0x40: "DISCONNECTION", 0x41: "CONNECTION"}

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
NOTIF_KIND = {1: "키보드", 2: "마우스", 3: "숫자패드", 8: "트랙볼", 9: "터치패드"}
FEATURE_NAMES = {
    0x0000: "IRoot", 0x0001: "IFeatureSet", 0x0003: "DeviceInformation",
    0x0005: "DeviceNameAndType", 0x0007: "DeviceFriendlyName",
    0x0020: "ConfigChange", 0x1000: "BatteryStatus",
    0x1004: "UnifiedBattery", 0x1814: "ChangeHost", 0x1815: "HostsInfo",
    0x1B04: "ReprogrammableKeys", 0x1D4B: "WirelessDeviceStatus",
    0x2110: "SmartShift", 0x2201: "AdjustableDPI", 0x4531: "MultiPlatform",
}

# 발견 단계는 절전 중인 기기를 기다려야 해서 길게, 이후 조회는 짧게
DISCOVER_TIMEOUT_MS = 2500
NORMAL_TIMEOUT_MS = 600

LOG = None


def allow_shared_open():
    """hidapi 를 비배타 모드로 돌린다.

    hidapi 0.12+ 는 macOS 에서 기기를 배타 점유(seize)로 연다. Logi Options+
    가 같은 기기를 이미 열어둔 상태면 그대로는 절대 못 연다. 그런데 에러가
    그냥 "open failed" 라서 오랫동안 입력 모니터링 권한 문제로 오진했었다.
    권한과 무관하다. 비배타로 바꾸면 그냥 열린다.
    """
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        lib = ctypes.CDLL(hid.__file__)
        lib.hid_darwin_set_open_exclusive(ctypes.c_int(0))
    except Exception as e:
        print("비배타 모드 전환 실패(계속 진행): %s" % e)


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


VENDOR_RECEIVER = 0xFF00   # 리시버 경유
VENDOR_BLE = 0xFF43        # 블루투스 직결


def open_vendor():
    """리시버의 HID++ 인터페이스를 연다.

    리시버와 블루투스 직결 기기가 동시에 붙어 있을 수 있다. 그때
    usage_page 만 보고 아무거나 열면 마우스의 0xFF43 을 잡아버린다.
    0xFF00 을 먼저 시도하고, 실제로 리시버가 대답하는지까지 확인한다.
    """
    seen = {}
    for x in hid.enumerate(LOGITECH_VID, 0):
        if (x.get("usage_page") or 0) >= 0xFF00:
            seen.setdefault(x["path"], x)

    cands = sorted(
        seen.values(),
        key=lambda x: (0 if (x.get("usage_page") or 0) == VENDOR_RECEIVER else 1,
                       x.get("usage") or 0),
    )
    fallback = None
    for meta in cands:
        try:
            h = hid.device()
            h.open_path(meta["path"])
            h.set_nonblocking(0)
        except Exception as e:
            out("열기 실패: %s" % e)
            continue
        if get_flags(h) is not None:
            return h, meta
        # 리시버 레지스터에 답하지 않으면 리시버가 아니다
        if fallback is None:
            fallback = (h, meta)
        else:
            h.close()
    if fallback:
        out("리시버 레지스터에 응답이 없습니다. 그래도 진행합니다.")
        return fallback
    return None, None


class Stats:
    foreign = 0


def transact(h, slot, fidx, func, params, rid=REPORT_SHORT,
             timeout_ms=NORMAL_TIMEOUT_MS, echo=None):
    """echo 를 주면 payload 첫 바이트가 그 값인 응답만 받는다.

    같은 feature/function 을 인자만 바꿔 연달아 부를 때 앞 요청의 늦은
    응답을 뒤 요청의 것으로 오인해 표가 한 칸씩 밀릴 수 있다.
    """
    h.write(hidpp2(rid, slot, fidx, func, params))
    want = ((func & 0x0F) << 4) | SW_ID
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        r = h.read(64, timeout_ms=int((deadline - time.time()) * 1000) + 1)
        if not r or len(r) < 4 or r[1] != slot:
            continue
        if r[2] in (ERR_V2, ERR_V1):
            return None
        if r[2] == fidx and r[3] == want:
            if echo is not None and r[4] != echo:
                continue          # 이전 요청의 늦은 응답
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

    def host_of(self, descriptor):
        """이 디스크립터를 가진 채널들을 돌려준다."""
        return [hidx for hidx, d in self.desc.items() if d == descriptor]


def probe_slot(h, slot, timeout_ms):
    """해당 슬롯이 살아있는지 넉넉히 기다리며 확인."""
    return transact(h, slot, 0x00, IROOT_PING, [0, 0, 0xAA],
                    timeout_ms=timeout_ms) is not None


def read_slot(h, slot, resolve=True):
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
            r = transact(h, slot, d.hi_idx, HI_GET_HOST_DESC, [host, 0, 0],
                         rid=REPORT_LONG, echo=host)
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
    return d


def scan(h, need=(), tries=4, resolve=True):
    """need 에 준 기기 종류가 모두 나올 때까지 재시도한다.

    절전 중인 기기는 첫 요청에 응답이 없거나 아주 느리다.
    발견용 ping 만 타임아웃을 크게 준다.
    """
    devs = {}
    for attempt in range(tries):
        timeout = DISCOVER_TIMEOUT_MS if attempt else NORMAL_TIMEOUT_MS * 2
        for slot in range(1, 7):
            if slot in devs:
                continue
            if probe_slot(h, slot, timeout):
                devs[slot] = read_slot(h, slot, resolve=resolve)

        have = {d.tcode for d in devs.values()}
        if all(t in have for t in need):
            return devs

        missing = [DEVICE_TYPES.get(t, str(t)) for t in need if t not in have]
        if attempt < tries - 1:
            out("  %s 를 못 찾았습니다. 해당 기기를 살짝 움직이거나 눌러서"
                " 깨워주세요. 재시도 %d/%d..."
                % ("/".join(missing), attempt + 2, tries))
            time.sleep(1.2)
    return devs


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


def decode(r, devs):
    slot, b2, b3 = r[1], r[2], r[3]
    d = devs.get(slot)
    who = d.label if d else ("리시버" if slot == RECEIVER else "슬롯%d" % slot)
    payload = r[4:]
    lines = []

    if b2 in NOTIF_NAMES:
        st = payload[0] if payload else 0
        lines.append("[알림] %s  링크 %s (%s)"
                     % (who, "끊김" if st & 0x40 else "연결됨",
                        NOTIF_KIND.get(st & 0x0F, "?")))
        return lines, ("link_down" if st & 0x40 else "link_up", None)

    if (b3 & 0x0F) == 0x00 and b2 < 0x80:
        fid = d.features.get(b2) if d else None
        lines.append("[이벤트] %s  %s"
                     % (who, d.feat_label(b2) if d else "featidx=0x%02X" % b2))
        if fid == FEAT_CHANGE_HOST and len(payload) >= 2:
            lines.append("   ChangeHost: host %d -> host %d (채널 %d -> %d)"
                         % (payload[0], payload[1],
                            payload[0] + 1, payload[1] + 1))
            return lines, ("changehost", payload)
        lines.append("   payload: %s" % hexs(payload[:8]))
        return lines, None

    if b2 in (ERR_V1, ERR_V2):
        return ["[에러] %s err=0x%02X" % (who, r[5] if len(r) > 5 else 0)], None

    swid = b3 & 0x0F
    if swid != SW_ID:
        Stats.foreign += 1
    return ["[응답] %s swid=0x%X%s"
            % (who, swid, "  <- 다른 프로그램" if swid != SW_ID else "")], None


def show_devices(devs):
    for s, d in sorted(devs.items()):
        cur = "채널%d" % (d.cur_host + 1) if d.cur_host is not None else "?"
        out("  슬롯 %d = %-32s 현재 %s  ChangeHost=0x%02X"
            % (s, d.label, cur, d.ch_idx))
        for host in sorted(d.desc):
            mark = " <- 현재" if host == d.cur_host else ""
            out("           채널%d  %s%s" % (host + 1, hexs(d.desc[host]), mark))


def report_mapping(kb, mouse):
    out("\n키보드 채널 -> 마우스 채널  (디스크립터 대조)")
    issues = []
    for kh in sorted(kb.desc):
        target = kb.desc[kh]
        matches = mouse.host_of(target)
        if not matches:
            out("  키보드 채널%d  ->  대응 없음" % (kh + 1))
            issues.append("키보드 채널%d 에 대응하는 마우스 채널이 없습니다"
                          % (kh + 1))
        elif len(matches) == 1:
            out("  키보드 채널%d  ->  마우스 채널%d" % (kh + 1, matches[0] + 1))
        else:
            out("  키보드 채널%d  ->  마우스 채널 %s (중복)"
                % (kh + 1, ", ".join(str(m + 1) for m in matches)))
            issues.append("마우스 채널 %s 가 같은 호스트에 중복 페어링돼 있습니다"
                          % ", ".join(str(m + 1) for m in matches))
    if issues:
        out("\n확인 필요:")
        for i in sorted(set(issues)):
            out("  - %s" % i)
    return issues


def cmd_map(args):
    allow_shared_open()
    h, meta = open_vendor()
    if not h:
        sys.exit("벤더 인터페이스를 못 찾았습니다.")
    try:
        out("리시버 PID=0x%04X\n" % meta["product_id"])
        devs = scan(h, need=(TYPE_KEYBOARD, TYPE_MOUSE))
        if not devs:
            sys.exit("기기를 못 찾았습니다.")
        show_devices(devs)
        kbs = [d for d in devs.values() if d.tcode == TYPE_KEYBOARD]
        mice = [d for d in devs.values() if d.tcode == TYPE_MOUSE]
        if kbs and mice:
            report_mapping(kbs[0], mice[0])
        if Stats.foreign:
            out("\n다른 프로그램 트래픽 %d건 감지" % Stats.foreign)
    finally:
        h.close()


def cmd_follow(args):
    allow_shared_open()
    h, meta = open_vendor()
    if not h:
        sys.exit("벤더 인터페이스를 못 찾았습니다.")
    orig = None
    try:
        out("리시버 PID=0x%04X\n" % meta["product_id"])
        devs = scan(h, need=(TYPE_KEYBOARD, TYPE_MOUSE))
        kbs = [d for d in devs.values() if d.tcode == TYPE_KEYBOARD]
        mice = [d for d in devs.values() if d.tcode == TYPE_MOUSE and d.ch_idx]
        if not kbs or not mice:
            show_devices(devs)
            sys.exit("\n키보드와 마우스가 모두 리시버 채널에 있어야 합니다.\n"
                     "마우스를 움직여 깨운 뒤 다시 실행하세요.")
        kb, mouse = kbs[0], mice[0]
        show_devices(devs)
        report_mapping(kb, mouse)

        orig = get_flags(h)
        if orig is not None:
            want = orig | FLAG_WIRELESS | FLAG_SW_PRESENT
            if want != orig:
                set_flags(h, want)

        out("\n감시   : %s (슬롯 %d)" % (kb.label, kb.slot))
        out("따라감 : %s (슬롯 %d)" % (mouse.label, mouse.slot))
        if args.dry_run:
            out("*** dry-run — 실제로 보내지 않습니다 ***")
        out("\n" + "=" * 60)
        out("키보드 Easy-Switch 키를 누르세요.  (Ctrl-C 종료)")
        out("=" * 60 + "\n")

        h.set_nonblocking(1)
        while True:
            # 기기가 채널을 옮기거나 리시버가 잠깐 흔들리면 핸들이 무효가 되고
            # hidapi 가 OSError 를 던진다. 죽지 말고 다시 열어야 한다.
            try:
                r = h.read(64)
            except OSError as e:
                out("%s  읽기 오류(%s). 리시버를 다시 엽니다..." % (ts(), e))
                try:
                    h.close()
                except Exception:
                    pass
                h = None
                for _ in range(40):
                    time.sleep(0.5)
                    h2, _meta = open_vendor()
                    if h2:
                        h = h2
                        h.set_nonblocking(1)
                        out("%s  리시버를 다시 열었습니다." % ts())
                        break
                if h is None:
                    out("리시버를 다시 열지 못했습니다. 종료합니다.")
                    break
                continue
            if not r:
                time.sleep(0.005)
                continue
            r = bytes(r)
            lines, ev = decode(r, devs)
            if ev or args.verbose:
                out("%s  %s" % (ts(), hexs(r)))
                for l in lines:
                    out("            %s" % l)

            if not ev:
                continue
            kind = ev[0]

            # 마우스가 돌아오면 현재 호스트가 바뀌었을 수 있으니 갱신
            if kind == "link_up" and r[1] == mouse.slot:
                h.set_nonblocking(0)
                fresh = read_slot(h, mouse.slot, resolve=False)
                h.set_nonblocking(1)
                if fresh.cur_host is not None:
                    mouse.cur_host = fresh.cur_host
                    mouse.desc = fresh.desc or mouse.desc
                continue

            if kind != "changehost" or r[1] != kb.slot:
                continue

            kb_dest = ev[1][1]
            target = kb.desc.get(kb_dest)
            if target is None:
                out("            -> 키보드 채널%d 디스크립터를 모릅니다"
                    % (kb_dest + 1))
                out("")
                continue

            # 마우스가 이미 같은 호스트에 있으면 할 일이 없다
            if mouse.cur_host is not None and \
               mouse.desc.get(mouse.cur_host) == target:
                out("            -> 마우스는 이미 같은 호스트입니다")
                out("")
                continue

            matches = mouse.host_of(target)
            if not matches:
                out("            -> 마우스에 대응 채널이 없습니다: %s"
                    % hexs(target))
                out("")
                continue
            dest_m = matches[0]

            pkt = hidpp2(REPORT_SHORT, mouse.slot, mouse.ch_idx,
                         CH_SET_CURRENT_HOST, [dest_m, 0, 0])
            out("            => 마우스 -> 채널 %d   %s"
                % (dest_m + 1, hexs(pkt)))
            if not args.dry_run:
                h.set_nonblocking(0)
                h.write(pkt)
                h.set_nonblocking(1)
                mouse.cur_host = dest_m
            out("")

    except KeyboardInterrupt:
        out("\n종료합니다.")
    finally:
        if orig is not None and h is not None:
            try:
                h.set_nonblocking(0)
                set_flags(h, orig)
            except Exception:
                pass
        if h is not None:
            h.close()


def parse_args(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--dry-run", action="store_true")
    pre.add_argument("--verbose", action="store_true")
    pre.add_argument("--log")
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
