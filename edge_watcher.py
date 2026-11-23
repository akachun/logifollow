#!/usr/bin/env python3
"""edge_watcher.py - 화면 경계선(Edge Switch) 상태 머신 및 macOS Quartz 감시기.

공통 EdgeStateMachine 은 OS 독립적이며 단위 테스트 가능하다.
- 멀티 모니터 환경에서 인접 모니터가 없는 최외곽 변만 경계로 판정한다.
- 벽 밀기(push, 기본) / 체류(dwell) 두 가지 트리거를 지원한다.
- 모서리 제외(corner_deadzone_px), 드래그 중 차단, 보조키 필터,
  쿨다운 + 안쪽 재진입(핑퐁 방지) 안전장치를 적용한다.
"""

import sys
import threading
import time


class EdgeStateMachine:
    """OS 독립적인 화면 경계 감지 상태 머신."""

    EDGE_TOLERANCE_PX = 2.0
    REENTER_MARGIN_PX = 16.0
    PUSH_IDLE_RESET_S = 0.45

    def __init__(self, get_config):
        self._get_config = get_config
        self._monitors = []          # [(x, y, w, h), ...]
        self._active_edge = None     # "left" | "right" | "top" | "bottom" | None
        self._enter_time = 0.0
        self._push_accum = 0.0
        self._last_push_time = 0.0
        self._cooldown_until = 0.0
        self._need_reenter_edge = None
        self._last_pos = (0.0, 0.0)
        self._last_dragging = False
        self._last_modifiers = set()

    def set_monitors(self, monitors):
        self._monitors = list(monitors)

    def _find_monitor(self, x, y):
        for m in self._monitors:
            mx, my, mw, mh = m
            if mx <= x < mx + mw and my <= y < my + mh:
                return m
        # 좌표가 경계(mx + mw 또는 my + mh)에 딱 걸린 경우 허용
        for m in self._monitors:
            mx, my, mw, mh = m
            if mx - 1 <= x <= mx + mw + 1 and my - 1 <= y <= my + mh + 1:
                return m
        return None

    def _point_in_any_monitor(self, x, y, exclude=None):
        for m in self._monitors:
            if m == exclude:
                continue
            mx, my, mw, mh = m
            if mx <= x < mx + mw and my <= y < my + mh:
                return True
        return False

    def detect_outer_edge(self, x, y, deadzone_px):
        """현재 좌표가 '인접 모니터가 없는 최외곽 경계'에 닿아 있으면 방향을 반환."""
        mon = self._find_monitor(x, y)
        if not mon:
            return None
        mx, my, mw, mh = mon
        tol = self.EDGE_TOLERANCE_PX
        dz = max(0, min(deadzone_px, min(mw, mh) / 3.0))

        # 좌/우 경계 검사 (상·하단 모서리 deadzone 제외)
        if my + dz <= y <= my + mh - dz:
            if x <= mx + tol:
                if not self._point_in_any_monitor(mx - 4, y, exclude=mon):
                    return "left"
            if x >= mx + mw - 1 - tol:
                if not self._point_in_any_monitor(mx + mw + 4, y, exclude=mon):
                    return "right"

        # 상/하 경계 검사 (좌·우단 모서리 deadzone 제외)
        if mx + dz <= x <= mx + mw - dz:
            if y <= my + tol:
                if not self._point_in_any_monitor(x, my - 4, exclude=mon):
                    return "top"
            if y >= my + mh - 1 - tol:
                if not self._point_in_any_monitor(x, my + mh + 4, exclude=mon):
                    return "bottom"

        return None

    def _is_inside_reenter_margin(self, x, y, edge):
        mon = self._find_monitor(x, y)
        if not mon:
            return True
        mx, my, mw, mh = mon
        m = self.REENTER_MARGIN_PX
        if edge == "left":
            return x >= mx + m
        if edge == "right":
            return x <= mx + mw - 1 - m
        if edge == "top":
            return y >= my + m
        if edge == "bottom":
            return y <= my + mh - 1 - m
        return True

    def compute_nudge_position(self, x, y, edge, nudge_px):
        if nudge_px <= 0:
            return None
        if edge == "left":
            return (x + nudge_px, y)
        if edge == "right":
            return (x - nudge_px, y)
        if edge == "top":
            return (x, y + nudge_px)
        if edge == "bottom":
            return (x, y - nudge_px)
        return None

    def _reset_active(self):
        self._active_edge = None
        self._enter_time = 0.0
        self._push_accum = 0.0
        self._last_push_time = 0.0

    def _outward_delta(self, edge, dx, dy):
        if edge == "left":
            return -dx if dx < 0 else 0.0
        if edge == "right":
            return dx if dx > 0 else 0.0
        if edge == "top":
            return -dy if dy < 0 else 0.0
        if edge == "bottom":
            return dy if dy > 0 else 0.0
        return 0.0

    def process(self, x, y, dx=0.0, dy=0.0, dragging=False, modifiers=None, now=None):
        """마우스 이동 이벤트 처리. 트리거되면 (edge, target_channel, nudge_pos) 반환, 아니면 None."""
        now = time.monotonic() if now is None else now
        mods = set(modifiers or ())
        self._last_pos = (x, y)
        self._last_dragging = dragging
        self._last_modifiers = mods

        cfg = self._get_config()
        if not cfg.get("enabled"):
            self._reset_active()
            return None

        # 재진입 대기 중이면 안쪽으로 충분히 들어온 뒤에만 해제
        if self._need_reenter_edge is not None:
            if self._is_inside_reenter_margin(x, y, self._need_reenter_edge):
                self._need_reenter_edge = None
            else:
                return None

        if now < self._cooldown_until:
            self._reset_active()
            return None

        if dragging:
            self._reset_active()
            return None

        req_mod = cfg.get("modifier")
        if req_mod and req_mod not in mods:
            self._reset_active()
            return None

        edge = self.detect_outer_edge(x, y, cfg.get("corner_deadzone_px", 60))
        if not edge:
            self._reset_active()
            return None

        target = (cfg.get("edges") or {}).get(edge)
        if not target:
            self._reset_active()
            return None

        if self._active_edge != edge:
            self._active_edge = edge
            self._enter_time = now
            self._push_accum = 0.0
            self._last_push_time = now

        trigger = cfg.get("trigger", "push")
        if trigger == "push":
            if now - self._last_push_time > self.PUSH_IDLE_RESET_S:
                self._push_accum = 0.0
            outward = self._outward_delta(edge, dx, dy)
            if outward > 0:
                self._push_accum += outward
                self._last_push_time = now
            if self._push_accum >= cfg.get("push_px", 120):
                return self._fire(edge, target, x, y, cfg, now)
            return None

        # dwell 모드
        dwell_s = cfg.get("dwell_ms", 300) / 1000.0
        if now - self._enter_time >= dwell_s:
            return self._fire(edge, target, x, y, cfg, now)
        return None

    def tick(self, now=None):
        """dwell 모드에서 커서가 정지해 있을 때도 시간 경과를 판정한다."""
        now = time.monotonic() if now is None else now
        cfg = self._get_config()
        if not cfg.get("enabled") or cfg.get("trigger") != "dwell":
            return None
        if not self._active_edge or now < self._cooldown_until:
            return None
        if self._last_dragging:
            self._reset_active()
            return None
        req_mod = cfg.get("modifier")
        if req_mod and req_mod not in self._last_modifiers:
            self._reset_active()
            return None
        target = (cfg.get("edges") or {}).get(self._active_edge)
        if not target:
            self._reset_active()
            return None
        dwell_s = cfg.get("dwell_ms", 300) / 1000.0
        if now - self._enter_time >= dwell_s:
            x, y = self._last_pos
            return self._fire(self._active_edge, target, x, y, cfg, now)
        return None

    def _fire(self, edge, target, x, y, cfg, now):
        nudge = self.compute_nudge_position(x, y, edge, cfg.get("nudge_back_px", 20))
        self._reset_active()
        self._cooldown_until = now + cfg.get("cooldown_ms", 1500) / 1000.0
        self._need_reenter_edge = edge
        return (edge, int(target), nudge)


