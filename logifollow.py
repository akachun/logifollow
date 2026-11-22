#!/usr/bin/env python3
"""logifollow — 키보드가 채널을 옮기면 마우스도 따라간다.

로지텍 Bolt/Unifying 리시버 및 블루투스 직결(BLE++ 커스텀 GATT) 지원.
macOS / Windows / Linux.

전송 방식(--mode):
  auto (기본) : USB 리시버가 있으면 리시버 모드, 없으면 BLE++ 직결 모드
  usb         : Bolt/Unifying 리시버(0xFF00) 전용
  ble         : 블루투스 직결(BLE++ GATT 00010000-...) 전용

사용법:
  python logifollow.py map              채널 대응표 확인
  python logifollow.py follow           상시 감시 (이게 본 기능)
  python logifollow.py switch 3         마우스를 채널3 으로 (수동)
  python logifollow.py --mode ble follow
"""

import argparse
import ctypes
import datetime
import os
import sys
import threading
import time

try:
    import hid
except ImportError:
    hid = None

# ---- 프로토콜 상수 (전부 실측으로 확정된 값) ----------------------------

LOGITECH_VID = 0x046D
VENDOR_RECEIVER = 0xFF00          # 리시버 경유. 블루투스 직결은 0xFF43
REPORT_SHORT, REPORT_LONG = 0x10, 0x11
SHORT_LEN, LONG_LEN = 7, 20
SW_ID = 0x05                       # 0 이 아니어야 한다. 응답의 swID 가 0이면 이벤트
RECEIVER_IDX = 0xFF

SUB_SET_REGISTER, SUB_GET_REGISTER = 0x80, 0x81
REG_NOTIFICATIONS = 0x00
FLAG_WIRELESS, FLAG_SW_PRESENT = 0x000100, 0x000800
NOTIF_DISCONNECTION, NOTIF_CONNECTION = 0x40, 0x41
LINK_DOWN_BIT = 0x40

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
                4: "터치패드", 5: "트랙볼", 6: "프리젠터", 7: "리시버"}
NOTIF_KIND = {1: "키보드", 2: "마우스", 3: "숫자패드", 8: "트랙볼", 9: "터치패드"}

DISCOVER_TIMEOUT_MS = 2500        # 절전 중인 마우스를 기다려야 한다
NORMAL_TIMEOUT_MS = 600

RAW = False                       # map --raw : 원시 응답까지 찍는다


# ---- 출력 --------------------------------------------------------------

_LOG = None
_OUT_LOCK = threading.Lock()


def out(s=""):
    with _OUT_LOCK:
        print(s, flush=True)
        if _LOG:
            _LOG.write(s + "\n")
            _LOG.flush()


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hexs(b):
    return " ".join("%02X" % x for x in b)


def chan(host):
    return "채널%d" % (host + 1) if host is not None else "?"


def allow_shared_open():
    """macOS 에서 hidapi 의 배타 점유(seize)를 끈다.

    hidapi 0.12+ 는 macOS 에서 기기를 seize 로 연다. Logi Options+ 가 이미
    열어둔 기기는 그대로는 못 연다. 에러가 그냥 'open failed' 라 권한 문제로
    오해하기 쉽다. 권한과 무관하다.
    """
    if sys.platform != "darwin":
        return
    try:
        ctypes.CDLL(hid.__file__).hid_darwin_set_open_exclusive(ctypes.c_int(0))
    except Exception as e:
        out("비배타 모드 전환 실패(계속 진행): %s" % e)


def build(report_id, index, b2, b3, params):
    ln = SHORT_LEN if report_id == REPORT_SHORT else LONG_LEN
    return bytes(([report_id, index, b2, b3] + list(params) + [0] * ln)[:ln])


def request(report_id, index, feat_idx, func, params, sw_id=SW_ID):
    return build(report_id, index, feat_idx, ((func & 0x0F) << 4) | (sw_id & 0x0F), params)


# 요청마다 swID 를 돌려 쓴다. 같은 feature/function 을 연달아 부를 때
# 앞 요청의 늦은 응답이 뒤 요청의 것으로 오인되는 것을 막는 유일한
# 확실한 방법이다. 0x0B 는 Logi Options+ 가 쓰므로 뺀다.
SW_IDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15]


# ---- 전송 계층 ---------------------------------------------------------

