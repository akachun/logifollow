#!/usr/bin/env python3
"""블루투스 직결 기기 진단 스니퍼 — 키보드와 마우스를 동시에 본다.

목적: 채널 전환 때 HID++ 알림(ChangeHost)이 블루투스로 오는지 확인.
필터를 걸지 않고 오는 것을 전부 기록하되, 실제 키 입력 내용은 절대
찍지 않는다(스캔코드가 그대로 남기 때문).

  .venv-hid/bin/python sniff_bt.py              공유 모드(기본)
  .venv-hid/bin/python sniff_bt.py --exclusive  배타 점유 시도
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hidpp_bt as B

EXCLUSIVE = "--exclusive" in sys.argv
if not EXCLUSIVE:
    B.allow_shared_open()


class Watch:
    """기기 하나를 붙들고 읽는다. 끊기면 스스로 다시 붙는다."""

    def __init__(self, dev):
        self.dev = dev
        self.tag = dev.label
        self.gone = False
        self.next_try = 0.0
        self.input_counts = {}

    def chan(self):
        c = self.dev.cur_host
        return "채널%d" % (c + 1) if c is not None else "?"

    def poll(self):
        if self.gone:
            self.retry()
            return
        try:
            r = self.dev.h.read(64)
        except OSError as e:
            self.gone = True
            self.next_try = time.time() + 1.0
            print("%s  [%s] ** 링크 끊김 (%s) **" % (B.ts(), self.tag, e))
            if self.input_counts:
                print("%s  [%s]    직전까지 본 리포트: %s"
                      % (B.ts(), self.tag,
                         ", ".join("0x%02X x%d" % kv
                                   for kv in sorted(self.input_counts.items()))))
            self.input_counts = {}
            return
        if not r:
            return
        r = bytes(r)
        rid = r[0]
        if rid in (0x10, 0x11):
            # 이게 우리가 찾는 것이다. 통째로 찍는다.
            print("%s  [%s] HID++  %s" % (B.ts(), self.tag, B.hexs(r[:16])))
            self.decode(r)
        else:
            n = self.input_counts.get(rid, 0) + 1
            self.input_counts[rid] = n
            if n in (1, 50, 200, 1000):
                print("%s  [%s] 입력리포트 rid=0x%02X 누적 %d건 (내용 미기록)"
                      % (B.ts(), self.tag, rid, n))

    def decode(self, r):
        if len(r) < 6 or r[1] != B.DIRECT:
            return
        fidx, b3 = r[2], r[3]
        if (b3 & 0x0F) != 0x00:
            return                      # 우리 요청에 대한 응답
        if fidx == self.dev.ch_idx:
            print("%s  [%s]    >>> ChangeHost 이벤트: 채널%d -> 채널%d <<<"
                  % (B.ts(), self.tag, r[4] + 1, r[5] + 1))
        else:
            print("%s  [%s]    이벤트 featIdx=0x%02X payload=%s"
                  % (B.ts(), self.tag, fidx, B.hexs(r[4:10])))

    def retry(self):
        if time.time() < self.next_try:
            return
        self.next_try = time.time() + 1.0
        for cand in B.discover(self.dev.pid):
            if cand.product != self.dev.product:
                continue
            try:
                cand.open()
            except Exception:
                continue
            if not cand.ping():
                cand.close()
                continue
            self.dev.h = cand.h
            cand.h = None
            self.dev.path = cand.path
            self.dev.desc = {}
            self.dev.load()
            self.dev.h.set_nonblocking(1)
            self.gone = False
            print("%s  [%s] ** 재연결 — 현재 %s **" % (B.ts(), self.tag, self.chan()))
            return


def main():
    print("모드: %s" % ("배타 점유(seize)" if EXCLUSIVE else "공유(비배타)"))
    devs = B.load_all(None)
    if not devs:
        sys.exit("기기를 못 찾았습니다.")
    watches = []
    for d in devs:
        d.h.set_nonblocking(1)
        w = Watch(d)
        watches.append(w)
        print("감시: %-34s ChangeHost=0x%02X  현재 %s"
              % (d.label, d.ch_idx, w.chan()))
    print("\n전환을 시작하세요. HID++ 리포트는 전부 기록됩니다.\n")

    while True:
        for w in watches:
            w.poll()
        time.sleep(0.002)


main()