# =========================================================================
# macOS Quartz 백엔드
# =========================================================================

# =========================================================================
# Windows ctypes Raw Input (WM_INPUT) 백엔드
# =========================================================================

def _classify_win_device_name(name):
    """Windows Raw Input 장치 경로(RIDI_DEVICENAME)에서 로지텍 USB/BLE 그룹 판별."""
    if not name:
        return None
    up = name.upper()
    # 로지텍 VID 046D 가 아닌 일반/터치패드 장치는 None
    if "046D" not in up:
        return None
    # Bluetooth LE HID 장치 경로에는 BTHLEDEVICE 또는 VID&02046D 가 포함된다
    if "BTHLEDEVICE" in up or "VID&02046D" in up or "BTHENUM" in up:
        return "ble"
    # USB Bolt(C548) / Unifying(C52B 등) 리시버
    if "PID_C548" in up or "PID_C52B" in up or "PID_C52F" in up or "PID_C534" in up or "VID_046D" in up:
        return "usb"
    return None


class WinEdgeWatcher:
    """Windows Win32 Raw Input(WM_INPUT) 기반 화면 경계 감시기.

    외부 의존성 없이 ctypes 만으로 동작한다.
    - WM_INPUT 의 RAWMOUSE.lLastX/Y 로 화면 끝에 걸린 뒤에도 벽 밀기(push) delta 를 정확히 수신한다.
    - RAWINPUTHEADER.hDevice 로 방금 움직인 마우스가 USB 리시버인지 BLE 인지 자동 식별한다.
    """

    def __init__(self, get_config, on_trigger, out):
        self._get_config = get_config
        self._on_trigger = on_trigger
        self._out = out
        self._sm = EdgeStateMachine(get_config)
        self._stop = threading.Event()
        self._thread = None
        self._hwnd = None
        self._dev_cache = {}  # hDevice(int) -> "usb" | "ble" | None
        self._last_mon_refresh = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="edge-watcher-win", daemon=True
        )
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._hwnd:
            try:
                import ctypes
                ctypes.windll.user32.PostMessageW(self._hwnd, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # DPI 스케일링 보정 (Per-Monitor V2 -> fallback SetProcessDPIAware)
        try:
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass

        LRESULT = ctypes.c_ssize_t
        WPARAM = ctypes.c_size_t
        LPARAM = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HICON),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON),
            ]

        class RAWINPUTDEVICE(ctypes.Structure):
            _fields_ = [
                ("usUsagePage", wintypes.USHORT),
                ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD),
                ("hwndTarget", wintypes.HWND),
            ]

        class RAWINPUTHEADER(ctypes.Structure):
            _fields_ = [
                ("dwType", wintypes.DWORD),
                ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE),
                ("wParam", WPARAM),
            ]

        class RAWMOUSE(ctypes.Structure):
            _fields_ = [
                ("usFlags", wintypes.USHORT),
                ("usPad", wintypes.USHORT),
                ("ulButtons", wintypes.ULONG),
                ("ulRawButtons", wintypes.ULONG),
                ("lLastX", wintypes.LONG),
                ("lLastY", wintypes.LONG),
                ("ulExtraInformation", wintypes.ULONG),
            ]

        class RAWINPUT(ctypes.Structure):
            _fields_ = [
                ("header", RAWINPUTHEADER),
                ("mouse", RAWMOUSE),
            ]

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.RegisterRawInputDevices.argtypes = [
            ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT
        ]
        user32.RegisterRawInputDevices.restype = wintypes.BOOL
        user32.GetRawInputData.argtypes = [
            ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
            ctypes.POINTER(wintypes.UINT), wintypes.UINT
        ]
        user32.GetRawInputData.restype = wintypes.UINT
        user32.GetRawInputDeviceInfoW.argtypes = [
            wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
            ctypes.POINTER(wintypes.UINT)
        ]
        user32.GetRawInputDeviceInfoW.restype = wintypes.UINT

        MON_CB = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
            ctypes.POINTER(wintypes.RECT), LPARAM
        )

        def refresh_monitors():
            mons = []

            def cb(_hmon, _hdc, lprect, _lparam):
                r = lprect.contents
                mons.append((float(r.left), float(r.top),
                             float(r.right - r.left), float(r.bottom - r.top)))
                return True

            user32.EnumDisplayMonitors(None, None, MON_CB(cb), 0)
            if mons:
                self._sm.set_monitors(mons)
            self._last_mon_refresh = time.monotonic()

        def identify_device(hdev):
            if not hdev:
                return None
            key = int(hdev)
            if key in self._dev_cache:
                return self._dev_cache[key]
            RIDI_DEVICENAME = 0x20000007
            size = wintypes.UINT(0)
            user32.GetRawInputDeviceInfoW(hdev, RIDI_DEVICENAME, None, ctypes.byref(size))
            group = None
            if size.value > 0:
                buf = ctypes.create_unicode_buffer(size.value)
                if user32.GetRawInputDeviceInfoW(
                    hdev, RIDI_DEVICENAME, buf, ctypes.byref(size)
                ) != 0xFFFFFFFF:
                    group = _classify_win_device_name(buf.value)
            self._dev_cache[key] = group
            return group

        def get_mods_and_drag():
            dragging = bool(
                (user32.GetAsyncKeyState(0x01) & 0x8000)
                or (user32.GetAsyncKeyState(0x02) & 0x8000)
                or (user32.GetAsyncKeyState(0x04) & 0x8000)
            )
            mods = set()
            if user32.GetAsyncKeyState(0x11) & 0x8000:
                mods.add("ctrl")
            if user32.GetAsyncKeyState(0x12) & 0x8000:
                mods.add("alt")
            if user32.GetAsyncKeyState(0x10) & 0x8000:
                mods.add("shift")
            if (user32.GetAsyncKeyState(0x5B) & 0x8000) or (user32.GetAsyncKeyState(0x5C) & 0x8000):
                mods.add("cmd")
            return dragging, mods

        def dispatch_res(res, src_group):
            if not res:
                return
            edge, target, nudge = res
            if nudge is not None:
                try:
                    user32.SetCursorPos(int(nudge[0]), int(nudge[1]))
                except Exception:
                    pass
            threading.Thread(
                target=self._on_trigger,
                args=(edge, target, src_group),
                daemon=True,
            ).start()

        WM_INPUT = 0x00FF
        WM_TIMER = 0x0113
        WM_DESTROY = 0x0002
        RID_INPUT = 0x10000003
        RIM_TYPEMOUSE = 0
        MOUSE_MOVE_ABSOLUTE = 0x01
        pt = wintypes.POINT()

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_INPUT:
                ri = RAWINPUT()
                sz = wintypes.UINT(ctypes.sizeof(RAWINPUT))
                res_sz = user32.GetRawInputData(
                    ctypes.c_void_p(lparam), RID_INPUT,
                    ctypes.byref(ri), ctypes.byref(sz),
                    ctypes.sizeof(RAWINPUTHEADER),
                )
                if res_sz != 0xFFFFFFFF and ri.header.dwType == RIM_TYPEMOUSE:
                    now = time.monotonic()
                    if now - self._last_mon_refresh > 5.0:
                        refresh_monitors()
                    # 상대 이동 마우스(일반 USB/BLE 마우스)의 delta 추출
                    if not (ri.mouse.usFlags & MOUSE_MOVE_ABSOLUTE):
                        dx = float(ri.mouse.lLastX)
                        dy = float(ri.mouse.lLastY)
                    else:
                        dx, dy = 0.0, 0.0
                    user32.GetCursorPos(ctypes.byref(pt))
                    dragging, mods = get_mods_and_drag()
                    src_group = identify_device(ri.header.hDevice)
                    res = self._sm.process(
                        float(pt.x), float(pt.y),
                        dx=dx, dy=dy,
                        dragging=dragging,
                        modifiers=mods,
                        now=now,
                    )
                    dispatch_res(res, src_group)
                return 0
            if msg == WM_TIMER:
                res = self._sm.tick()
                if res:
                    dispatch_res(res, None)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        refresh_monitors()
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = "LogiFollowEdgeWatcher_%d" % threading.get_ident()
        proc_ref = WNDPROC(wndproc)  # GC 방지용 참조 유지

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = proc_ref
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(wc))

        HWND_MESSAGE = wintypes.HWND(-3)
        hwnd = user32.CreateWindowExW(
            0, cls_name, "LogiFollowEdge", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, hinst, None,
        )
        if not hwnd:
            self._out("Windows Edge Watcher 윈도우 생성 실패")
            return
        self._hwnd = hwnd

        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01
        rid.usUsage = 0x02        # Mouse
        rid.dwFlags = 0x00000100  # RIDEV_INPUTSINK
        rid.hwndTarget = hwnd
        if not user32.RegisterRawInputDevices(
            ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)
        ):
            self._out("Windows Raw Input 등록 실패")
            user32.DestroyWindow(hwnd)
            return

        user32.SetTimer(hwnd, 1, 50, None)
        msg = wintypes.MSG()
        while not self._stop.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.KillTimer(hwnd, 1)
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(cls_name, hinst)
        self._hwnd = None


