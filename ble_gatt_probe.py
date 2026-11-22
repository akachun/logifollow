#!/usr/bin/env python3
"""ble_gatt_probe.py - macOS CoreBluetooth(BLE++) 기반 로지텍 진단 및 자동 추종.

원리:
  표준 BLE HID 서비스(0x1812)는 macOS/iOS CoreBluetooth 가 숨기지만,
  로지텍 벤더 커스텀 GATT 서비스(00010000-0000-1000-8000-011F2000046D)의
  특성(00010001-...)은 그대로 노출되며 18바이트 BLE++ 포맷
  ([featIdx, (func<<4)|swID, params(16B)])으로 HID++ 2.0/4.5 통신 및
  ChangeHost(0x1814) 비동기 Notify 수신이 가능하다.

사용법:
  uv run --with pyobjc-framework-CoreBluetooth --with pyobjc-framework-libdispatch ble_gatt_probe.py probe
  uv run --with pyobjc-framework-CoreBluetooth --with pyobjc-framework-libdispatch ble_gatt_probe.py sniff
  uv run --with pyobjc-framework-CoreBluetooth --with pyobjc-framework-libdispatch ble_gatt_probe.py follow --dry-run
  uv run --with pyobjc-framework-CoreBluetooth --with pyobjc-framework-libdispatch ble_gatt_probe.py follow
"""

import argparse
import datetime
import sys
import threading
import time

import objc
from Foundation import NSObject, NSData
from dispatch import dispatch_queue_create
import CoreBluetooth as CB

LOGI_SVC_UUID = "00010000-0000-1000-8000-011F2000046D"
LOGI_CHR_UUID = "00010001-0000-1000-8000-011F2000046D"

FEAT_DEVICE_NAME = 0x0005
FEAT_CHANGE_HOST = 0x1814
FEAT_HOSTS_INFO = 0x1815

TYPE_KEYBOARD, TYPE_MOUSE = 0, 3
DEVICE_TYPES = {0: "키보드", 1: "리모컨", 2: "숫자패드", 3: "마우스",
                4: "터치패드", 5: "트랙볼", 6: "프리젠터"}

# Options+ 가 쓰는 swID(0x0B, 0x0E)를 피해 순환
SW_IDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 15]

QUERY_SERVICES = [
    LOGI_SVC_UUID,
    "180F",
    "180A",
    "1812",
]

PROP_NAMES = [
    (CB.CBCharacteristicPropertyBroadcast, "broadcast"),
    (CB.CBCharacteristicPropertyRead, "read"),
    (CB.CBCharacteristicPropertyWriteWithoutResponse, "writeNoResp"),
    (CB.CBCharacteristicPropertyWrite, "write"),
    (CB.CBCharacteristicPropertyNotify, "notify"),
    (CB.CBCharacteristicPropertyIndicate, "indicate"),
]


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hexs(b):
    return " ".join("%02X" % x for x in b)


def chan(host):
    return "채널%d" % (host + 1) if host is not None else "?"


def fmt_props(props):
    out = [name for bit, name in PROP_NAMES if props & bit]
    return ",".join(out) if out else "none"


def make_nsdata(bs):
    return NSData.dataWithBytes_length_(bs, len(bs))


class BleDevice:
    def __init__(self, peripheral, char):
        self.peripheral = peripheral
        self.pid = str(peripheral.identifier().UUIDString())
        self.bt_name = peripheral.name() or "?"
        self.char = char
        self.connected = True
        self.name = None
        self.tcode = None
        self.ch_idx = 0
        self.hi_idx = 0
        self.n_hosts = 0
        self.cur_host = None
        self.desc = {}  # host(0-based) -> hex str

    @property
    def label(self):
        return "%s(%s)" % (self.name or self.bt_name, DEVICE_TYPES.get(self.tcode, "?"))

    def hosts_with(self, descriptor):
        return [h for h, d in self.desc.items() if d == descriptor]


