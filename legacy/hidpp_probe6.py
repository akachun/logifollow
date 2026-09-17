#!/usr/bin/env python3
"""
hidpp_probe6.py - Logitech HID++ (v6) : 이벤트 관찰

새 명령:
  notif        리시버 알림 플래그(레지스터 0x00)를 읽고 켠다
  watch        비동기로 날아오는 모든 리포트를 해석해서 출력

목적:
  키보드가 Easy-Switch 키로 다른 채널에 갈 때 리시버가 어떤 알림을
  보내는지, 그리고 거기에 "어느 채널로 갔는지" 정보가 실려 있는지
  실측으로 확인한다.

사용법:
  uv run --python 3.12 --with hidapi hidpp_probe6.py notif
  uv run --python 3.12 --with hidapi hidpp_probe6.py watch

watch 를 켜둔 상태에서 해볼 것:
  1. 키보드 F1(채널1) 누르기      -> 떠나는 알림
  2. 키보드 F3(리시버 채널) 누르기 -> 돌아오는 알림
  3. 마우스 바닥 버튼으로 왕복
  4. 마우스를 잠시 안 건드려 절전 진입시키기

각 동작 사이에 몇 초 간격을 두면 로그 읽기가 편하다.
Ctrl-C 로 종료하면 알림 플래그를 원래대로 되돌린다.
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

# HID++ 1.0 레지스터 접근
SUB_SET_REGISTER = 0x80
SUB_GET_REGISTER = 0x81
REG_NOTIFICATIONS = 0x00

# Solaar 기준: wireless notification 은 3바이트 플래그 중 0x000100
FLAG_WIRELESS = 0x000100
FLAG_SW_PRESENT = 0x000800

# HID++ 1.0 알림 sub id
NOTIF_DEVICE_CONNECTION = 0x41
NOTIF_DEVICE_DISCONNECTION = 0x40
NOTIF_NAMES = {
    0x40: "DEVICE_DISCONNECTION",
    0x41: "DEVICE_CONNECTION",
    0x49: "DEVICE_DISCOVERY",
    0x4A: "PAIRING_STATUS",
    0x0F: "가상 키보드 입력",
}

FEAT_DEVICE_NAME = 0x0005
FEAT_CHANGE_HOST = 0x1814
FEAT_HOSTS_INFO = 0x1815
IROOT_GET_FEATURE, IROOT_PING = 0x00, 0x01
NAME_GET_COUNT, NAME_GET_NAME, NAME_GET_TYPE = 0x00, 0x01, 0x02
CH_GET_HOST_INFO = 0x00
ERR_V2, ERR_V1 = 0x8F, 0xFF

DEVICE_TYPES = {0: "키보드", 1: "리모컨", 2: "숫자패드", 3: "마우스",
                4: "터치패드", 5: "트랙볼", 6: "프리젠터", 7: "리시버"}


def hexs(b):
    return " ".join("%02X" % x for x in b)


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def build(report_id, slot, b2, b3, params):
    ln = SHORT_LEN if report_id == REPORT_SHORT else LONG_LEN
    pkt = [report_id, slot, b2, b3] + list(params) + [0] * ln
    return bytes(pkt[:ln])


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


# ------------------------------------------------------------------ 레지스터

def get_notification_flags(h, timeout_ms=700):
    h.write(build(REPORT_SHORT, RECEIVER, SUB_GET_REGISTER,
                  REG_NOTIFICATIONS, [0, 0, 0]))
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        r = h.read(64, timeout_ms=int((deadline - time.time()) * 1000) + 1)
        if not r or len(r) < 7:
            continue
        if r[1] == RECEIVER and r[2] == SUB_GET_REGISTER and r[3] == REG_NOTIFICATIONS:
            return (r[4] << 16) | (r[5] << 8) | r[6], bytes(r)
        if r[1] == RECEIVER and r[2] == ERR_V1:
            return None, bytes(r)
    return None, None


def set_notification_flags(h, flags, timeout_ms=700):
    p = [(flags >> 16) & 0xFF, (flags >> 8) & 0xFF, flags & 0xFF]
    h.write(build(REPORT_SHORT, RECEIVER, SUB_SET_REGISTER,
                  REG_NOTIFICATIONS, p))
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        r = h.read(64, timeout_ms=int((deadline - time.time()) * 1000) + 1)
        if not r or len(r) < 4:
            continue
        if r[1] == RECEIVER and r[2] == SUB_SET_REGISTER:
            return True, bytes(r)
        if r[1] == RECEIVER and r[2] == ERR_V1:
            return False, bytes(r)
    return False, None


def cmd_notif(args):
    h, meta = open_vendor()
    if not h:
        sys.exit("벤더 인터페이스를 못 찾았습니다.")
    try:
        cur, raw = get_notification_flags(h)
        if cur is None:
            print("플래그 읽기 실패: %s" % (hexs(raw) if raw else "무응답"))
            print("이 리시버는 레지스터 0x00 을 지원하지 않을 수 있습니다.")
            print("그래도 watch 는 해보세요. 알림이 기본으로 켜져 있을 수 있습니다.")
            return
        print("현재 플래그 : 0x%06X   (%s)" % (cur, hexs(raw)))
        print("  wireless      : %s" % ("켜짐" if cur & FLAG_WIRELESS else "꺼짐"))
        print("  sw_present    : %s" % ("켜짐" if cur & FLAG_SW_PRESENT else "꺼짐"))

        want = cur | FLAG_WIRELESS | FLAG_SW_PRESENT
        if want == cur:
            print("\n이미 필요한 비트가 켜져 있습니다.")
            return
        ok, raw2 = set_notification_flags(h, want)
        print("\n0x%06X 로 설정 : %s" % (want, "성공" if ok else "실패"))
        if raw2:
            print("  응답 %s" % hexs(raw2))
        back, _ = get_notification_flags(h)
        print("  재확인 : 0x%06X" % back if back is not None else "  재확인 실패")
    finally:
        h.close()


# ------------------------------------------------------------------ 기기 정보

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


def slot_labels(h):
    """슬롯 번호 -> 사람이 읽을 이름. watch 로그를 알아보기 쉽게."""
    labels = {}
    for slot in range(1, 7):
        r = transact(h, slot, 0x00, IROOT_PING, [0, 0, 0xAA])
        if not r:
            continue
        fi = transact(h, slot, 0x00, IROOT_GET_FEATURE, [0x00, 0x05, 0])
        name = None
        tcode = None
        if fi:
            idx = fi[4]
            c = transact(h, slot, idx, NAME_GET_COUNT, [0, 0, 0])
            if c:
                count, txt, pos = c[4], "", 0
                while pos < count and pos < 64:
                    nr = transact(h, slot, idx, NAME_GET_NAME, [pos, 0, 0],
                                  report_id=REPORT_LONG)
                    if not nr:
                        break
                    txt += bytes(nr[4:20])[:min(16, count - pos)] \
                        .decode("ascii", "replace")
                    pos += 16
                name = txt.strip()
            t = transact(h, slot, idx, NAME_GET_TYPE, [0, 0, 0])
            if t:
                tcode = t[4]
        labels[slot] = "%s(%s)" % (name or "?", DEVICE_TYPES.get(tcode, "?"))
    return labels


# ------------------------------------------------------------------ 관찰

def decode(r, labels):
    """리포트 한 개를 사람이 읽을 수 있게 푼다."""
    rid, slot = r[0], r[1]
    who = labels.get(slot, "슬롯%d" % slot if slot != RECEIVER else "리시버")
    b2, b3 = r[2], r[3]
    lines = []

    # HID++ 1.0 알림: sub id 가 0x40~0x4F 근처
    if b2 in NOTIF_NAMES:
        lines.append("[알림] %s  <- %s" % (NOTIF_NAMES[b2], who))
        if b2 == NOTIF_DEVICE_CONNECTION:
            info = b3
            dev_type = info & 0x0F
            link_down = bool(info & 0x40)
            encrypted = bool(info & 0x20)
            lines.append("   protocol/info=0x%02X  기기종류=%s" %
                         (info, DEVICE_TYPES.get(dev_type, "0x%X" % dev_type)))
            lines.append("   링크=%s  암호화=%s" %
                         ("끊김" if link_down else "연결됨",
                          "예" if encrypted else "아니오"))
            lines.append("   나머지 payload: %s" % hexs(r[4:]))
        return lines

    # HID++ 2.0 이벤트: func|swid 의 하위 4비트(swid)가 0
    if (b3 & 0x0F) == 0x00 and b2 < 0x80:
        lines.append("[이벤트] %s  featidx=0x%02X func=%d"
                     % (who, b2, (b3 >> 4) & 0x0F))
        lines.append("   payload: %s" % hexs(r[4:]))
        return lines

    if b2 in (ERR_V1, ERR_V2):
        lines.append("[에러] %s  err=0x%02X" % (who, r[5] if len(r) > 5 else 0))
        return lines

    swid = b3 & 0x0F
    lines.append("[응답] %s  featidx=0x%02X func=%d swid=0x%X%s"
                 % (who, b2, (b3 >> 4) & 0x0F, swid,
                    "" if swid == SW_ID else "  <- 다른 프로그램"))
    lines.append("   payload: %s" % hexs(r[4:]))
    return lines


def cmd_watch(args):
    h, meta = open_vendor()
    if not h:
        sys.exit("벤더 인터페이스를 못 찾았습니다.")

    original = None
    try:
        print("리시버 PID=0x%04X" % meta["product_id"])
        print("기기 확인 중...")
        labels = slot_labels(h)
        for s, l in sorted(labels.items()):
            print("  슬롯 %d = %s" % (s, l))

        if not args.no_enable:
            cur, _ = get_notification_flags(h)
            if cur is not None:
                original = cur
                want = cur | FLAG_WIRELESS | FLAG_SW_PRESENT
                if want != cur:
                    ok, _ = set_notification_flags(h, want)
                    print("\n알림 플래그 0x%06X -> 0x%06X : %s"
                          % (cur, want, "성공" if ok else "실패"))
                else:
                    print("\n알림 플래그 이미 설정됨 (0x%06X)" % cur)
            else:
                print("\n알림 플래그를 못 읽었습니다. 그대로 관찰합니다.")

        print("\n" + "=" * 56)
        print("관찰 시작. 키보드 Easy-Switch 키를 눌러보세요.  (Ctrl-C 종료)")
        print("=" * 56 + "\n")

        h.set_nonblocking(1)
        last = time.time()
        while True:
            r = h.read(64)
            if not r:
                time.sleep(0.01)
                if args.heartbeat and time.time() - last > 10:
                    print("%s  (대기 중)" % ts())
                    last = time.time()
                continue
            last = time.time()
            r = bytes(r)
            print("%s  %s" % (ts(), hexs(r)))
            for line in decode(r, labels):
                print("            %s" % line)
            print()

    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        if original is not None:
            try:
                h.set_nonblocking(0)
                set_notification_flags(h, original)
                print("알림 플래그를 0x%06X 로 복원했습니다." % original)
            except Exception:
                pass
        h.close()


def parse_args(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--no-enable", action="store_true",
                     help="알림 플래그를 건드리지 않고 관찰만")
    pre.add_argument("--heartbeat", action="store_true",
                     help="조용할 때 10초마다 살아있음 표시")
    opts, rest = pre.parse_known_args(argv)

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("notif", help="알림 플래그 읽기/켜기")
    sub.add_parser("watch", help="비동기 리포트 관찰")

    args = p.parse_args(rest)
    for k, v in vars(opts).items():
        setattr(args, k, v)
    return args


def main():
    args = parse_args()
    {"notif": cmd_notif, "watch": cmd_watch}[args.cmd](args)


if __name__ == "__main__":
    main()