def is_edge_watcher_available():
    if sys.platform == "darwin":
        try:
            import Quartz  # noqa: F401
            return True
        except ImportError:
            return False
    if sys.platform == "win32":
        return True
    return False


class MacEdgeWatcher:
    """macOS Quartz CGEventTap 기반 화면 경계 감시기."""

    def __init__(self, get_config, on_trigger, out):
        self._get_config = get_config
        self._on_trigger = on_trigger
        self._out = out
        self._sm = EdgeStateMachine(get_config)
        self._stop = threading.Event()
        self._thread = None
        self._runloop = None
        self._tap = None
        self._last_mon_refresh = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="edge-watcher-mac", daemon=True
        )
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._runloop is not None:
            try:
                import Quartz
                Quartz.CFRunLoopStop(self._runloop)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _refresh_monitors(self, Quartz):
        err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
        if err != 0 or not count:
            return
        mons = []
        for did in ids[:count]:
            b = Quartz.CGDisplayBounds(did)
            mons.append((
                float(b.origin.x), float(b.origin.y),
                float(b.size.width), float(b.size.height),
            ))
        self._sm.set_monitors(mons)
        self._last_mon_refresh = time.monotonic()

    def _extract_modifiers(self, Quartz, flags):
        mods = set()
        if flags & Quartz.kCGEventFlagMaskControl:
            mods.add("ctrl")
        if flags & Quartz.kCGEventFlagMaskAlternate:
            mods.add("alt")
        if flags & Quartz.kCGEventFlagMaskShift:
            mods.add("shift")
        if flags & Quartz.kCGEventFlagMaskCommand:
            mods.add("cmd")
        return mods

    def _dispatch_result(self, Quartz, res):
        if not res:
            return
        edge, target, nudge = res
        if nudge is not None:
            try:
                Quartz.CGWarpMouseCursorPosition(Quartz.CGPoint(nudge[0], nudge[1]))
            except Exception:
                pass
        # 콜백이 이벤트 탭을 블로킹하지 않도록 별도 스레드로 실행
        threading.Thread(
            target=self._on_trigger,
            args=(edge, target, None),
            daemon=True,
        ).start()

    def _run(self):
        try:
            import Quartz
        except ImportError:
            self._out("macOS Edge Switch 에는 pyobjc-framework-Quartz 가 필요합니다:")
            self._out("  pip install pyobjc-framework-Quartz")
            return

        self._refresh_monitors(Quartz)

        mask = (
            (1 << Quartz.kCGEventMouseMoved)
            | (1 << Quartz.kCGEventLeftMouseDragged)
            | (1 << Quartz.kCGEventRightMouseDragged)
            | (1 << Quartz.kCGEventOtherMouseDragged)
        )

        def callback(proxy, etype, event, refcon):
            if etype in (Quartz.kCGEventTapDisabledByTimeout,
                         Quartz.kCGEventTapDisabledByUserInput):
                if self._tap:
                    Quartz.CGEventTapEnable(self._tap, True)
                return event

            now = time.monotonic()
            if now - self._last_mon_refresh > 5.0:
                self._refresh_monitors(Quartz)

            loc = Quartz.CGEventGetLocation(event)
            dx = float(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaX))
            dy = float(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaY))
            dragging = etype in (
                Quartz.kCGEventLeftMouseDragged,
                Quartz.kCGEventRightMouseDragged,
                Quartz.kCGEventOtherMouseDragged,
            )
            mods = self._extract_modifiers(Quartz, Quartz.CGEventGetFlags(event))

            res = self._sm.process(
                float(loc.x), float(loc.y),
                dx=dx, dy=dy,
                dragging=dragging,
                modifiers=mods,
                now=now,
            )
            self._dispatch_result(Quartz, res)
            return event

        tap = Quartz.CGEventTapCreate(
            Quartz.kCGHIDEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            callback,
            None,
        )
        if not tap:
            self._out("macOS 화면 경계 감시 시작 실패: '손쉬운 사용(Accessibility)' 또는 '입력 모니터링' 권한이 필요합니다.")
            self._out("  시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용 / 입력 모니터링 에서 터미널(또는 Python)을 허용하세요.")
            return

        self._tap = tap
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._runloop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._runloop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)

        # dwell 모드용 50ms 틱 스레드
        def dwell_ticker():
            while not self._stop.is_set():
                time.sleep(0.05)
                res = self._sm.tick()
                if res:
                    self._dispatch_result(Quartz, res)

        ticker = threading.Thread(target=dwell_ticker, daemon=True)
        ticker.start()

        while not self._stop.is_set():
            Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.25, False)

        try:
            Quartz.CGEventTapEnable(tap, False)
        except Exception:
            pass
        self._tap = None
        self._runloop = None