class Delegate(NSObject):
    def initWithOptions_(self, opts):
        self = objc.super(Delegate, self).init()
        if self is None:
            return None
        self.mode = opts["mode"]
        self.dry_run = opts.get("dry_run", False)
        self.verbose = opts.get("verbose", False)
        self.central = None
        self.ready = False
        self.state_name = "unknown"
        self.lock = threading.Lock()
        self._sw = 0

        self.peripherals = {}        # pid -> CBPeripheral (strong ref)
        self.pending_services = {}   # pid -> remaining services count
        self.done_peripherals = set()
        self.devices = {}            # pid -> BleDevice
        self.ready_queue = []        # 새로 Notify 구독 완료된 pid 목록

        # transact 대기용: pid -> {"want": (feat_idx, func_sw), "echo": int|None, "event": Event, "resp": bytes|None}
        self.waiters = {}
        self.follow_active = False
        return self

    @objc.python_method
    def next_sw(self):
        with self.lock:
            self._sw = (self._sw + 1) % len(SW_IDS)
            return SW_IDS[self._sw]

    def centralManagerDidUpdateState_(self, central):
        st = central.state()
        names = {
            CB.CBManagerStateUnknown: "Unknown",
            CB.CBManagerStateResetting: "Resetting",
            CB.CBManagerStateUnsupported: "Unsupported",
            CB.CBManagerStateUnauthorized: "Unauthorized (블루투스 권한 필요)",
            CB.CBManagerStatePoweredOff: "PoweredOff",
            CB.CBManagerStatePoweredOn: "PoweredOn",
        }
        self.state_name = names.get(st, str(st))
        print("%s  CoreBluetooth 상태: %s" % (ts(), self.state_name), flush=True)
        if st == CB.CBManagerStatePoweredOn:
            self.ready = True
            self.scan_connected(initial=True)

    @objc.python_method
    def scan_connected(self, initial=False):
        if initial and self.verbose:
            print("\n[1] 시스템에 이미 연결된 BLE 기기 조회 (retrieveConnectedPeripherals)", flush=True)
            for suuid in QUERY_SERVICES:
                cbuuid = CB.CBUUID.UUIDWithString_(suuid)
                ps = self.central.retrieveConnectedPeripheralsWithServices_([cbuuid])
                print("  서비스 %-38s -> %d개" % (suuid, len(ps)), flush=True)
                for p in ps:
                    print("      + %s  [%s]" % (p.name() or "?", p.identifier().UUIDString()), flush=True)

        cbuuid = CB.CBUUID.UUIDWithString_(LOGI_SVC_UUID)
        ps = self.central.retrieveConnectedPeripheralsWithServices_([cbuuid])
        for p in ps:
            pid = str(p.identifier().UUIDString())
            with self.lock:
                dev = self.devices.get(pid)
                already_connected = (dev is not None and dev.connected) or (
                    pid in self.peripherals and p.state() in (CB.CBPeripheralStateConnected, CB.CBPeripheralStateConnecting)
                )
            if already_connected:
                continue
            with self.lock:
                self.peripherals[pid] = p
                self.done_peripherals.discard(pid)
            p.setDelegate_(self)
            self.central.connectPeripheral_options_(p, None)

    def centralManager_didConnectPeripheral_(self, central, peripheral):
        name = peripheral.name() or "?"
        print("%s  연결됨: %s" % (ts(), name), flush=True)
        if self.verbose:
            peripheral.discoverServices_(None)
        else:
            peripheral.discoverServices_([CB.CBUUID.UUIDWithString_(LOGI_SVC_UUID)])

    def centralManager_didFailToConnectPeripheral_error_(self, central, peripheral, error):
        pid = str(peripheral.identifier().UUIDString())
        print("%s  연결 실패: %s (%s)" % (ts(), peripheral.name() or "?", error), flush=True)
        with self.lock:
            self.done_peripherals.add(pid)
            self.peripherals.pop(pid, None)

    def centralManager_didDisconnectPeripheral_error_(self, central, peripheral, error):
        pid = str(peripheral.identifier().UUIDString())
        name = peripheral.name() or "?"
        print("%s  [연결 끊김] %s" % (ts(), name), flush=True)
        with self.lock:
            dev = self.devices.get(pid)
            if dev:
                dev.connected = False
            self.peripherals.pop(pid, None)

    def peripheral_didDiscoverServices_(self, peripheral, error):
        pid = str(peripheral.identifier().UUIDString())
        name = peripheral.name() or "?"
        if error:
            print("  [%s] 서비스 탐색 오류: %s" % (name, error), flush=True)
            with self.lock:
                self.done_peripherals.add(pid)
            return
        svcs = peripheral.services() or []
        if self.verbose:
            print("  [%s] 서비스 %d개:" % (name, len(svcs)), flush=True)
        if not svcs:
            with self.lock:
                self.done_peripherals.add(pid)
            return
        with self.lock:
            self.pending_services[pid] = len(svcs)
        for s in svcs:
            suuid = str(s.UUID().UUIDString())
            if self.verbose:
                mark = "  <=== LOGITECH VENDOR!" if suuid.upper() == LOGI_SVC_UUID else ""
                print("    Service %s%s" % (suuid, mark), flush=True)
            peripheral.discoverCharacteristics_forService_(None, s)

    def peripheral_didDiscoverCharacteristicsForService_error_(self, peripheral, service, error):
        pid = str(peripheral.identifier().UUIDString())
        suuid = str(service.UUID().UUIDString())
        if not error:
            for c in (service.characteristics() or []):
                cuuid = str(c.UUID().UUIDString())
                if self.verbose:
                    mark = "  <=== TARGET!" if cuuid.upper() == LOGI_CHR_UUID else ""
                    print("      Char %-38s [%s]%s" % (cuuid, fmt_props(c.properties()), mark), flush=True)
                if cuuid.upper() == LOGI_CHR_UUID:
                    with self.lock:
                        old = self.devices.get(pid)
                        if old:
                            old.peripheral = peripheral
                            old.char = c
                            old.connected = True
                        else:
                            self.devices[pid] = BleDevice(peripheral, c)
                    peripheral.setNotifyValue_forCharacteristic_(True, c)

        with self.lock:
            rem = self.pending_services.get(pid, 1) - 1
            self.pending_services[pid] = rem
            if rem <= 0:
                self.done_peripherals.add(pid)

    def peripheral_didUpdateNotificationStateForCharacteristic_error_(self, peripheral, char, error):
        pid = str(peripheral.identifier().UUIDString())
        name = peripheral.name() or "?"
        if error:
            print("%s  [%s] Notify 구독 실패: %s" % (ts(), name, error), flush=True)
            return
        if self.verbose:
            print("%s  [%s] Notify 구독 성공" % (ts(), name), flush=True)
        with self.lock:
            if pid not in self.ready_queue:
                self.ready_queue.append(pid)

    def peripheral_didUpdateValueForCharacteristic_error_(self, peripheral, char, error):
        if error or not char.value():
            return
        pid = str(peripheral.identifier().UUIDString())
        val = bytes(char.value())
        name = peripheral.name() or "?"

        if self.mode == "sniff":
            print("%s  [%s] <- NOTIFY (%dB) %s" % (ts(), name, len(val), hexs(val)), flush=True)

        # 1) 진행 중인 동기 요청(transact) 응답 매칭
        with self.lock:
            waiter = self.waiters.get(pid)
        if waiter and len(val) >= 2:
            want_fidx, want_fs = waiter["want"]
            if val[0] in (0x8F, 0xFF):
                waiter["resp"] = None
                waiter["event"].set()
                return
            if val[0] == want_fidx and val[1] == want_fs:
                echo = waiter.get("echo")
                if echo is None or (len(val) >= 3 and val[2] == echo):
                    waiter["resp"] = val
                    waiter["event"].set()
                    return

        # 2) 비동기 이벤트 처리 (follow 모드)
        if self.follow_active and len(val) >= 4:
            self._handle_event(pid, val)

    @objc.python_method
    def _handle_event(self, pid, val):
        fidx, b1 = val[0], val[1]
        if (b1 & 0x0F) != 0x00 or fidx >= 0x80:
            return  # 타 앱(Options+ 등) 응답 무시

        with self.lock:
            dev = self.devices.get(pid)
            devs = list(self.devices.values())

        if not dev:
            return

        # 마우스가 스스로 채널 변경한 경우 상태만 업데이트
        if dev.tcode == TYPE_MOUSE and fidx == dev.ch_idx:
            src, dst = val[2], val[3]
            dev.cur_host = dst
            print("%s  [이벤트] 마우스가 스스로 %s -> %s" % (ts(), chan(src), chan(dst)), flush=True)
            return

        # 키보드 ChangeHost 이벤트 -> 즉시 마우스 전환!
        if dev.tcode == TYPE_KEYBOARD and fidx == dev.ch_idx:
            src, dst = val[2], val[3]
            dev.cur_host = dst
            ms = next((d for d in devs if d.tcode == TYPE_MOUSE and d.ch_idx and d.connected), None)
            if not ms:
                print("%s  [이벤트] 키보드 %s -> %s  (연결된 마우스 없음)"
                      % (ts(), chan(src), chan(dst)), flush=True)
                return

            target = dev.desc.get(dst)
            m = ms.hosts_with(target) if target else []
            if not m and not target and ms.n_hosts and dst < ms.n_hosts:
                m = [dst]

            already = bool(m) and ms.cur_host == m[0]
            sent = False
            if m and not already and not self.dry_run:
                sent = self.send_set_host(ms, m[0])
                if sent:
                    ms.cur_host = m[0]

            print("%s  [이벤트] 키보드 %s -> %s" % (ts(), chan(src), chan(dst)), flush=True)
            if not m:
                print("           -> 대응하는 마우스 채널이 없습니다 (키보드 %s: %s)"
                      % (chan(dst), target or "?"), flush=True)
            elif already:
                print("           -> 마우스는 이미 %s 입니다" % chan(m[0]), flush=True)
            elif self.dry_run:
                print("           -> [시험] 마우스 -> %s" % chan(m[0]), flush=True)
            else:
                print("           => 마우스 -> %s  %s"
                      % (chan(m[0]), "전송함" if sent else "(전송 실패)"), flush=True)

    @objc.python_method
    def transact(self, dev, feat_idx, func, params, timeout=1.2, echo=None):
        sw = self.next_sw()
        fs = ((func & 0x0F) << 4) | (sw & 0x0F)
        pkt = bytes(([feat_idx, fs] + list(params) + [0] * 18)[:18])
        ev = threading.Event()
        waiter = {"want": (feat_idx, fs), "echo": echo, "event": ev, "resp": None}
        with self.lock:
            self.waiters[dev.pid] = waiter
        try:
            wtype = (CB.CBCharacteristicWriteWithResponse
                     if (dev.char.properties() & CB.CBCharacteristicPropertyWrite)
                     else CB.CBCharacteristicWriteWithoutResponse)
            dev.peripheral.writeValue_forCharacteristic_type_(make_nsdata(pkt), dev.char, wtype)
            if ev.wait(timeout):
                return waiter["resp"]
            return None
        finally:
            with self.lock:
                if self.waiters.get(dev.pid) is waiter:
                    self.waiters.pop(dev.pid, None)

    @objc.python_method
    def send_set_host(self, dev, host):
        """마우스 채널 전환. 지연 최소화를 위해 WriteWithoutResponse 우선 사용."""
        sw = self.next_sw()
        pkt = bytes(([dev.ch_idx, (0x01 << 4) | sw, host] + [0] * 18)[:18])
        wtype = (CB.CBCharacteristicWriteWithoutResponse
                 if (dev.char.properties() & CB.CBCharacteristicPropertyWriteWithoutResponse)
                 else CB.CBCharacteristicWriteWithResponse)
        try:
            dev.peripheral.writeValue_forCharacteristic_type_(make_nsdata(pkt), dev.char, wtype)
            return True
        except Exception:
            return False

    @objc.python_method
    def load_device(self, dev):
        r = self.transact(dev, 0x00, 0x01, [0, 0, 0xAA])
        if not r:
            return False
        # 이름 / 종류
        r_ni = self.transact(dev, 0x00, 0x00, [0x00, 0x05, 0])
        ni = r_ni[2] if r_ni else 0
        if ni:
            c = self.transact(dev, ni, 0x00, [0, 0, 0])
            if c:
                count, txt, pos = c[2], "", 0
                while pos < count and pos < 64:
                    nr = self.transact(dev, ni, 0x01, [pos, 0, 0])
                    if not nr:
                        break
                    txt += bytes(nr[2:18])[:min(16, count - pos)].decode("ascii", "replace")
                    pos += 16
                dev.name = txt.strip()
            t = self.transact(dev, ni, 0x02, [0, 0, 0])
            if t:
                dev.tcode = t[2]

        r_ch = self.transact(dev, 0x00, 0x00, [0x18, 0x14, 0])
        dev.ch_idx = r_ch[2] if r_ch else 0
        r_hi = self.transact(dev, 0x00, 0x00, [0x18, 0x15, 0])
        dev.hi_idx = r_hi[2] if r_hi else 0

        if dev.ch_idx:
            ri = self.transact(dev, dev.ch_idx, 0x00, [0, 0, 0])
            if ri:
                dev.n_hosts, dev.cur_host = ri[2], ri[3]

        if dev.hi_idx and dev.n_hosts:
            for h in range(dev.n_hosts):
                rd = self.transact(dev, dev.hi_idx, 0x02, [h, 0, 0], echo=h)
                if rd:
                    dev.desc[h] = hexs(bytes(rd[3:11]).rstrip(b"\x00"))
        return True


