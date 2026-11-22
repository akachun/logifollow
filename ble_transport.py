#!/usr/bin/env python3
"""ble_transport.py - 로지텍 BLE++ 커스텀 GATT(00010000-...) 기반 map/follow/switch.

표준 BLE HID 서비스(0x1812) 대신 로지텍 벤더 커스텀 GATT 서비스
(00010000-0000-1000-8000-011F2000046D / 특성 00010001-...)를 사용한다.
18바이트 BLE++ 패킷([featIdx, (func<<4)|swID, params(16B)])으로
HID++ 2.0/4.5 조회, ChangeHost(0x1814) Notify 수신, 마우스 전환을 수행한다.

지원 백엔드:
  - macOS : CoreBluetooth (pyobjc-framework-CoreBluetooth + pyobjc-framework-libdispatch)
            실기기(MX Keys Mini + Logi M750) 동작 확인 완료.
  - Windows / Linux : bleak (선택적, 미실측)
"""

import asyncio
import datetime
import sys
import threading
import time

LOGI_SVC_UUID = "00010000-0000-1000-8000-011F2000046D"
LOGI_CHR_UUID = "00010001-0000-1000-8000-011F2000046D"

FEAT_DEVICE_NAME = 0x0005
FEAT_CHANGE_HOST = 0x1814
FEAT_HOSTS_INFO = 0x1815

TYPE_KEYBOARD, TYPE_MOUSE = 0, 3
DEVICE_TYPES = {0: "키보드", 1: "리모컨", 2: "숫자패드", 3: "마우스",
                4: "터치패드", 5: "트랙볼", 6: "프리젠터"}
SW_IDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 15]  # 0x0B, 0x0E(Options+) 제외


def _ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _hexs(b):
    return " ".join("%02X" % x for x in b)


def _chan(host):
    return "채널%d" % (host + 1) if host is not None else "?"


class BleDev:
    def __init__(self, uid, bt_name, handle=None, char=None):
        self.uid = uid
        self.bt_name = bt_name
        self.handle = handle
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


def _show_ble_devices(devs, out):
    for d in devs:
        out("  BLE  %-30s 현재 %s  ChangeHost=0x%02X"
            % (d.label, _chan(d.cur_host), d.ch_idx))
        for h in sorted(d.desc):
            mark = "  <- 현재" if h == d.cur_host else ""
            out("       %s  %s%s" % (_chan(h), d.desc[h], mark))


def _show_ble_mapping(kb, ms, out):
    out("\n키보드 채널 -> 마우스 채널  (무선 엔드포인트 식별자 대조)")
    for kh in sorted(kb.desc):
        m = ms.hosts_with(kb.desc[kh])
        if not m:
            out("  키보드 %s  ->  대응 없음" % _chan(kh))
        elif len(m) == 1:
            out("  키보드 %s  ->  마우스 %s" % (_chan(kh), _chan(m[0])))
        else:
            out("  키보드 %s  ->  마우스 %s (중복)"
                % (_chan(kh), ", ".join(_chan(x) for x in m)))


def _find_pair(devs):
    kb = next((d for d in devs if d.tcode == TYPE_KEYBOARD and d.ch_idx), None)
    ms = next((d for d in devs if d.tcode == TYPE_MOUSE and d.ch_idx), None)
    return kb, ms


# =========================================================================
# macOS CoreBluetooth 백엔드
# =========================================================================

