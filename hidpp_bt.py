#!/usr/bin/env python3
"""
hidpp_bt.py - 블루투스 직결 로지텍 기기 HID++ 제어

실측으로 확정된 사실:
  - 직결 기기의 벤더 usage page 는 0xFF43 (리시버는 0xFF00)
  - 0xFF43 컬렉션은 리포트 ID 0x11 (long, 20바이트) 만 노출한다.
    0x10 (short) 는 서술자에 없어서 write 가 -1 로 거부된다
  - device index 는 0xFF (리시버 경유가 아니라 직결)
  - 확인된 응답: 11 FF 00 15 04 05 AA  -> HID++ 4.5

  키보드는 macOS 키보드 HID 보호 때문에 입력 모니터링 권한이
  없으면 열기 자체가 실패한다.
  시스템 설정 > 개인정보 보호 및 보안 > 입력 모니터링 에 터미널 추가
  (추가 후 터미널을 완전히 종료했다 다시 열어야 적용된다)

사용법:
  uv run --python 3.12 --with hidapi hidpp_bt.py probe
  uv run --python 3.12 --with hidapi hidpp_bt.py switch 3 --type mouse
  uv run --python 3.12 --with hidapi hidpp_bt.py follow --dry-run
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
REPORT_LONG = 0x11
LONG_LEN = 20
SW_ID = 0x05
DIRECT = 0xFF

FEAT_DEVICE_NAME = 0x0005
FEAT_CHANGE_HOST = 0x1814
FEAT_HOSTS_INFO = 0x1815
IROOT_GET_FEATURE, IROOT_PING = 0x00, 0x01
NAME_GET_COUNT, NAME_GET_NAME, NAME_GET_TYPE = 0x00, 0x01, 0x02
CH_GET_HOST_INFO, CH_SET_CURRENT_HOST = 0x00, 0x01
HI_GET_HOST_DESC = 0x02
ERR_V2, ERR_V1 = 0x8F, 0xFF

TYPE_KEYBOARD, TYPE_MOUSE = 0, 3
DEVICE_TYPES = {0: "키보드", 1: "리모컨", 2: "숫자패드", 3: "마우스",
                4: "터치패드", 5: "트랙볼", 6: "프리젠터"}

VENDOR_BLE = 0xFF43


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


def hexs(b):
    return " ".join("%02X" % x for x in b)


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def build(fidx, func, params):
    """직결 기기용. 항상 long 리포트."""
    pkt = [REPORT_LONG, DIRECT, fidx, ((func & 0x0F) << 4) | SW_ID]
    pkt += list(params) + [0] * LONG_LEN
    return bytes(pkt[:LONG_LEN])


class BtDevice:
    def __init__(self, path, pid, product):
        self.path = path
        self.pid = pid
        self.product = product
        self.h = None
        self.name = None
        self.tcode = None
        self.ch_idx = 0
        self.hi_idx = 0
        self.n_hosts = None
        self.cur_host = None
        self.desc = {}

    @property
    def label(self):
        return "%s(%s)" % (self.name or self.product,
                           DEVICE_TYPES.get(self.tcode, "?"))

    def open(self):
        self.h = hid.device()
        self.h.open_path(self.path)
        self.h.set_nonblocking(0)

    def close(self):
        if self.h:
            try:
                self.h.close()
            except Exception:
                pass
            self.h = None

    def reopen(self, timeout_s=180):
        """링크가 끊긴 뒤 기기가 돌아올 때까지 기다렸다 다시 연다.

        키보드가 채널을 옮기면 블루투스 링크가 끊기고 hidapi 핸들이 무효가
        된다. 재연결되면 경로가 바뀔 수 있으므로 경로째 재탐색해야 한다.
        """
        self.close()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(1.0)
            for cand in discover(self.pid):
                if cand.product != self.product:
                    continue
                try:
                    cand.open()
                except Exception:
                    continue
                if not cand.ping():
                    cand.close()
                    continue
                self.path, self.h = cand.path, cand.h
                cand.h = None          # 핸들 소유권을 넘겼다
                self.desc = {}
                self.load()
                return True
        return False

    def transact(self, fidx, func, params, timeout_ms=1200, raw=False):
        pkt = build(fidx, func, params)
        if raw:
            print("      -> %s" % hexs(pkt))
        if self.h.write(pkt) < 0:
            return None
        want = ((func & 0x0F) << 4) | SW_ID
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            r = self.h.read(64, timeout_ms=int((deadline - time.time()) * 1000) + 1)
            if not r or len(r) < 4:
                continue
            if r[0] != REPORT_LONG or r[1] != DIRECT:
                continue          # 마우스 입력 리포트(0x02) 등은 건너뛴다
            if raw:
                print("      <- %s" % hexs(r))
            if r[2] in (ERR_V2, ERR_V1):
                return None
            if r[2] == fidx and r[3] == want:
                return bytes(r)
        return None

    def ping(self):
        r = self.transact(0x00, IROOT_PING, [0, 0, 0xAA])
        return "HID++ %d.%d" % (r[4], r[5]) if r else None

    def feature(self, fid):
        r = self.transact(0x00, IROOT_GET_FEATURE,
                          [(fid >> 8) & 0xFF, fid & 0xFF, 0])
        return r[4] if r else 0

    def load(self):
        ni = self.feature(FEAT_DEVICE_NAME)
        if ni:
            c = self.transact(ni, NAME_GET_COUNT, [0, 0, 0])
            if c:
                count, txt, pos = c[4], "", 0
                while pos < count and pos < 64:
                    r = self.transact(ni, NAME_GET_NAME, [pos, 0, 0])
                    if not r:
                        break
                    txt += bytes(r[4:20])[:min(16, count - pos)] \
                        .decode("ascii", "replace")
                    pos += 16
                self.name = txt.strip()
            t = self.transact(ni, NAME_GET_TYPE, [0, 0, 0])
            if t:
                self.tcode = t[4]
        self.ch_idx = self.feature(FEAT_CHANGE_HOST)
        self.hi_idx = self.feature(FEAT_HOSTS_INFO)
        if self.ch_idx:
            r = self.transact(self.ch_idx, CH_GET_HOST_INFO, [0, 0, 0])
            if r:
                self.n_hosts, self.cur_host = r[4], r[5]
        if self.hi_idx and self.n_hosts:
            for host in range(self.n_hosts):
                r = self.transact(self.hi_idx, HI_GET_HOST_DESC, [host, 0, 0])
                if r:
                    self.desc[host] = bytes(r[4:])[1:9].rstrip(b"\x00")

    def hosts_with(self, descriptor):
        return [h for h, d in self.desc.items() if d == descriptor]

    def switch(self, channel, dry=False):
        pkt = build(self.ch_idx, CH_SET_CURRENT_HOST, [channel - 1, 0, 0])
        if dry:
            print("      dry-run: %s" % hexs(pkt))
            return True
        print("      => %s" % hexs(pkt))
        return self.h.write(pkt) >= 0


def discover(pid=None):
    """0xFF43 벤더 페이지를 가진 직결 기기만."""
    seen = {}
    for d in hid.enumerate(LOGITECH_VID, 0):
        if (d.get("usage_page") or 0) != VENDOR_BLE:
            continue
        if pid and d["product_id"] != pid:
            continue
        seen.setdefault(d["path"],
                        (d["product_id"], d.get("product_string") or "?"))
    return [BtDevice(p, pid_, prod) for p, (pid_, prod) in seen.items()]


def load_all(pid=None, verbose=True):
    devs = []
    for dev in discover(pid):
        try:
            dev.open()
        except Exception as e:
            if verbose:
                print("  %s 열기 실패: %s" % (dev.product, e))
                print("     Logi Options+ 가 이미 열어둔 기기를 배타 점유로")
                print("     열려다 실패했을 수 있습니다. allow_shared_open() 확인.")
            continue
        ver = dev.ping()
        if not ver:
            if verbose:
                print("  %s 응답 없음" % dev.product)
            dev.close()
            continue
        dev.load()
        devs.append(dev)
    return devs


def show(dev):
    cur = "채널%d" % (dev.cur_host + 1) if dev.cur_host is not None else "?"
    print("  %-34s 현재 %s  ChangeHost=%s"
          % (dev.label, cur,
             "0x%02X" % dev.ch_idx if dev.ch_idx else "없음"))
    for h in sorted(dev.desc):
        mark = " <- 현재" if h == dev.cur_host else ""
        print("      채널%d  %s%s" % (h + 1, hexs(dev.desc[h]), mark))


def cmd_probe(args):
    allow_shared_open()
    devs = load_all(args.pid)
    if not devs:
        sys.exit("직결 기기를 못 찾았습니다.")
    print("직결 기기 %d개\n" % len(devs))
    for d in devs:
        show(d)
        print()
    kbs = [d for d in devs if d.tcode == TYPE_KEYBOARD]
    mice = [d for d in devs if d.tcode == TYPE_MOUSE]
    if kbs and mice:
        kb, ms = kbs[0], mice[0]
        print("키보드 채널 -> 마우스 채널")
        for kh in sorted(kb.desc):
            m = ms.hosts_with(kb.desc[kh])
            print("  채널%d -> %s"
                  % (kh + 1,
                     ", ".join("채널%d" % (x + 1) for x in m) if m else "대응 없음"))
    elif not kbs:
        print("키보드를 못 찾았습니다. 입력 모니터링 권한을 확인하세요.")
    for d in devs:
        d.close()


def cmd_switch(args):
    allow_shared_open()
    devs = load_all(args.pid)
    if args.type == "mouse":
        devs = [d for d in devs if d.tcode == TYPE_MOUSE]
    elif args.type == "keyboard":
        devs = [d for d in devs if d.tcode == TYPE_KEYBOARD]
    devs = [d for d in devs if d.ch_idx]
    if not devs:
        sys.exit("전환 가능한 기기가 없습니다.")
    for d in devs:
        if d.n_hosts and args.channel > d.n_hosts:
            print("  %s : 채널 %d 없음 (최대 %d)"
                  % (d.label, args.channel, d.n_hosts))
            continue
        if d.cur_host == args.channel - 1:
            print("  %s : 이미 채널 %d" % (d.label, args.channel))
            continue
        print("  %s -> 채널 %d" % (d.label, args.channel))
        d.switch(args.channel, dry=args.dry_run)
        time.sleep(0.25)
    for d in devs:
        d.close()


def cmd_follow(args):
    allow_shared_open()
    devs = load_all(args.pid)
    kbs = [d for d in devs if d.tcode == TYPE_KEYBOARD]
    mice = [d for d in devs if d.tcode == TYPE_MOUSE and d.ch_idx]
    if not kbs:
        for d in devs:
            d.close()
        sys.exit("키보드를 못 찾았습니다.\n"
                 "Options+ 와 경합 중일 수 있습니다. allow_shared_open() 확인.")
    if not mice:
        for d in devs:
            d.close()
        sys.exit("마우스를 못 찾았습니다.")
    kb, ms = kbs[0], mice[0]
    for d in devs:
        if d is not kb and d is not ms:
            d.close()

    print("감시   : %s" % kb.label)
    print("따라감 : %s" % ms.label)
    show(kb)
    show(ms)
    if args.dry_run:
        print("*** dry-run ***")
    print("\n" + "=" * 56)
    print("키보드 Easy-Switch 키를 누르세요.  (Ctrl-C 종료)")
    print("=" * 56 + "\n")

    try:
        kb.h.set_nonblocking(1)
        while True:
            try:
                r = kb.h.read(64)
            except OSError as e:
                # 키보드가 떠나면서 링크가 끊겼다. 이 시점엔 마우스 전송이
                # 이미 끝나 있다 — ChangeHost 이벤트가 링크 끊김보다
                # 40~60ms 먼저 도착하기 때문이다. 핸들만 되살리면 된다.
                print("%s  키보드 링크 끊김(%s). 복귀를 기다립니다..." % (ts(), e))
                if not kb.reopen():
                    print("키보드가 돌아오지 않았습니다. 종료합니다.")
                    break
                print("%s  키보드 재연결. 현재 채널%s"
                      % (ts(), kb.cur_host + 1 if kb.cur_host is not None else "?"))
                kb.h.set_nonblocking(1)
                continue
            if not r:
                time.sleep(0.005)
                continue
            r = bytes(r)
            if len(r) < 5 or r[0] != REPORT_LONG or r[1] != DIRECT:
                continue
            if (r[3] & 0x0F) != 0x00 or r[2] != kb.ch_idx:
                continue          # ChangeHost 이벤트가 아니다

            src, dst = r[4], r[5]
            print("%s  %s" % (ts(), hexs(r[:8])))
            print("      ChangeHost: 채널%d -> 채널%d" % (src + 1, dst + 1))

            target = kb.desc.get(dst)
            if target is None:
                print("      키보드 채널%d 디스크립터 없음\n" % (dst + 1))
                continue
            if ms.cur_host is not None and ms.desc.get(ms.cur_host) == target:
                print("      마우스는 이미 같은 호스트\n")
                continue
            m = ms.hosts_with(target)
            if not m:
                print("      마우스에 대응 채널 없음: %s\n" % hexs(target))
                continue

            print("      마우스 -> 채널 %d" % (m[0] + 1))
            if ms.switch(m[0] + 1, dry=args.dry_run) and not args.dry_run:
                ms.cur_host = m[0]
            print()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        kb.close()
        ms.close()


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--pid", type=lambda x: int(x, 0))
    pre.add_argument("--dry-run", action="store_true")
    pre.add_argument("--type", choices=["mouse", "keyboard", "all"],
                     default="all")
    opts, rest = pre.parse_known_args()

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="직결 기기 조회")
    sw = sub.add_parser("switch", help="채널 전환")
    sw.add_argument("channel", type=int)
    sub.add_parser("follow", help="키보드를 따라 마우스 전환")

    args = p.parse_args(rest)
    for k, v in vars(opts).items():
        setattr(args, k, v)
    {"probe": cmd_probe, "switch": cmd_switch, "follow": cmd_follow}[args.cmd](args)


if __name__ == "__main__":
    main()