class Receiver:
    """리시버의 HID++ 인터페이스.

    윈도우는 HID 상위 컬렉션을 각각 별개의 디바이스 경로로 노출한다.
    short(0x10) 와 long(0x11) 이 서로 다른 핸들로 갈릴 수 있고, 엉뚱한
    핸들에 쓰면 hidapi 가 리포트 길이에 맞춰 패딩해버려 조용히 무시된다.
    그래서 크기별로 어느 핸들이 실제로 통하는지 왕복으로 확인해 기억한다.
    macOS/Linux 에서는 보통 한 핸들이 둘 다 처리한다.
    """

    def __init__(self):
        self.handles = []            # [(usage, hid.device, path)]
        self.route = {}              # report_id -> hid.device
        self.product_id = None

    # -- 열기 --

    @classmethod
    def open(cls, verbose=True):
        if hid is None:
            if verbose:
                out("hidapi 가 없습니다:  pip install hidapi")
            return None
        allow_shared_open()
        entries = {}
        for d in hid.enumerate(LOGITECH_VID, 0):
            if (d.get("usage_page") or 0) != VENDOR_RECEIVER:
                continue
            entries.setdefault(d["path"], d)
        if not entries:
            if verbose:
                out("로지텍 리시버를 찾지 못했습니다. USB 연결을 확인하세요.")
            return None

        r = cls()
        for path, meta in sorted(entries.items(), key=lambda kv: kv[1].get("usage") or 0):
            try:
                h = hid.device()
                h.open_path(path)
                h.set_nonblocking(1)
            except Exception as e:
                if verbose:
                    out("핸들 열기 실패(%s): %s" % (meta.get("usage"), e))
                continue
            r.handles.append((meta.get("usage") or 0, h, path))
            r.product_id = meta["product_id"]
        if not r.handles:
            return None

        if not r._calibrate_short():
            if verbose:
                out("리시버가 HID++ 에 응답하지 않습니다.")
            r.close()
            return None
        if verbose:
            out("리시버 PID=0x%04X  핸들 %d개" % (r.product_id, len(r.handles)))
        return r

    def _calibrate_short(self):
        """short(0x10) 를 받아주는 핸들을 찾는다. 알림 레지스터 읽기로 확인."""
        pkt = build(REPORT_SHORT, RECEIVER_IDX, SUB_GET_REGISTER,
                    REG_NOTIFICATIONS, [0, 0, 0])
        for _usage, h, _path in self.handles:
            self.drain()
            try:
                if h.write(pkt) < 0:
                    continue
            except Exception:
                continue
            if self._await(400, lambda r: r[1] == RECEIVER_IDX
                           and r[2] == SUB_GET_REGISTER):
                self.route[REPORT_SHORT] = h
                self.route.setdefault(REPORT_LONG, h)
                return True
        return False

    def calibrate_long(self, slot):
        """long(0x11) 이 통하는 핸들을 확정한다. 살아있는 슬롯이 필요하다.

        macOS 는 이미 맞지만 윈도우는 여기서 갈린다.
        """
        cur = self.route.get(REPORT_LONG)
        pkt = request(REPORT_LONG, slot, 0x00, IROOT_PING, [0, 0, 0xAA])
        order = ([cur] if cur else []) + [h for _u, h, _p in self.handles if h is not cur]
        for h in order:
            self.drain()
            try:
                if h.write(pkt) < 0:
                    continue
            except Exception:
                continue
            if self._await(500, lambda r: r[1] == slot and r[0] == REPORT_LONG):
                self.route[REPORT_LONG] = h
                return True
        return False

    # -- 입출력 --

    def write(self, pkt):
        h = self.route.get(pkt[0])
        if h is None:
            return False
        try:
            return h.write(pkt) >= 0
        except Exception:
            return False

    def read(self, timeout_ms):
        """어느 핸들에서든 먼저 오는 리포트를 돌려준다."""
        end = time.time() + timeout_ms / 1000.0
        while True:
            for _usage, h, _path in self.handles:
                try:
                    r = h.read(64)
                except OSError:
                    raise
                if r:
                    return bytes(r)
            if time.time() >= end:
                return None
            time.sleep(0.002)

    def drain(self):
        for _usage, h, _path in self.handles:
            try:
                while h.read(64):
                    pass
            except Exception:
                pass

    def _await(self, timeout_ms, match):
        end = time.time() + timeout_ms / 1000.0
        while time.time() < end:
            try:
                r = self.read(max(1, int((end - time.time()) * 1000)))
            except OSError:
                return False
            if r and len(r) >= 4 and r[0] in (REPORT_SHORT, REPORT_LONG) and match(r):
                return True
        return False

    def close(self):
        for _usage, h, _path in self.handles:
            try:
                h.close()
            except Exception:
                pass
        self.handles = []
        self.route = {}


