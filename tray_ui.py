#!/usr/bin/env python3
"""tray_ui.py - macOS 메뉴바(AppKit) / Windows 시스템 트레이(ctypes Win32) 네이티브 UI.

외부 GUI 프레임워크 없이 OS 네이티브 API 만으로 동작한다.
- macOS  : AppKit NSStatusBar (pyobjc-framework-Cocoa)
- Windows: Win32 Shell_NotifyIconW + CreatePopupMenu (표준 ctypes, 추가 패키지 불필요)

메뉴에서 화면 경계 전환(Edge Switch) 켜기/끄기, 트리거 방식(벽 밀기/체류),
경계별 채널(좌/우/상/하), 대상 세트(자동/USB/BLE/모두), 보조키, 시험 모드를
실시간으로 변경하고 ~/.config/logifollow/config.json 에 즉시 저장한다.
"""

import os
import subprocess
import sys
import threading

EDGE_LABELS = (
    ("left", "왼쪽 경계"),
    ("right", "오른쪽 경계"),
    ("top", "위쪽 경계"),
    ("bottom", "아래쪽 경계"),
)

MOD_LABELS = (
    (None, "없음"),
    ("ctrl", "Control"),
    ("alt", "Option (Alt)"),
    ("shift", "Shift"),
    ("cmd", "Command"),
)

GROUP_LABELS = (
    ("auto", "자동 (연결된 세트)"),
    ("usb", "USB 리시버 세트만"),
    ("ble", "블루투스(BLE++) 세트만"),
    ("all", "모두 (USB + BLE)"),
)


