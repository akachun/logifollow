#!/usr/bin/env python3
"""tests/test_edge_watcher.py - EdgeStateMachine 단위 테스트."""

import copy
import unittest

from edge_config import DEFAULT_EDGE_CONFIG
from edge_watcher import EdgeStateMachine, _classify_win_device_name


class TestEdgeStateMachine(unittest.TestCase):
    def make_sm(self, **overrides):
        cfg = copy.deepcopy(DEFAULT_EDGE_CONFIG)
        cfg["enabled"] = True
        cfg["edges"] = {"left": 2, "right": 3, "top": None, "bottom": None}
        cfg.update(overrides)
        sm = EdgeStateMachine(lambda: cfg)
        sm.set_monitors([(0, 0, 1920, 1080)])
        return sm, cfg

    def test_push_triggers_after_threshold(self):
        sm, _ = self.make_sm(trigger="push", push_px=100)
        # 오른쪽 끝 도달, 아직 누적 부족
        self.assertIsNone(sm.process(1919, 500, dx=40, dy=0, now=1.0))
        self.assertIsNone(sm.process(1919, 500, dx=50, dy=0, now=1.05))
        # 누적 110 >= 100 -> 트리거!
        res = sm.process(1919, 500, dx=20, dy=0, now=1.10)
        self.assertIsNotNone(res)
        edge, target, nudge = res
        self.assertEqual(edge, "right")
        self.assertEqual(target, 3)
        self.assertEqual(nudge, (1899, 500))

    def test_push_resets_after_idle(self):
        sm, _ = self.make_sm(trigger="push", push_px=100)
        self.assertIsNone(sm.process(1919, 500, dx=80, dy=0, now=1.0))
        # 0.6초 후에는 이전 누적(80)이 리셋되어야 함
        self.assertIsNone(sm.process(1919, 500, dx=40, dy=0, now=1.6))

    def test_dwell_triggers_after_time(self):
        sm, _ = self.make_sm(trigger="dwell", dwell_ms=300)
        self.assertIsNone(sm.process(0, 500, now=10.0))
        self.assertIsNone(sm.tick(now=10.2))
        res = sm.tick(now=10.35)
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "left")
        self.assertEqual(res[1], 2)

    def test_corner_deadzone_blocks_trigger(self):
        sm, _ = self.make_sm(trigger="push", push_px=50, corner_deadzone_px=60)
        # 상단 모서리 (y=20 < 60) -> 차단
        self.assertIsNone(sm.process(1919, 20, dx=200, dy=0, now=1.0))
        # 하단 모서리 (y=1050 > 1020) -> 차단
        self.assertIsNone(sm.process(1919, 1050, dx=200, dy=0, now=1.1))
        # 중앙(y=500) -> 통과
        self.assertIsNotNone(sm.process(1919, 500, dx=200, dy=0, now=1.2))

    def test_dragging_blocks_trigger(self):
        sm, _ = self.make_sm(trigger="push", push_px=50)
        self.assertIsNone(sm.process(1919, 500, dx=200, dy=0, dragging=True, now=1.0))

    def test_modifier_filter(self):
        sm, _ = self.make_sm(trigger="push", push_px=50, modifier="ctrl")
        self.assertIsNone(sm.process(1919, 500, dx=200, dy=0, modifiers=set(), now=1.0))
        res = sm.process(1919, 500, dx=200, dy=0, modifiers={"ctrl"}, now=1.1)
        self.assertIsNotNone(res)

    def test_cooldown_and_reenter_prevents_pingpong(self):
        sm, _ = self.make_sm(trigger="push", push_px=50, cooldown_ms=1500)
        self.assertIsNotNone(sm.process(1919, 500, dx=100, dy=0, now=1.0))
        # 쿨다운 중 차단
        self.assertIsNone(sm.process(1919, 500, dx=100, dy=0, now=2.0))
        # 쿨다운 지났지만 아직 안쪽으로 재진입 안 함 -> 차단
        self.assertIsNone(sm.process(1919, 500, dx=100, dy=0, now=3.0))
        # 안쪽으로 충분히 들어온 뒤 다시 경계로 가면 허용
        self.assertIsNone(sm.process(1850, 500, dx=-50, dy=0, now=3.1))
        self.assertIsNotNone(sm.process(1919, 500, dx=100, dy=0, now=3.2))

    def test_dual_monitor_ignores_inner_border(self):
        sm, _ = self.make_sm(trigger="push", push_px=50)
        # 듀얼 모니터: 왼쪽(0..1920), 오른쪽(1920..3840)
        sm.set_monitors([(0, 0, 1920, 1080), (1920, 0, 1920, 1080)])
        # 왼쪽 모니터의 오른쪽 끝(x=1919)은 오른쪽 모니터와 붙어있으므로 내부 경계 -> 무시!
        self.assertIsNone(sm.process(1919, 500, dx=200, dy=0, now=1.0))
        # 오른쪽 모니터의 오른쪽 끝(x=3839)은 진짜 최외곽 -> 트리거!
        res = sm.process(3839, 500, dx=200, dy=0, now=1.1)
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "right")

    def test_win_device_classification(self):
        self.assertEqual(
            _classify_win_device_name(r"\\?\HID#VID_046D&PID_C548&MI_01#7&1234"),
            "usb",
        )
        self.assertEqual(
            _classify_win_device_name(r"\\?\HID#{00001812-0000-1000-8000-00805F9B34FB}_DEV_VID&02046D_PID&B02C#8&abcd"),
            "ble",
        )
        self.assertIsNone(
            _classify_win_device_name(r"\\?\HID#VID_06CB&PID_7E7E#touchpad"),
        )


if __name__ == "__main__":
    unittest.main()