# ---- 프로토콜 계층 -----------------------------------------------------

class Device:
    def __init__(self, slot):
        self.slot = slot
        self.name = None
        self.tcode = None
        self.ch_idx = 0
        self.hi_idx = 0
        self.n_hosts = None
        self.cur_host = None
        self.desc = {}               # host index -> hex 문자열

    @property
    def label(self):
        return "%s(%s)" % (self.name or "?", DEVICE_TYPES.get(self.tcode, "?"))

    def hosts_with(self, descriptor):
        return [h for h, d in self.desc.items() if d == descriptor]


class Session:
    """요청/응답. 대기 중 도착한 이벤트는 버리지 않고 쌓아둔다.

    채널 전환 이벤트가 다른 조회와 겹쳐 유실되면 이 프로그램은 통째로
    쓸모없어진다.
    """

    def __init__(self, rx):
        self.rx = rx
        self.pending = []
        self._sw = 0

    def _next_sw(self):
        self._sw = (self._sw + 1) % len(SW_IDS)
        return SW_IDS[self._sw]

    def _flush_stale(self):
        """요청 직전에 큐에 남은 것을 치운다.

        앞 요청이 타임아웃된 뒤 늦게 온 응답이 남아 있으면 곤란하다.
        이벤트는 절대 버리면 안 되므로 pending 으로 옮기고 지난 응답만 버린다.
        """
        while True:
            r = self.rx.read(0)
            if not r:
                return
            if len(r) >= 4 and self._is_event(r) and len(self.pending) < 64:
                self.pending.append(r)

    def read(self, timeout_ms):
        if self.pending:
            return self.pending.pop(0)
        return self.rx.read(timeout_ms)

    @staticmethod
    def _is_event(r):
        if r[2] in (NOTIF_CONNECTION, NOTIF_DISCONNECTION):
            return True
        return (r[3] & 0x0F) == 0x00 and r[2] < 0x80

    def transact(self, index, feat_idx, func, params,
                 report_id=REPORT_SHORT, timeout_ms=NORMAL_TIMEOUT_MS, echo=None):
        """echo 를 주면 payload 첫 바이트가 그 값인 응답만 받는다.

        같은 feature 의 같은 function 을 인자만 바꿔 연달아 부를 때
        (호스트 디스크립터를 채널별로 읽을 때가 그렇다) 앞 요청의 늦은
        응답과 구분할 방법이 feat_idx/func/swID 뿐이라 표가 한 칸씩
        밀릴 수 있다. 0x1815 응답은 첫 바이트로 호스트 번호를 되돌려주므로
        그것으로 가린다.
        """
        self._flush_stale()
        sw = self._next_sw()
        if not self.rx.write(request(report_id, index, feat_idx, func, params, sw)):
            return None
        want = ((func & 0x0F) << 4) | sw
        end = time.time() + timeout_ms / 1000.0
        while True:
            left = int((end - time.time()) * 1000)
            if left <= 0:
                return None
            r = self.rx.read(left)
            if not r or len(r) < 4:
                continue
            if r[1] != index or self._is_event(r):
                if len(self.pending) < 64:
                    self.pending.append(r)
                continue
            if r[2] in (ERR_V2, ERR_V1):
                return None
            if r[2] == feat_idx and r[3] == want:
                if echo is not None and r[4] != echo:
                    continue                     # 이전 요청의 늦은 응답이다
                return r

    def feature(self, index, fid):
        r = self.transact(index, 0x00, IROOT_GET_FEATURE,
                          [(fid >> 8) & 0xFF, fid & 0xFF, 0])
        return r[4] if r else 0

    def ping(self, slot, timeout_ms):
        return self.transact(slot, 0x00, IROOT_PING, [0, 0, 0xAA],
                             timeout_ms=timeout_ms) is not None

    def read_slot(self, slot):
        d = Device(slot)
        ni = self.feature(slot, FEAT_DEVICE_NAME)
        if ni:
            c = self.transact(slot, ni, NAME_GET_COUNT, [0, 0, 0])
            if c:
                count, txt, pos = c[4], "", 0
                while pos < count and pos < 64:
                    nr = self.transact(slot, ni, NAME_GET_NAME, [pos, 0, 0],
                                       report_id=REPORT_LONG)
                    if not nr:
                        break
                    txt += bytes(nr[4:20])[:min(16, count - pos)] \
                        .decode("ascii", "replace")
                    pos += 16
                d.name = txt.strip()
            t = self.transact(slot, ni, NAME_GET_TYPE, [0, 0, 0])
            if t:
                d.tcode = t[4]
        d.ch_idx = self.feature(slot, FEAT_CHANGE_HOST)
        d.hi_idx = self.feature(slot, FEAT_HOSTS_INFO)
        if d.ch_idx:
            r = self.transact(slot, d.ch_idx, CH_GET_HOST_INFO, [0, 0, 0])
            if r:
                d.n_hosts, d.cur_host = r[4], r[5]
        if d.hi_idx and d.n_hosts:
            for host in range(d.n_hosts):
                r = self.transact(slot, d.hi_idx, HI_GET_HOST_DESC, [host, 0, 0],
                                  report_id=REPORT_LONG, echo=host)
                if r is None:
                    # 막 재연결된 기기는 첫 요청에 느리다. 조금 더 기다려 한 번 더.
                    # 이벤트 처리가 밀리지 않게 대기는 1.2초로 묶는다.
                    r = self.transact(slot, d.hi_idx, HI_GET_HOST_DESC, [host, 0, 0],
                                      report_id=REPORT_LONG, echo=host, timeout_ms=1200)
                if RAW:
                    out("    raw 슬롯%d host%d -> %s"
                        % (slot, host, hexs(r[:16]) if r else "(응답 없음)"))
                if r:
                    d.desc[host] = hexs(bytes(r[5:13]).rstrip(b"\x00"))
                else:
                    out("  ! 슬롯%d %s 디스크립터를 못 읽었습니다"
                        % (slot, chan(host)))
        return d

    def scan(self, need=(), tries=4):
        """슬롯 1~6 을 훑는다. 슬롯 번호는 재페어링하면 바뀌므로 고정하지 않는다."""
        devs = {}
        long_ok = False
        for attempt in range(tries):
            timeout = NORMAL_TIMEOUT_MS * 2 if attempt == 0 else DISCOVER_TIMEOUT_MS
            for slot in range(1, 7):
                if slot in devs:
                    continue
                if not self.ping(slot, timeout):
                    continue
                if not long_ok:
                    # 살아있는 슬롯이 생겼으니 long 경로를 확정한다 (윈도우 대비)
                    long_ok = self.rx.calibrate_long(slot)
                devs[slot] = self.read_slot(slot)
                out("  슬롯 %d = %s" % (slot, devs[slot].label))
            have = {d.tcode for d in devs.values()}
            if all(t in have for t in need):
                return devs
            if attempt < tries - 1:
                miss = "/".join(DEVICE_TYPES.get(t, str(t)) for t in need if t not in have)
                out("  %s 를 못 찾았습니다. 움직여서 깨워주세요. (%d/%d)"
                    % (miss, attempt + 2, tries))
                time.sleep(1.2)
        return devs

    def get_flags(self):
        if not self.rx.write(build(REPORT_SHORT, RECEIVER_IDX, SUB_GET_REGISTER,
                                   REG_NOTIFICATIONS, [0, 0, 0])):
            return None
        end = time.time() + 0.7
        while time.time() < end:
            r = self.rx.read(int((end - time.time()) * 1000) + 1)
            if not r:
                continue
            if len(r) >= 7 and r[1] == RECEIVER_IDX and r[2] == SUB_GET_REGISTER:
                return (r[4] << 16) | (r[5] << 8) | r[6]
            self.pending.append(r)
        return None

    def set_flags(self, flags):
        self.rx.write(build(REPORT_SHORT, RECEIVER_IDX, SUB_SET_REGISTER,
                            REG_NOTIFICATIONS,
                            [(flags >> 16) & 0xFF, (flags >> 8) & 0xFF, flags & 0xFF]))

    def set_host(self, dev, host):
        """전환. 기기가 즉시 링크를 끊으므로 응답을 기다리지 않는다."""
        return self.rx.write(request(REPORT_SHORT, dev.slot, dev.ch_idx,
                                     CH_SET_CURRENT_HOST, [host, 0, 0],
                                     self._next_sw()))