def _run_macos(cmd, args, out, stop_event=None):
    try:
        import objc
        from Foundation import NSObject, NSData
        from dispatch import dispatch_queue_create
        import CoreBluetooth as CB
    except ImportError:
        out("macOS BLE++ 모드에는 PyObjC 가 필요합니다:")
        out("  pip install pyobjc-framework-CoreBluetooth pyobjc-framework-libdispatch")
        out("  (또는 uv run --with pyobjc-framework-CoreBluetooth --with pyobjc-framework-libdispatch ...)")
        return 1

    def make_nsdata(bs):
        return NSData.dataWithBytes_length_(bs, len(bs))

    class Delegate(NSObject):
        def initWithCfg_(self, cfg):
            self = objc.super(Delegate, self).init()
            if self is None:
                return None
            self.dry_run = cfg.get("dry_run", False)
            self.central = None
            self.ready = False
            self.state_name = "unknown"
            self.lock = threading.Lock()
            self._sw = 0
            self.peripherals = {}
            self.done_peripherals = set()
            self.devices = {}
            self.ready_queue = []
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
            if st == CB.CBManagerStatePoweredOn:
                self.ready = True
                self.scan_connected()

        @objc.python_method
        def scan_connected(self):
            cbuuid = CB.CBUUID.UUIDWithString_(LOGI_SVC_UUID)
            ps = self.central.retrieveConnectedPeripheralsWithServices_([cbuuid])
            for p in ps:
                pid = str(p.identifier().UUIDString())
                with self.lock:
                    dev = self.devices.get(pid)
                    already = (dev is not None and dev.connected) or (
                        pid in self.peripherals and p.state() in (
                            CB.CBPeripheralStateConnected, CB.CBPeripheralStateConnecting
                        )
                    )
                if already:
                    continue
                with self.lock:
                    self.peripherals[pid] = p
                    self.done_peripherals.discard(pid)
                p.setDelegate_(self)
                self.central.connectPeripheral_options_(p, None)

        def centralManager_didConnectPeripheral_(self, central, peripheral):
            peripheral.discoverServices_([CB.CBUUID.UUIDWithString_(LOGI_SVC_UUID)])

        def centralManager_didFailToConnectPeripheral_error_(self, central, peripheral, error):
            pid = str(peripheral.identifier().UUIDString())
            with self.lock:
                self.done_peripherals.add(pid)
                self.peripherals.pop(pid, None)

        def centralManager_didDisconnectPeripheral_error_(self, central, peripheral, error):
            pid = str(peripheral.identifier().UUIDString())
            with self.lock:
                dev = self.devices.get(pid)
                if dev:
                    dev.connected = False
                self.peripherals.pop(pid, None)
            if self.follow_active and dev:
                out("%s  [알림] %s 링크 끊김" % (_ts(), dev.label))

        def peripheral_didDiscoverServices_(self, peripheral, error):
            pid = str(peripheral.identifier().UUIDString())
            svcs = peripheral.services() or []
            if error or not svcs:
                with self.lock:
                    self.done_peripherals.add(pid)
                return
            for s in svcs:
                peripheral.discoverCharacteristics_forService_(
                    [CB.CBUUID.UUIDWithString_(LOGI_CHR_UUID)], s
                )

        def peripheral_didDiscoverCharacteristicsForService_error_(self, peripheral, service, error):
            pid = str(peripheral.identifier().UUIDString())
            if not error:
                for c in (service.characteristics() or []):
                    if str(c.UUID().UUIDString()).upper() == LOGI_CHR_UUID:
                        with self.lock:
                            old = self.devices.get(pid)
                            if old:
                                old.handle = peripheral
                                old.char = c
                                old.connected = True
                            else:
                                self.devices[pid] = BleDev(
                                    pid, peripheral.name() or "?", peripheral, c
                                )
                        peripheral.setNotifyValue_forCharacteristic_(True, c)
            with self.lock:
                self.done_peripherals.add(pid)

        def peripheral_didUpdateNotificationStateForCharacteristic_error_(self, peripheral, char, error):
            if error:
                return
            pid = str(peripheral.identifier().UUIDString())
            with self.lock:
                if pid not in self.ready_queue:
                    self.ready_queue.append(pid)

        def peripheral_didUpdateValueForCharacteristic_error_(self, peripheral, char, error):
            if error or not char.value():
                return
            pid = str(peripheral.identifier().UUIDString())
            val = bytes(char.value())

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

            if self.follow_active and len(val) >= 4:
                self._handle_event(pid, val)

        @objc.python_method
        def _handle_event(self, pid, val):
            fidx, b1 = val[0], val[1]
            if (b1 & 0x0F) != 0x00 or fidx >= 0x80:
                return
            with self.lock:
                dev = self.devices.get(pid)
                devs = list(self.devices.values())
            if not dev:
                return

            if dev.tcode == TYPE_MOUSE and fidx == dev.ch_idx:
                dev.cur_host = val[3]
                out("%s  [이벤트] 마우스가 스스로 %s -> %s"
                    % (_ts(), _chan(val[2]), _chan(val[3])))
                return

            if dev.tcode == TYPE_KEYBOARD and fidx == dev.ch_idx:
                src, dst = val[2], val[3]
                dev.cur_host = dst
                ms = next((d for d in devs if d.tcode == TYPE_MOUSE and d.ch_idx and d.connected), None)
                if not ms:
                    out("%s  [이벤트] 키보드 %s -> %s  (연결된 마우스 없음)"
                        % (_ts(), _chan(src), _chan(dst)))
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
                out("%s  [이벤트] 키보드 %s -> %s" % (_ts(), _chan(src), _chan(dst)))
                if not m:
                    out("           -> 대응하는 마우스 채널이 없습니다")
                elif already:
                    out("           -> 마우스는 이미 %s 입니다" % _chan(m[0]))
                elif self.dry_run:
                    out("           -> [시험] 마우스 -> %s" % _chan(m[0]))
                else:
                    out("           => 마우스 -> %s  %s"
                        % (_chan(m[0]), "" if sent else "(전송 실패)"))

        @objc.python_method
        def transact(self, dev, feat_idx, func, params, timeout=1.2, echo=None):
            sw = self.next_sw()
            fs = ((func & 0x0F) << 4) | (sw & 0x0F)
            pkt = bytes(([feat_idx, fs] + list(params) + [0] * 18)[:18])
            ev = threading.Event()
            waiter = {"want": (feat_idx, fs), "echo": echo, "event": ev, "resp": None}
            with self.lock:
                self.waiters[dev.uid] = waiter
            try:
                wtype = (CB.CBCharacteristicWriteWithResponse
                         if (dev.char.properties() & CB.CBCharacteristicPropertyWrite)
                         else CB.CBCharacteristicWriteWithoutResponse)
                dev.handle.writeValue_forCharacteristic_type_(make_nsdata(pkt), dev.char, wtype)
                return waiter["resp"] if ev.wait(timeout) else None
            finally:
                with self.lock:
                    if self.waiters.get(dev.uid) is waiter:
                        self.waiters.pop(dev.uid, None)

        @objc.python_method
        def send_set_host(self, dev, host):
            sw = self.next_sw()
            pkt = bytes(([dev.ch_idx, (0x01 << 4) | sw, host] + [0] * 18)[:18])
            wtype = (CB.CBCharacteristicWriteWithoutResponse
                     if (dev.char.properties() & CB.CBCharacteristicPropertyWriteWithoutResponse)
                     else CB.CBCharacteristicWriteWithResponse)
            try:
                dev.handle.writeValue_forCharacteristic_type_(make_nsdata(pkt), dev.char, wtype)
                return True
            except Exception:
                return False

        @objc.python_method
        def load_device(self, dev):
            if not self.transact(dev, 0x00, 0x01, [0, 0, 0xAA]):
                return False
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
                        dev.desc[h] = _hexs(bytes(rd[3:11]).rstrip(b"\x00"))
            return True

    def drain_ready(delegate):
        loaded = []
        while True:
            with delegate.lock:
                if not delegate.ready_queue:
                    break
                pid = delegate.ready_queue.pop(0)
                dev = delegate.devices.get(pid)
            if dev and dev.connected and delegate.load_device(dev):
                loaded.append(dev)
        return loaded

    q = dispatch_queue_create(b"dev.logifollow.ble", None)
    delegate = Delegate.alloc().initWithCfg_({"dry_run": getattr(args, "dry_run", False)})
    central = CB.CBCentralManager.alloc().initWithDelegate_queue_(delegate, q)
    delegate.central = central

    try:
        end = time.time() + 4.5
        while time.time() < end:
            time.sleep(0.05)
            with delegate.lock:
                done = delegate.ready and delegate.peripherals and len(delegate.done_peripherals) >= len(delegate.peripherals)
            if done:
                break
        if not delegate.ready:
            out("CoreBluetooth 가 활성화되지 않았습니다: %s" % delegate.state_name)
            return 1

        time.sleep(0.4)
        drain_ready(delegate)
        with delegate.lock:
            devs = list(delegate.devices.values())

        if cmd == "map":
            if not devs:
                out("블루투스로 연결된 로지텍 BLE++ 기기가 없습니다.")
                return 1
            out("전송: 블루투스 직결 (CoreBluetooth BLE++)")
            _show_ble_devices(devs, out)
            kb, ms = _find_pair(devs)
            if kb and ms:
                _show_ble_mapping(kb, ms, out)
            else:
                out("\n키보드와 마우스가 모두 이 컴퓨터에 블루투스로 연결돼 있어야 합니다.")
            return 0

        if cmd == "switch":
            _kb, ms = _find_pair(devs)
            if not ms:
                out("블루투스 마우스를 찾지 못했습니다.")
                return 1
            host = args.channel - 1
            if ms.n_hosts and host >= ms.n_hosts:
                out("마우스는 채널 %d 까지만 있습니다." % ms.n_hosts)
                return 1
            if ms.cur_host == host:
                out("마우스는 이미 %s 입니다." % _chan(host))
                return 0
            ok = delegate.send_set_host(ms, host)
            time.sleep(0.15)
            out("마우스 -> %s  %s" % (_chan(host), "전송함" if ok else "전송 실패"))
            return 0 if ok else 1

        # follow
        out("전송: 블루투스 직결 (CoreBluetooth BLE++)")
        if devs:
            _show_ble_devices(devs, out)
            kb, ms = _find_pair(devs)
            if kb and ms:
                _show_ble_mapping(kb, ms, out)
        else:
            out("현재 연결된 블루투스 로지텍 기기가 없습니다. 기기가 돌아오면 자동 연결합니다.")

        delegate.follow_active = True
        if args.dry_run:
            out("\n*** 시험 모드 — 실제로 전송하지 않습니다 ***")
        out("=" * 58)
        out("키보드 Easy-Switch 키를 누르세요.  (Ctrl-C 종료)")
        out("=" * 58 + "\n")

        last_scan = time.time()
        try:
            while not (stop_event and stop_event.is_set()):
                time.sleep(0.2)
                if time.time() - last_scan >= 1.5:
                    delegate.scan_connected()
                    last_scan = time.time()
                for d in drain_ready(delegate):
                    out("%s  [알림] %s 링크 연결됨 (현재 %s)"
                        % (_ts(), d.label, _chan(d.cur_host)))
        except KeyboardInterrupt:
            out("\n종료합니다.")
            return 0
        return 0
    finally:
        with delegate.lock:
            ps = list(delegate.peripherals.values())
        for p in ps:
            try:
                central.cancelPeripheralConnection_(p)
            except Exception:
                pass
        time.sleep(0.15)