class SwitchController:
    """USB / BLE 감시 그룹과 EdgeWatcher 사이의 중재 컨트롤러."""

    def __init__(self, store, out, dry_run=False):
        self.store = store
        self.out = out
        self.dry_run = dry_run
        self._lock = threading.Lock()
        self._groups = {}  # name -> (switch_fn, status_fn)

    def register_group(self, name, switch_fn, status_fn):
        with self._lock:
            self._groups[name] = (switch_fn, status_fn)

    def unregister_group(self, name):
        with self._lock:
            self._groups.pop(name, None)

    def group_statuses(self):
        with self._lock:
            items = list(self._groups.items())
        result = {}
        for name, (_sw, st_fn) in items:
            try:
                result[name] = st_fn()
            except Exception:
                result[name] = "?"
        return result

    def trigger_edge(self, edge, kb_channel, source_group=None):
        cfg = self.store.get_edge()
        target_group = cfg.get("target_group", "auto")
        with self._lock:
            groups = dict(self._groups)

        if not groups:
            self.out("[엣지] %s 경계 -> 채널%d (연결된 감시 그룹 없음)" % (edge, kb_channel))
            return

        if target_group == "all":
            selected = list(groups.keys())
        elif target_group in ("usb", "ble"):
            selected = [target_group] if target_group in groups else []
        else:  # auto
            if source_group and source_group in groups:
                selected = [source_group]
            else:
                selected = list(groups.keys())

        if not selected:
            self.out("[엣지] %s 경계 -> 채널%d (대상 그룹 '%s' 미연결)"
                     % (edge, kb_channel, target_group))
            return

        for gname in selected:
            sw_fn, _st = groups[gname]
            try:
                sw_fn(kb_channel, dry_run=self.dry_run)
            except Exception as e:
                self.out("[엣지] %s 전환 오류: %s" % (gname, e))


def create_edge_watcher(get_config, on_trigger, out):
    if sys.platform == "darwin":
        return MacEdgeWatcher(get_config, on_trigger, out)
    if sys.platform == "win32":
        return WinEdgeWatcher(get_config, on_trigger, out)
    out("현재 플랫폼(%s)의 Edge Watcher 는 지원하지 않습니다." % sys.platform)
    return None