# ---- 표시 --------------------------------------------------------------

def show(devs):
    for slot, d in sorted(devs.items()):
        out("  슬롯 %d = %-32s 현재 %s  ChangeHost=0x%02X"
            % (slot, d.label, chan(d.cur_host), d.ch_idx))
        for h in sorted(d.desc):
            out("           %s  %s%s" % (chan(h), d.desc[h],
                                         "  <- 현재" if h == d.cur_host else ""))


def mapping(kb, ms):
    out("\n키보드 채널 -> 마우스 채널  (무선 엔드포인트 식별자 대조)")
    issues = []
    for kh in sorted(kb.desc):
        m = ms.hosts_with(kb.desc[kh])
        if not m:
            out("  키보드 %s  ->  대응 없음" % chan(kh))
            issues.append("키보드 %s 에 대응하는 마우스 채널이 없습니다" % chan(kh))
        elif len(m) == 1:
            out("  키보드 %s  ->  마우스 %s" % (chan(kh), chan(m[0])))
        else:
            out("  키보드 %s  ->  마우스 %s (중복)"
                % (chan(kh), ", ".join(chan(x) for x in m)))
            issues.append("마우스 채널이 같은 호스트에 중복 페어링돼 있습니다")
    for i in sorted(set(issues)):
        out("  ! %s" % i)
    return issues