def run_macos_tray(store, controller, stop_event, log_path=None, on_quit=None):
    try:
        import objc
        import AppKit
        from Foundation import NSObject
    except ImportError:
        print("macOS 메뉴바 UI 에는 pyobjc-framework-Cocoa 가 필요합니다:")
        print("  pip install pyobjc-framework-Cocoa")
        return 1

    class TrayDelegate(NSObject):
        def initWithCtx_(self, ctx):
            self = objc.super(TrayDelegate, self).init()
            if self is None:
                return None
            self.store = ctx["store"]
            self.controller = ctx["controller"]
            self.stop_event = ctx["stop_event"]
            self.log_path = ctx.get("log_path")
            self.on_quit = ctx.get("on_quit")
            self.status_item = None
            return self

        @objc.python_method
        def setup(self):
            sb = AppKit.NSStatusBar.systemStatusBar()
            self.status_item = sb.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
            self.refresh_title()
            menu = AppKit.NSMenu.alloc().init()
            menu.setDelegate_(self)
            self.status_item.setMenu_(menu)

        @objc.python_method
        def refresh_title(self):
            if not self.status_item:
                return
            cfg = self.store.get_edge()
            title = "LF"
            if cfg["enabled"]:
                title += "·"
            if self.controller.dry_run:
                title += "[T]"
            btn = self.status_item.button()
            if btn:
                btn.setTitle_(title)

        @objc.python_method
        def _item(self, title, payload=None, checked=False, enabled=True):
            mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, "onAction:" if payload else None, ""
            )
            if payload:
                mi.setTarget_(self)
                mi.setRepresentedObject_(payload)
            mi.setEnabled_(enabled)
            if checked:
                mi.setState_(AppKit.NSControlStateValueOn)
            return mi

        def menuNeedsUpdate_(self, menu):
            menu.removeAllItems()
            cfg = self.store.get_edge()
            statuses = self.controller.group_statuses()

            # 1) 상태 요약
            hdr = "● 감시 중 (%s)" % (" + ".join(k.upper() for k in statuses) if statuses else "대기")
            menu.addItem_(self._item(hdr, enabled=False))
            for gname, summary in statuses.items():
                menu.addItem_(self._item("  %s: %s" % (gname.upper(), summary), enabled=False))
            menu.addItem_(AppKit.NSMenuItem.separatorItem())

            # 2) 경계 전환 켜기/끄기
            menu.addItem_(self._item(
                "화면 경계 전환 사용",
                payload=("toggle_enabled", None, None),
                checked=cfg["enabled"],
            ))

            # 3) 트리거 방식 서브메뉴
            trig_sub = AppKit.NSMenu.alloc().init()
            trig_sub.addItem_(self._item(
                "벽 밀기 (기본, %dpx)" % cfg["push_px"],
                payload=("trigger", "push", None),
                checked=(cfg["trigger"] == "push"),
            ))
            trig_sub.addItem_(self._item(
                "체류 (Dwell, %dms)" % cfg["dwell_ms"],
                payload=("trigger", "dwell", None),
                checked=(cfg["trigger"] == "dwell"),
            ))
            trig_item = self._item("트리거 방식: %s" % ("벽 밀기" if cfg["trigger"] == "push" else "체류"))
            trig_item.setSubmenu_(trig_sub)
            menu.addItem_(trig_item)

            # 4) 경계별 채널 서브메뉴
            edges_menu = AppKit.NSMenu.alloc().init()
            for ekey, elabel in EDGE_LABELS:
                cur_ch = cfg["edges"].get(ekey)
                sub = AppKit.NSMenu.alloc().init()
                sub.addItem_(self._item(
                    "사용 안 함",
                    payload=("edge", ekey, None),
                    checked=(cur_ch is None),
                ))
                for ch in (1, 2, 3):
                    sub.addItem_(self._item(
                        "채널 %d" % ch,
                        payload=("edge", ekey, ch),
                        checked=(cur_ch == ch),
                    ))
                cur_txt = "채널 %d" % cur_ch if cur_ch else "안 함"
                eitem = self._item("%s (%s)" % (elabel, cur_txt))
                eitem.setSubmenu_(sub)
                edges_menu.addItem_(eitem)
            edges_root = self._item("경계 채널 설정")
            edges_root.setSubmenu_(edges_menu)
            menu.addItem_(edges_root)

            # 5) 대상 세트 서브메뉴
            grp_sub = AppKit.NSMenu.alloc().init()
            for gkey, glabel in GROUP_LABELS:
                grp_sub.addItem_(self._item(
                    glabel,
                    payload=("target_group", gkey, None),
                    checked=(cfg["target_group"] == gkey),
                ))
            grp_item = self._item("대상 세트")
            grp_item.setSubmenu_(grp_sub)
            menu.addItem_(grp_item)

            # 6) 보조키 서브메뉴
            mod_sub = AppKit.NSMenu.alloc().init()
            for mkey, mlabel in MOD_LABELS:
                mod_sub.addItem_(self._item(
                    mlabel,
                    payload=("modifier", mkey, None),
                    checked=(cfg["modifier"] == mkey),
                ))
            mod_item = self._item("보조키 필요")
            mod_item.setSubmenu_(mod_sub)
            menu.addItem_(mod_item)

            # 7) 시험 모드
            menu.addItem_(self._item(
                "시험 모드 (Dry-Run)",
                payload=("toggle_dry", None, None),
                checked=self.controller.dry_run,
            ))

            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            menu.addItem_(self._item("설정 파일 열기", payload=("open_config", None, None)))
            if self.log_path and os.path.exists(self.log_path):
                menu.addItem_(self._item("로그 파일 열기", payload=("open_log", None, None)))
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            menu.addItem_(self._item("LogiFollow 종료", payload=("quit", None, None)))

        def onAction_(self, sender):
            payload = sender.representedObject()
            if not payload:
                return
            action, k, v = payload
            if action == "toggle_enabled":
                cur = self.store.get_edge()["enabled"]
                self.store.update_edge(enabled=not cur)
            elif action == "trigger":
                self.store.update_edge(trigger=k)
            elif action == "edge":
                self.store.update_edge(edges={k: v})
            elif action == "target_group":
                self.store.update_edge(target_group=k)
            elif action == "modifier":
                self.store.update_edge(modifier=k)
            elif action == "toggle_dry":
                self.controller.dry_run = not self.controller.dry_run
            elif action == "open_config":
                self.store.update_edge()  # 파일이 아직 없으면 생성
                subprocess.Popen(["open", self.store.path])
            elif action == "open_log" and self.log_path:
                subprocess.Popen(["open", self.log_path])
            elif action == "quit":
                self.stop_event.set()
                if self.on_quit:
                    self.on_quit()
                AppKit.NSApplication.sharedApplication().terminate_(None)
            self.refresh_title()

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    delegate = TrayDelegate.alloc().initWithCtx_({
        "store": store,
        "controller": controller,
        "stop_event": stop_event,
        "log_path": log_path,
        "on_quit": on_quit,
    })
    delegate.setup()

    # Ctrl-C 로 종료 요청이 오면 앱 루프도 종료
    def watch_stop():
        stop_event.wait()
        try:
            AppKit.NSApplication.sharedApplication().performSelectorOnMainThread_withObject_waitUntilDone_(
                "terminate:", None, False
            )
        except Exception:
            pass

    threading.Thread(target=watch_stop, daemon=True).start()
    try:
        app.run()
    except KeyboardInterrupt:
        stop_event.set()
    return 0