def show_devices(devs):
    for d in devs:
        print("  %-30s 현재 %s  ChangeHost=0x%02X"
              % (d.label, chan(d.cur_host), d.ch_idx), flush=True)
        for h in sorted(d.desc):
            mark = "  <- 현재" if h == d.cur_host else ""
            print("      %s  %s%s" % (chan(h), d.desc[h], mark), flush=True)

    kb = next((d for d in devs if d.tcode == TYPE_KEYBOARD and d.ch_idx), None)
    ms = next((d for d in devs if d.tcode == TYPE_MOUSE and d.ch_idx), None)
    if kb and ms:
        print("\n키보드 채널 -> 마우스 채널  (무선 엔드포인트 식별자 대조)", flush=True)
        for kh in sorted(kb.desc):
            m = ms.hosts_with(kb.desc[kh])
            if not m:
                print("  키보드 %s  ->  대응 없음" % chan(kh), flush=True)
            else:
                print("  키보드 %s  ->  마우스 %s" % (chan(kh), ", ".join(chan(x) for x in m)), flush=True)


def drain_ready_devices(delegate):
    loaded = []
    while True:
        with delegate.lock:
            if not delegate.ready_queue:
                break
            pid = delegate.ready_queue.pop(0)
            dev = delegate.devices.get(pid)
        if dev and dev.connected:
            if delegate.load_device(dev):
                loaded.append(dev)
    return loaded


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["probe", "sniff", "follow"], default="probe", nargs="?")
    ap.add_argument("--dry-run", action="store_true", help="follow 시 실제 전송 없이 판단만 출력")
    ap.add_argument("--seconds", type=int, default=30, help="sniff 대기 시간(초)")
    args = ap.parse_args()

    q = dispatch_queue_create(b"dev.logifollow.ble", None)
    delegate = Delegate.alloc().initWithOptions_({
        "mode": args.cmd,
        "dry_run": args.dry_run,
        "verbose": args.cmd in ("probe", "sniff"),
    })
    central = CB.CBCentralManager.alloc().initWithDelegate_queue_(delegate, q)
    delegate.central = central

    # PoweredOn 및 초기 연결 대기
    end = time.time() + 5.0
    while time.time() < end:
        time.sleep(0.05)
        with delegate.lock:
            done = delegate.ready and delegate.peripherals and len(delegate.done_peripherals) >= len(delegate.peripherals)
        if done:
            break

    if not delegate.ready:
        print("CoreBluetooth 가 PoweredOn 되지 않았습니다. 상태: %s" % delegate.state_name, flush=True)
        return 1

    time.sleep(0.5)
    drain_ready_devices(delegate)
    with delegate.lock:
        devs = list(delegate.devices.values())

    if not devs and args.cmd != "follow":
        print("로지텍 BLE++ 기기를 찾지 못했습니다. 키보드/마우스가 이 맥에 블루투스로 연결돼 있는지 확인하세요.", flush=True)
        return 1

    if devs:
        print("\n[기기 및 채널 대응표]", flush=True)
        show_devices(devs)
    else:
        print("\n현재 연결된 로지텍 BLE++ 기기가 없습니다. 기기가 이 맥 채널로 돌아오면 자동으로 연결합니다...", flush=True)

    if args.cmd == "probe":
        pass
    elif args.cmd == "sniff":
        print("\n" + "=" * 60, flush=True)
        print("Easy-Switch 키를 눌러보세요 (%d초간 감시, Ctrl-C 종료)" % args.seconds, flush=True)
        print("=" * 60 + "\n", flush=True)
        try:
            time.sleep(args.seconds)
        except KeyboardInterrupt:
            pass
    elif args.cmd == "follow":
        delegate.follow_active = True
        print("\n" + "=" * 60, flush=True)
        if args.dry_run:
            print("*** 시험 모드 — 실제로 마우스를 전환하지 않습니다 ***", flush=True)
        print("블루투스 직결 상시 감시 시작! 키보드 Easy-Switch 키를 누르세요. (Ctrl-C 종료)", flush=True)
        print("=" * 60 + "\n", flush=True)
        try:
            last_scan = time.time()
            while True:
                time.sleep(0.2)
                # 돌아온 기기 자동 재연결
                if time.time() - last_scan >= 1.5:
                    delegate.scan_connected(initial=False)
                    last_scan = time.time()
                reloaded = drain_ready_devices(delegate)
                for d in reloaded:
                    print("%s  [복귀] %s  현재 %s" % (ts(), d.label, chan(d.cur_host)), flush=True)
        except KeyboardInterrupt:
            print("\n종료합니다.", flush=True)

    with delegate.lock:
        ps = list(delegate.peripherals.values())
    for p in ps:
        central.cancelPeripheralConnection_(p)
    time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