# =========================================================================
# Windows / Linux bleak 백엔드
# =========================================================================

def _run_bleak(cmd, args, out, stop_event=None):
    try:
        from bleak import BleakClient, BleakScanner
        from bleak.backends.device import BLEDevice
    except ImportError:
        out("BLE++ 모드에는 bleak 가 필요합니다:  pip install bleak")
        return 1

    def _u64_to_mac(addr_int):
        return ":".join("%02X" % ((addr_int >> (8 * i)) & 0xFF) for i in range(5, -1, -1))

    async def _discover_connected_ble():
        """OS 에 이미 연결된 BLE 기기들의 {mac: (name, ble_device_or_addr, addr_type)} 를 찾는다.

        Windows 는 이미 연결된 BLE HID 기기가 광고(Advertising)를 하지 않는다.
        또한 BleakClient 에 문자열 MAC 을 넘기면 내부적으로 BleakScanner 광고 스캔을
        먼저 돌려 타임아웃으로 실패한다.
        따라서 WinRT 로 연결된 BLE 기기의 MAC 과 address_type 을 직접 조회한 뒤
        BLEDevice 객체로 감싸 넘겨 광고 스캔을 완전히 건너뛴다.
        """
        found = {}
        if sys.platform == "win32":
            try:
                from winrt.windows.devices.enumeration import DeviceInformation
                from winrt.windows.devices.bluetooth import (
                    BluetoothLEDevice, BluetoothConnectionStatus
                )
                find_fn = (getattr(DeviceInformation, "find_all_async_aqs_filter", None)
                           or getattr(DeviceInformation, "find_all_async"))
                aqs_conn = BluetoothLEDevice.get_device_selector_from_connection_status(
                    BluetoothConnectionStatus.CONNECTED
                )
                infos = await find_fn(aqs_conn)
                for info in infos:
                    try:
                        ble = await BluetoothLEDevice.from_id_async(info.id)
                        if ble and ble.bluetooth_address:
                            mac = _u64_to_mac(ble.bluetooth_address)
                            atype = None
                            try:
                                # 0=Public, 1=Random
                                at = int(ble.bluetooth_address_type)
                                atype = "public" if at == 0 else ("random" if at == 1 else None)
                            except Exception:
                                pass
                            name = info.name or getattr(ble, "name", None) or mac
                            # BLEDevice 로 감싸면 BleakClient 가 광고 스캔을 건너뛴다
                            try:
                                bdev = BLEDevice(mac, name, None, rssi=-127)
                            except TypeError:
                                bdev = BLEDevice(mac, name, None)
                            found[mac] = (name, bdev, atype)
                        if ble and hasattr(ble, "close"):
                            ble.close()
                    except Exception as e:
                        out("  WinRT 기기 조회 오류(%s): %s" % (getattr(info, "name", "?"), e))
            except Exception as e:
                out("  WinRT 연결 기기 조회 실패: %s" % e)

        if not found:
            try:
                devices = await BleakScanner.discover(timeout=3.0)
                for d in devices:
                    found[d.address] = (d.name or d.address, d, None)
            except Exception as e:
                out("  BLE 스캔 실패: %s" % e)
        return found

    async def _main():
        sw_idx = [0]

        def next_sw():
            sw_idx[0] = (sw_idx[0] + 1) % len(SW_IDS)
            return SW_IDS[sw_idx[0]]

        devs = {}         # addr -> BleDev
        known_logi = set()
        waiters = {}      # addr -> waiter dict
        follow_active = [False]
        dry_run = getattr(args, "dry_run", False)

        def make_disconnect_cb(addr):
            def on_disc(_client):
                d = devs.get(addr)
                if d:
                    d.connected = False
                    if follow_active[0]:
                        out("%s  [알림] %s 링크 끊김" % (_ts(), d.label))
            return on_disc

        def make_notify_cb(dev):
            def cb(_sender, data):
                val = bytes(data)
                w = waiters.get(dev.uid)
                if w and len(val) >= 2:
                    if val[0] in (0x8F, 0xFF):
                        w["resp"] = None
                        w["ev"].set()
                        return
                    if val[0] == w["fidx"] and val[1] == w["fs"]:
                        if w["echo"] is None or (len(val) >= 3 and val[2] == w["echo"]):
                            w["resp"] = val
                            w["ev"].set()
                            return

                if not follow_active[0] or len(val) < 4:
                    return
                if (val[1] & 0x0F) != 0x00 or val[0] >= 0x80:
                    return

                fidx = val[0]
                if dev.tcode == TYPE_MOUSE and fidx == dev.ch_idx:
                    dev.cur_host = val[3]
                    out("%s  [이벤트] 마우스가 스스로 %s -> %s"
                        % (_ts(), _chan(val[2]), _chan(val[3])))
                    return

                if dev.tcode == TYPE_KEYBOARD and fidx == dev.ch_idx:
                    src, dst = val[2], val[3]
                    dev.cur_host = dst
                    ms = next(
                        (d for d in devs.values() if d.tcode == TYPE_MOUSE and d.ch_idx and d.connected),
                        None
                    )
                    if not ms:
                        out("%s  [이벤트] 키보드 %s -> %s  (연결된 마우스 없음)"
                            % (_ts(), _chan(src), _chan(dst)))
                        return
                    target = dev.desc.get(dst)
                    m = ms.hosts_with(target) if target else []
                    if not m and not target and ms.n_hosts and dst < ms.n_hosts:
                        m = [dst]
                    already = bool(m) and ms.cur_host == m[0]
                    out("%s  [이벤트] 키보드 %s -> %s" % (_ts(), _chan(src), _chan(dst)))
                    if not m:
                        out("           -> 대응하는 마우스 채널이 없습니다")
                    elif already:
                        out("           -> 마우스는 이미 %s 입니다" % _chan(m[0]))
                    elif dry_run:
                        out("           -> [시험] 마우스 -> %s" % _chan(m[0]))
                    else:
                        pkt = bytes(([ms.ch_idx, (0x01 << 4) | next_sw(), m[0]] + [0] * 18)[:18])
                        ms.cur_host = m[0]
                        asyncio.create_task(
                            ms.handle.write_gatt_char(ms.char or LOGI_CHR_UUID.lower(), pkt, response=False)
                        )
                        out("           => 마우스 -> %s" % _chan(m[0]))
            return cb

        async def xact(dev, fidx, func, params, echo=None, timeout=1.5):
            sw = next_sw()
            fs = ((func & 0x0F) << 4) | (sw & 0x0F)
            pkt = bytes(([fidx, fs] + list(params) + [0] * 18)[:18])
            ev = asyncio.Event()
            waiters[dev.uid] = {"fidx": fidx, "fs": fs, "echo": echo, "ev": ev, "resp": None}
            chr_target = dev.char or LOGI_CHR_UUID.lower()
            try:
                use_resp = getattr(dev, "use_write_resp", True)
                await dev.handle.write_gatt_char(chr_target, pkt, response=use_resp)
                await asyncio.wait_for(ev.wait(), timeout)
                return waiters[dev.uid]["resp"]
            except Exception:
                return None
            finally:
                waiters.pop(dev.uid, None)

        async def connect_candidates(only_known=False):
            cands = await _discover_connected_ble()
            if not only_known:
                out("  BLE 후보 %d개: %s" % (
                    len(cands), ", ".join("%s(%s)" % (v[0], k) for k, v in cands.items()) or "없음"
                ))
            svc_uuid = LOGI_SVC_UUID.lower()
            chr_uuid = LOGI_CHR_UUID.lower()
            newly = []
            for addr, (name, target_dev, atype) in cands.items():
                if only_known and known_logi and addr not in known_logi:
                    continue
                cur = devs.get(addr)
                if cur and cur.connected and cur.handle and cur.handle.is_connected:
                    continue
                kwargs = {"disconnected_callback": make_disconnect_cb(addr)}
                if sys.platform == "win32" and atype:
                    kwargs["winrt"] = {"address_type": atype}
                client = BleakClient(target_dev, **kwargs)
                try:
                    await client.connect(timeout=5.0)
                    svc = client.services.get_service(svc_uuid) or client.services.get_service(LOGI_SVC_UUID)
                    chr_obj = (svc.get_characteristic(chr_uuid) or svc.get_characteristic(LOGI_CHR_UUID)) if svc else None
                    if not svc or not chr_obj:
                        if not only_known:
                            uuids = [s.uuid for s in client.services]
                            out("  [%s] BLE++ 서비스 없음 (발견된 서비스: %s)" % (name, ", ".join(uuids)))
                        await client.disconnect()
                        continue
                    known_logi.add(addr)
                    dev = devs.get(addr) or BleDev(addr, name)
                    dev.handle = client
                    dev.char = chr_obj
                    dev.connected = True
                    dev.use_write_resp = True
                    devs[addr] = dev
                    await client.start_notify(chr_obj, make_notify_cb(dev))
                    ping_ok = await xact(dev, 0x00, 0x01, [0, 0, 0xAA])
                    if not ping_ok:
                        # 일부 WinRT 드라이버는 write-without-response 로만 동작
                        dev.use_write_resp = False
                        ping_ok = await xact(dev, 0x00, 0x01, [0, 0, 0xAA])
                    if not ping_ok:
                        if not only_known:
                            out("  [%s] BLE++ 서비스/Notify 성공했으나 Ping 응답 없음 (props=%s)"
                                % (name, chr_obj.properties))
                        await client.disconnect()
                        dev.connected = False
                        continue
                    r_ni = await xact(dev, 0x00, 0x00, [0x00, 0x05, 0])
                    ni = r_ni[2] if r_ni else 0
                    if ni:
                        c = await xact(dev, ni, 0x00, [0, 0, 0])
                        if c:
                            count, txt, pos = c[2], "", 0
                            while pos < count and pos < 64:
                                nr = await xact(dev, ni, 0x01, [pos, 0, 0])
                                if not nr:
                                    break
                                txt += bytes(nr[2:18])[:min(16, count - pos)].decode("ascii", "replace")
                                pos += 16
                            dev.name = txt.strip()
                        t = await xact(dev, ni, 0x02, [0, 0, 0])
                        if t:
                            dev.tcode = t[2]
                    r_ch = await xact(dev, 0x00, 0x00, [0x18, 0x14, 0])
                    dev.ch_idx = r_ch[2] if r_ch else 0
                    r_hi = await xact(dev, 0x00, 0x00, [0x18, 0x15, 0])
                    dev.hi_idx = r_hi[2] if r_hi else 0
                    if dev.ch_idx:
                        ri = await xact(dev, dev.ch_idx, 0x00, [0, 0, 0])
                        if ri:
                            dev.n_hosts, dev.cur_host = ri[2], ri[3]
                    if dev.hi_idx and dev.n_hosts:
                        for h in range(dev.n_hosts):
                            rd = await xact(dev, dev.hi_idx, 0x02, [h, 0, 0], echo=h)
                            if rd:
                                dev.desc[h] = _hexs(bytes(rd[3:11]).rstrip(b"\x00"))
                    newly.append(dev)
                except Exception as e:
                    if not only_known:
                        out("  BLE 연결 실패(%s): %s" % (name, e))
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            return newly

        try:
            await connect_candidates(only_known=False)
            active = [d for d in devs.values() if d.connected]

            if cmd == "map":
                if not active:
                    out("블루투스로 연결된 로지텍 BLE++ 기기가 없습니다.")
                    return 1
                out("전송: 블루투스 직결 (bleak BLE++)")
                _show_ble_devices(active, out)
                kb, ms = _find_pair(active)
                if kb and ms:
                    _show_ble_mapping(kb, ms, out)
                else:
                    out("\n키보드와 마우스가 모두 이 컴퓨터에 블루투스로 연결돼 있어야 합니다.")
                return 0

            if cmd == "switch":
                _kb, ms = _find_pair(active)
                if not ms:
                    out("블루투스 마우스를 찾지 못했습니다.")
                    return 1
                host = args.channel - 1
                if ms.n_hosts and host >= ms.n_hosts:
                    out("마우스는 채널 %d 까지만 있습니다." % ms.n_hosts)
                    return 1
                if ms.cur_host == host:
                    out("마우스는 이미 %s 입니다." % _chan(host))
                    return 0
                pkt = bytes(([ms.ch_idx, (0x01 << 4) | next_sw(), host] + [0] * 18)[:18])
                await ms.handle.write_gatt_char(ms.char or LOGI_CHR_UUID.lower(), pkt, response=False)
                out("마우스 -> %s  전송함" % _chan(host))
                return 0

            # follow
            out("전송: 블루투스 직결 (bleak BLE++)")
            if active:
                _show_ble_devices(active, out)
                kb, ms = _find_pair(active)
                if kb and ms:
                    _show_ble_mapping(kb, ms, out)
            else:
                out("현재 연결된 블루투스 로지텍 기기가 없습니다. 기기가 돌아오면 자동 연결합니다.")

            follow_active[0] = True
            if dry_run:
                out("\n*** 시험 모드 — 실제로 전송하지 않습니다 ***")
            out("=" * 58)
            out("키보드 Easy-Switch 키를 누르세요.  (Ctrl-C 종료)")
            out("=" * 58 + "\n")

            while not (stop_event and stop_event.is_set()):
                await asyncio.sleep(0.5)
                if stop_event and stop_event.is_set():
                    break
                kb_ok = any(d.tcode == TYPE_KEYBOARD and d.connected for d in devs.values())
                ms_ok = any(d.tcode == TYPE_MOUSE and d.connected for d in devs.values())
                if not kb_ok or not ms_ok:
                    reconnected = await connect_candidates(only_known=bool(known_logi))
                    for d in reconnected:
                        out("%s  [알림] %s 링크 연결됨 (현재 %s)"
                            % (_ts(), d.label, _chan(d.cur_host)))
            return 0
        except (KeyboardInterrupt, asyncio.CancelledError):
            out("\n종료합니다.")
            return 0
        finally:
            for d in devs.values():
                if d.handle:
                    try:
                        await d.handle.disconnect()
                    except Exception:
                        pass

    return asyncio.run(_main())


def is_ble_available():
    if sys.platform == "darwin":
        try:
            import CoreBluetooth  # noqa: F401
            import dispatch  # noqa: F401
            return True
        except ImportError:
            return False
    try:
        import bleak  # noqa: F401
        return True
    except ImportError:
        return False


def run_ble(cmd, args, out, stop_event=None):
    if sys.platform == "darwin":
        return _run_macos(cmd, args, out, stop_event=stop_event)
    return _run_bleak(cmd, args, out, stop_event=stop_event)