def find_pair(devs):
    kb = next((d for d in devs.values() if d.tcode == TYPE_KEYBOARD and d.ch_idx), None)
    ms = next((d for d in devs.values() if d.tcode == TYPE_MOUSE and d.ch_idx), None)
    return kb, ms


# ---- 명령 --------------------------------------------------------------

def cmd_map(args):
    rx = Receiver.open()
    if not rx:
        return 1
    try:
        s = Session(rx)
        devs = s.scan(need=(TYPE_KEYBOARD, TYPE_MOUSE))
        if not devs:
            out("리시버에 붙은 기기가 없습니다.")
            return 1
        show(devs)
        kb, ms = find_pair(devs)
        if kb and ms:
            mapping(kb, ms)
        else:
            out("\n키보드와 마우스가 모두 이 리시버 채널에 있어야 합니다.")
        return 0
    finally:
        rx.close()


def cmd_switch(args):
    rx = Receiver.open()
    if not rx:
        return 1
    try:
        s = Session(rx)
        devs = s.scan(need=(TYPE_MOUSE,))
        _kb, ms = find_pair(devs)
        if not ms:
            out("마우스를 찾지 못했습니다. 움직여서 깨운 뒤 다시 시도하세요.")
            return 1
        host = args.channel - 1
        if ms.n_hosts and host >= ms.n_hosts:
            out("마우스는 채널 %d 까지만 있습니다." % ms.n_hosts)
            return 1
        if ms.cur_host == host:
            out("마우스는 이미 %s 입니다." % chan(host))
            return 0
        ok = s.set_host(ms, host)
        out("마우스 -> %s  %s" % (chan(host), "전송함" if ok else "전송 실패"))
        return 0 if ok else 1
    finally:
        rx.close()


