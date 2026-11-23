#!/usr/bin/env python3
"""edge_config.py - 화면 경계선(Edge Switch) 설정 로드/저장 관리.

기본 경로: ~/.config/logifollow/config.json
"""

import copy
import json
import os
import threading

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "logifollow", "config.json"
)

DEFAULT_EDGE_CONFIG = {
    "enabled": False,
    "trigger": "push",           # "push" (벽 밀기, 기본) | "dwell" (체류)
    "push_px": 120,              # push 모드: 경계에서 바깥으로 누적 밀기 거리(px)
    "dwell_ms": 300,             # dwell 모드: 경계 체류 시간(ms)
    "modifier": None,            # None | "ctrl" | "alt" | "shift" | "cmd"
    "corner_deadzone_px": 60,    # 상·하단 모서리 제외 영역(px)
    "cooldown_ms": 1500,         # 전환 후 재트리거 금지 시간(ms)
    "nudge_back_px": 20,         # 전환 직전 커서를 안쪽으로 살짝 이동(px)
    "target_group": "auto",      # "auto" | "usb" | "ble" | "all"
    "edges": {
        "left": None,            # None 또는 1~6 (키보드 기준 채널 번호)
        "right": None,
        "top": None,
        "bottom": None,
    },
}

VALID_EDGES = ("left", "right", "top", "bottom")
VALID_TRIGGERS = ("push", "dwell")
VALID_MODIFIERS = (None, "ctrl", "alt", "shift", "cmd")
VALID_TARGET_GROUPS = ("auto", "usb", "ble", "all")


def _normalize_edge_cfg(raw):
    cfg = copy.deepcopy(DEFAULT_EDGE_CONFIG)
    if not isinstance(raw, dict):
        return cfg

    if "enabled" in raw:
        cfg["enabled"] = bool(raw["enabled"])
    if raw.get("trigger") in VALID_TRIGGERS:
        cfg["trigger"] = raw["trigger"]
    for int_key, lo, hi in (
        ("push_px", 10, 2000),
        ("dwell_ms", 50, 5000),
        ("corner_deadzone_px", 0, 500),
        ("cooldown_ms", 200, 10000),
        ("nudge_back_px", 0, 200),
    ):
        if int_key in raw:
            try:
                cfg[int_key] = max(lo, min(hi, int(raw[int_key])))
            except (TypeError, ValueError):
                pass

    mod = raw.get("modifier")
    if isinstance(mod, str):
        mod = mod.lower().strip()
        if mod in ("none", ""):
            mod = None
    if mod in VALID_MODIFIERS:
        cfg["modifier"] = mod

    tg = raw.get("target_group")
    if tg in VALID_TARGET_GROUPS:
        cfg["target_group"] = tg

    edges = raw.get("edges")
    if isinstance(edges, dict):
        for k in VALID_EDGES:
            v = edges.get(k)
            if v is None or v == "" or v == 0:
                cfg["edges"][k] = None
            else:
                try:
                    iv = int(v)
                    cfg["edges"][k] = iv if 1 <= iv <= 6 else None
                except (TypeError, ValueError):
                    cfg["edges"][k] = None
    return cfg


class ConfigStore:
    """스레드 안전한 설정 저장소."""

    def __init__(self, path=None):
        self.path = os.path.expanduser(path or DEFAULT_CONFIG_PATH)
        self._lock = threading.Lock()
        self._data = {"edge_switch": copy.deepcopy(DEFAULT_EDGE_CONFIG)}
        self.load()

    def load(self):
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict):
                        self._data = raw
                except Exception:
                    pass
            self._data["edge_switch"] = _normalize_edge_cfg(
                self._data.get("edge_switch")
            )
            return copy.deepcopy(self._data["edge_switch"])

    def get_edge(self):
        with self._lock:
            return copy.deepcopy(self._data["edge_switch"])

    def update_edge(self, **kwargs):
        with self._lock:
            cur = copy.deepcopy(self._data["edge_switch"])
            edges_update = kwargs.pop("edges", None)
            cur.update(kwargs)
            if isinstance(edges_update, dict):
                cur["edges"].update(edges_update)
            self._data["edge_switch"] = _normalize_edge_cfg(cur)
            self._save_unlocked()
            return copy.deepcopy(self._data["edge_switch"])

    def _save_unlocked(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)


def format_edge_config(cfg, path=None):
    lines = []
    if path:
        lines.append("설정 파일  : %s" % path)
    lines.append("경계 전환  : %s" % ("켜짐" if cfg["enabled"] else "꺼짐"))
    trig = "벽 밀기(push, %dpx)" % cfg["push_px"] if cfg["trigger"] == "push" \
        else "체류(dwell, %dms)" % cfg["dwell_ms"]
    lines.append("트리거     : %s" % trig)
    lines.append("보조키     : %s" % (cfg["modifier"] or "없음"))
    lines.append("대상 세트  : %s" % cfg["target_group"])
    lines.append("안전장치   : 모서리제외=%dpx  쿨다운=%dms  복귀=%dpx"
                 % (cfg["corner_deadzone_px"], cfg["cooldown_ms"], cfg["nudge_back_px"]))
    edges = cfg["edges"]
    lines.append("경계 채널  : 왼쪽=%s  오른쪽=%s  위=%s  아래=%s" % (
        "채널%d" % edges["left"] if edges["left"] else "-",
        "채널%d" % edges["right"] if edges["right"] else "-",
        "채널%d" % edges["top"] if edges["top"] else "-",
        "채널%d" % edges["bottom"] if edges["bottom"] else "-",
    ))
    return "\n".join(lines)