# =========================================================================
# Windows 네이티브 시스템 트레이 (ctypes Win32, 외부 패키지 불필요)
# =========================================================================

def run_windows_tray(store, controller, stop_event, log_path=None, on_quit=None):
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

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

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", ctypes.c_wchar * 128),
        ]

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HWND, ctypes.c_void_p
    ]
    user32.TrackPopupMenu.restype = wintypes.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    WM_DESTROY = 0x0002
    WM_TRAY = 0x0400 + 20
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONUP = 0x0205
    NIM_ADD = 0x00000000
    NIM_MODIFY = 0x00000001
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004

    MF_STRING = 0x00000000
    MF_GRAYED = 0x00000001
    MF_CHECKED = 0x00000008
    MF_POPUP = 0x00000010
    MF_SEPARATOR = 0x00000800
    TPM_RETURNCMD = 0x0100
    TPM_RIGHTBUTTON = 0x0002

    nid_holder = [None]
    hwnd_holder = [None]

    def update_tip():
        nid = nid_holder[0]
        if not nid:
            return
        cfg = store.get_edge()
        tip = "LogiFollow"
        if cfg["enabled"]:
            tip += " (경계: %s)" % ("벽밀기" if cfg["trigger"] == "push" else "체류")
        if controller.dry_run:
            tip += " [시험]"
        nid.szTip = tip[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def show_menu(hwnd):
        cfg = store.get_edge()
        statuses = controller.group_statuses()
        actions = {}
        next_id = [1000]

        def add_act(hmenu, label, payload, checked=False):
            cid = next_id[0]
            next_id[0] += 1
            actions[cid] = payload
            flags = MF_STRING | (MF_CHECKED if checked else 0)
            user32.AppendMenuW(hmenu, flags, cid, label)

        root = user32.CreatePopupMenu()
        submenus = []

        hdr = "● 감시 중 (%s)" % (" + ".join(k.upper() for k in statuses) if statuses else "대기")
        user32.AppendMenuW(root, MF_STRING | MF_GRAYED, 0, hdr)
        for gname, summary in statuses.items():
            user32.AppendMenuW(root, MF_STRING | MF_GRAYED, 0, "  %s: %s" % (gname.upper(), summary))
        user32.AppendMenuW(root, MF_SEPARATOR, 0, None)

        add_act(root, "화면 경계 전환 사용", ("toggle_enabled", None, None), checked=cfg["enabled"])

        # 트리거 방식
        trig_m = user32.CreatePopupMenu()
        submenus.append(trig_m)
        add_act(trig_m, "벽 밀기 (기본, %dpx)" % cfg["push_px"], ("trigger", "push", None), checked=(cfg["trigger"] == "push"))
        add_act(trig_m, "체류 (Dwell, %dms)" % cfg["dwell_ms"], ("trigger", "dwell", None), checked=(cfg["trigger"] == "dwell"))
        user32.AppendMenuW(root, MF_POPUP, trig_m, "트리거 방식")

        # 경계 채널 설정
        edges_m = user32.CreatePopupMenu()
        submenus.append(edges_m)
        for ekey, elabel in EDGE_LABELS:
            cur_ch = cfg["edges"].get(ekey)
            sub = user32.CreatePopupMenu()
            submenus.append(sub)
            add_act(sub, "사용 안 함", ("edge", ekey, None), checked=(cur_ch is None))
            for ch in (1, 2, 3):
                add_act(sub, "채널 %d" % ch, ("edge", ekey, ch), checked=(cur_ch == ch))
            cur_txt = "채널 %d" % cur_ch if cur_ch else "안 함"
            user32.AppendMenuW(edges_m, MF_POPUP, sub, "%s (%s)" % (elabel, cur_txt))
        user32.AppendMenuW(root, MF_POPUP, edges_m, "경계 채널 설정")

        # 대상 세트
        grp_m = user32.CreatePopupMenu()
        submenus.append(grp_m)
        for gkey, glabel in GROUP_LABELS:
            add_act(grp_m, glabel, ("target_group", gkey, None), checked=(cfg["target_group"] == gkey))
        user32.AppendMenuW(root, MF_POPUP, grp_m, "대상 세트")

        # 보조키
        mod_m = user32.CreatePopupMenu()
        submenus.append(mod_m)
        for mkey, mlabel in MOD_LABELS:
            add_act(mod_m, mlabel, ("modifier", mkey, None), checked=(cfg["modifier"] == mkey))
        user32.AppendMenuW(root, MF_POPUP, mod_m, "보조키 필요")

        add_act(root, "시험 모드 (Dry-Run)", ("toggle_dry", None, None), checked=controller.dry_run)
        user32.AppendMenuW(root, MF_SEPARATOR, 0, None)
        add_act(root, "설정 파일 열기", ("open_config", None, None))
        if log_path and os.path.exists(log_path):
            add_act(root, "로그 파일 열기", ("open_log", None, None))
        user32.AppendMenuW(root, MF_SEPARATOR, 0, None)
        add_act(root, "LogiFollow 종료", ("quit", None, None))

        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenu(root, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, 0, 0, 0)
        user32.DestroyMenu(root)

        if cmd and cmd in actions:
            action, k, v = actions[cmd]
            if action == "toggle_enabled":
                store.update_edge(enabled=not store.get_edge()["enabled"])
            elif action == "trigger":
                store.update_edge(trigger=k)
            elif action == "edge":
                store.update_edge(edges={k: v})
            elif action == "target_group":
                store.update_edge(target_group=k)
            elif action == "modifier":
                store.update_edge(modifier=k)
            elif action == "toggle_dry":
                controller.dry_run = not controller.dry_run
            elif action == "open_config":
                store.update_edge()
                os.startfile(store.path)
            elif action == "open_log" and log_path:
                os.startfile(log_path)
            elif action == "quit":
                stop_event.set()
                if on_quit:
                    on_quit()
                user32.PostQuitMessage(0)
                return
            update_tip()

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            ev = lparam & 0xFFFF
            if ev in (WM_RBUTTONUP, WM_LBUTTONUP):
                show_menu(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    hinst = kernel32.GetModuleHandleW(None)
    cls_name = "LogiFollowTray_%d" % os.getpid()
    proc_ref = WNDPROC(wndproc)

    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = proc_ref
    wc.hInstance = hinst
    wc.lpszClassName = cls_name
    user32.RegisterClassExW(ctypes.byref(wc))

    hwnd = user32.CreateWindowExW(
        0, cls_name, "LogiFollowTray", 0, 0, 0, 0, 0, None, None, hinst, None
    )
    if not hwnd:
        print("Windows 트레이 윈도우 생성 실패")
        return 1
    hwnd_holder[0] = hwnd

    IDI_APPLICATION = ctypes.c_void_p(32512)
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadIconW.restype = wintypes.HICON
    hicon = user32.LoadIconW(None, IDI_APPLICATION)

    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = hicon
    nid.szTip = "LogiFollow"
    nid_holder[0] = nid
    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
    update_tip()

    def watch_stop():
        stop_event.wait()
        try:
            user32.PostMessageW(hwnd, 0x0012, 0, 0)  # WM_QUIT
        except Exception:
            pass

    threading.Thread(target=watch_stop, daemon=True).start()

    msg = wintypes.MSG()
    try:
        while not stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(cls_name, hinst)
    return 0


def run_tray(store, controller, stop_event, log_path=None, on_quit=None):
    if sys.platform == "darwin":
        return run_macos_tray(store, controller, stop_event, log_path=log_path, on_quit=on_quit)
    if sys.platform == "win32":
        return run_windows_tray(store, controller, stop_event, log_path=log_path, on_quit=on_quit)
    print("현재 플랫폼(%s)의 트레이 UI 는 지원하지 않습니다." % sys.platform)
    return 1