def cmd_follow(args, stop_event=None):
    rx = Receiver.open()
    if not rx:
        return 1
    orig = None
    s = Session(rx)
    try:
        # 기기가 다른 채널에 가 있으면 돌아올 때까지 기다린다.
        # 여기서 종료해버리면 launchd/작업스케줄러가 다시 띄울 때까지
        # 공백이 생겨서, 돌아오자마자 누른 전환을 놓친다.
        while not (stop_event and stop_event.is_set()):
            devs = s.scan(need=(TYPE_KEYBOARD, TYPE_MOUSE))
            kb, ms = find_pair(devs)
            if kb and ms:
                break
            show(devs)
            out("%s  키보드와 마우스가 모두 이 리시버 채널에 있어야 합니다."
                " 20초 후 다시 찾습니다." % ts())
            for _ in range(40):
                if stop_event and stop_event.is_set():
                    return 0
                time.sleep(0.5)
        if stop_event and stop_event.is_set():
            return 0
        show(devs)
        mapping(kb, ms)

        orig = s.get_flags()
        if orig is not None:
            want = orig | FLAG_WIRELESS | FLAG_SW_PRESENT
            if want != orig:
                s.set_flags(want)

        out("\n감시   : %s (슬롯 %d)" % (kb.label, kb.slot))
        out("따라감 : %s (슬롯 %d)" % (ms.label, ms.slot))
        if args.dry_run:
            out("*** 시험 모드 — 실제로 전송하지 않습니다 ***")
        out("=" * 58)
        out("키보드 Easy-Switch 키를 누르세요.  (Ctrl-C 종료)")
        out("=" * 58 + "\n")

        while not (stop_event and stop_event.is_set()):
            try:
                r = s.read(200)
            except OSError as e:
                out("%s  읽기 오류(%s). 리시버를 다시 엽니다..." % (ts(), e))
                rx.close()
                rx = None
                for _ in range(60):
                    if stop_event and stop_event.is_set():
                        return 0
                    time.sleep(1.0)
                    rx = Receiver.open(verbose=False)
                    if rx:
                        break
                if not rx:
                    out("리시버를 다시 열지 못했습니다.")
                    return 1
                s = Session(rx)
                fresh = s.scan(need=(TYPE_KEYBOARD, TYPE_MOUSE))
                k2, m2 = find_pair(fresh)
                if k2 and m2:
                    kb, ms = k2, m2
                out("%s  리시버를 다시 열었습니다." % ts())
                continue
            if not r or len(r) < 4:
                continue
            handle(s, r, kb, ms, args)
        return 0
    except KeyboardInterrupt:
        out("\n종료합니다.")
        return 0
    finally:
        if rx and orig is not None:
            try:
                s.set_flags(orig)
            except Exception:
                pass
        if rx:
            rx.close()


def handle(s, r, kb, ms, args):
    slot, b2, b3 = r[1], r[2], r[3]

    if b2 in (NOTIF_CONNECTION, NOTIF_DISCONNECTION):
        st = r[4]
        who = kb.label if slot == kb.slot else (ms.label if slot == ms.slot else "슬롯%d" % slot)
        out("%s  [알림] %s 링크 %s (%s)"
            % (ts(), who, "끊김" if st & LINK_DOWN_BIT else "연결됨",
               NOTIF_KIND.get(st & 0x0F, "?")))
        if not (st & LINK_DOWN_BIT) and slot == ms.slot:
            fresh = s.read_slot(ms.slot)
            if fresh.cur_host is not None:
                ms.cur_host = fresh.cur_host
            # 통째로 교체하면 안 된다. 방금 깨어난 기기는 일부 채널만
            # 읽히는 일이 있는데, 그때 이미 알고 있던 값을 지워버리면
            # 하필 실패한 그 채널의 대응을 잃는다. 병합만 한다.
            ms.desc.update(fresh.desc)
            out("           마우스 현재 %s" % chan(ms.cur_host))
        return

    if (b3 & 0x0F) != 0x00 or b2 >= 0x80:
        return                                   # 요청 응답이지 이벤트가 아니다

    if slot == ms.slot and b2 == ms.ch_idx and len(r) >= 6:
        ms.cur_host = r[5]
        out("%s  [이벤트] 마우스가 스스로 %s -> %s"
            % (ts(), chan(r[4]), chan(r[5])))
        return

    if slot != kb.slot or b2 != kb.ch_idx or len(r) < 6:
        return

    # 트리거. 계산하고 곧바로 쏘고, 기록은 그다음이다.
    # 키보드가 떠나면서 링크 정리가 시작되므로 늑장 부리면 안 된다.
    src, dst = r[4], r[5]
    kb.cur_host = dst
    target = kb.desc.get(dst)
    m = ms.hosts_with(target) if target else []
    if not m and ms.n_hosts and dst < ms.n_hosts:
        m = [dst]                                # 식별자를 모르면 번호 그대로
    already = bool(m) and ms.cur_host == m[0]

    sent = False
    if m and not already and not args.dry_run:
        sent = s.set_host(ms, m[0])
        if sent:
            ms.cur_host = m[0]

    out("%s  [이벤트] 키보드 %s -> %s" % (ts(), chan(src), chan(dst)))
    if not m:
        out("           -> 대응하는 마우스 채널이 없습니다")
    elif already:
        out("           -> 마우스는 이미 %s 입니다" % chan(m[0]))
    elif args.dry_run:
        out("           -> [시험] 마우스 -> %s" % chan(m[0]))
    else:
        out("           => 마우스 -> %s  %s" % (chan(m[0]), "" if sent else "(전송 실패)"))


def _has_usb_receiver():
    if hid is None:
        return False
    allow_shared_open()
    try:
        return any(
            (d.get("usage_page") or 0) == VENDOR_RECEIVER
            for d in hid.enumerate(LOGITECH_VID, 0)
        )
    except Exception:
        return False


def main():
    global _LOG
    p = argparse.ArgumentParser(description="키보드를 따라 마우스 채널을 옮긴다")
    p.add_argument("--log", help="로그 파일 경로")
    p.add_argument("--mode", choices=("auto", "usb", "ble"), default="auto",
                   help="전송 방식: auto(기본, 리시버 우선 후 BLE++), usb, ble")
    sub = p.add_subparsers(dest="cmd", required=True)
    mp = sub.add_parser("map", help="채널 대응표 확인")
    mp.add_argument("--raw", action="store_true",
                    help="호스트 디스크립터 원시 응답까지 출력 (진단용)")
    f = sub.add_parser("follow", help="상시 감시")
    f.add_argument("--dry-run", action="store_true", help="판단만 하고 전송하지 않음")
    sw = sub.add_parser("switch", help="마우스를 지정 채널로")
    sw.add_argument("channel", type=int, choices=(1, 2, 3, 4, 5, 6))
    args = p.parse_args()

    if args.log:
        os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
        _LOG = open(args.log, "a", encoding="utf-8")
        out("\n===== %s 시작 =====" % datetime.datetime.now().isoformat(timespec="seconds"))

    global RAW
    RAW = getattr(args, "raw", False)
    if not hasattr(args, "dry_run"):
        args.dry_run = False

    if args.mode == "ble":
        import ble_transport
        return ble_transport.run_ble(args.cmd, args, out)

    if args.mode == "usb":
        fn = {"map": cmd_map, "follow": cmd_follow, "switch": cmd_switch}[args.cmd]
        return fn(args)

    # auto: USB 리시버와 BLE++ 를 모두 확인해 동시 지원한다
    has_usb = _has_usb_receiver()
    has_ble = False
    ble_transport = None
    try:
        import ble_transport as _bt
        if _bt.is_ble_available():
            ble_transport = _bt
            has_ble = True
    except Exception:
        pass

    if args.cmd == "map":
        rc_usb = 1
        rc_ble = 1
        if has_usb:
            out("== [USB 리시버] ==")
            rc_usb = cmd_map(args)
        if has_ble:
            if has_usb:
                out("\n== [블루투스 직결 (BLE++)] ==")
            rc_ble = ble_transport.run_ble("map", args, out)
        if not has_usb and not has_ble:
            return cmd_map(args)
        return 0 if (rc_usb == 0 or rc_ble == 0) else 1

    if args.cmd == "switch":
        if has_usb:
            rc = cmd_switch(args)
            if rc == 0 or not has_ble:
                return rc
        if has_ble:
            return ble_transport.run_ble("switch", args, out)
        return cmd_switch(args)

    # follow: USB 와 BLE++ 를 동시에 감시한다
    if has_usb and has_ble:
        stop_ev = threading.Event()
        out("== 하이브리드 감시 시작 (USB 리시버 + 블루투스 BLE++) ==")
        t_usb = threading.Thread(target=cmd_follow, args=(args, stop_ev),
                                 name="follow-usb", daemon=True)
        t_ble = threading.Thread(target=ble_transport.run_ble,
                                 args=("follow", args, out, stop_ev),
                                 name="follow-ble", daemon=True)
        t_usb.start()
        t_ble.start()
        try:
            while t_usb.is_alive() or t_ble.is_alive():
                time.sleep(0.3)
        except KeyboardInterrupt:
            out("\n종료합니다...")
            stop_ev.set()
            t_usb.join(timeout=3.0)
            t_ble.join(timeout=3.0)
        return 0

    if has_ble and not has_usb:
        return ble_transport.run_ble("follow", args, out)
    return cmd_follow(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
