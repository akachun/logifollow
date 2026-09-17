#!/usr/bin/env python3
"""
hidpp_probe.py - Logitech HID++ 2.0 탐색 / ChangeHost 테스트 도구

목적:
  안드로이드로 포팅하기 전에, 맥(또는 윈도우/리눅스)에서
  M750에 ChangeHost(0x1814) 명령이 실제로 먹히는지 확인한다.

사용법:
  pip install hidapi
  python3 hidpp_probe.py list             # 로지텍 HID 인터페이스 나열
  python3 hidpp_probe.py probe            # HID++ 응답하는 엔드포인트 탐색
  python3 hidpp_probe.py hosts            # 호스트 개수/현재 호스트 조회
  python3 hidpp_probe.py switch 1         # 1번 호스트로 전환 (1-based)

  # 특정 경로/인덱스를 직접 지정하고 싶을 때
  python3 hidpp_probe.py hosts --path "DevSrvsID:..." --dev 2

주의:
  - macOS에서는 터미널에 "입력 모니터링(Input Monitoring)" 권한이 필요할 수 있다.
    시스템 설정 > 개인정보 보호 및 보안 > 입력 모니터링
  - switch를 실행하면 마우스가 실제로 다른 채널로 넘어간다.
    다른 호스트가 준비 안 돼 있으면 마우스가 잠시 먹통이 되니,
    복귀는 마우스 바닥 버튼으로 하면 된다.
"""

import argparse
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("hidapi가 없습니다.  pip install hidapi")

LOGITECH_VID = 0x046D

# HID++ report IDs
REPORT_SHORT = 0x10   # 7 bytes  : id, devidx, featidx, func|swid, p0, p1, p2
REPORT_LONG = 0x11    # 20 bytes : id, devidx, featidx, func|swid, p0..p15

SHORT_LEN = 7
LONG_LEN = 20

SW_ID = 0x05          # 4비트짜리 임의 식별자 (0이면 안 됨)

FEAT_IROOT = 0x0000
FEAT_CHANGE_HOST = 0x1814

IROOT_GET_FEATURE = 0x00
IROOT_PING = 0x01

CH_GET_HOST_INFO = 0x00
CH_SET_CURRENT_HOST = 0x01

ERR_FEATIDX_V2 = 0x8F   # HID++ 2.0 error
ERR_FEATIDX_V1 = 0xFF   # HID++ 1.0 error

# 직결(USB/BT) 기기는 0xFF, 리시버 뒤에 붙은 기기는 1..6
DEVICE_INDICES = [0xFF, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06]


def hexs(b):
    return " ".join("%02X" % x for x in b)


def build(report_id, dev_idx, feat_idx, func, params):
    length = SHORT_LEN if report_id == REPORT_SHORT else LONG_LEN
    pkt = [report_id, dev_idx, feat_idx, ((func & 0x0F) << 4) | SW_ID]
    pkt += list(params)
    pkt += [0] * (length - len(pkt))
    return bytes(pkt[:length])


def transact(dev, report_id, dev_idx, feat_idx, func, params, timeout_ms=800,
             verbose=False):
    """요청 하나를 보내고, 우리 요청에 대응하는 응답만 골라 반환."""
    req = build(report_id, dev_idx, feat_idx, func, params)
    if verbose:
        print("    -> %s" % hexs(req))
    dev.write(req)

    want_swid = ((func & 0x0F) << 4) | SW_ID
    deadline = time.time() + timeout_ms / 1000.0

    while time.time() < deadline:
        rsp = dev.read(64, timeout_ms=int((deadline - time.time()) * 1000) + 1)
        if not rsp:
            continue
        if verbose:
            print("    <- %s" % hexs(rsp))
        if len(rsp) < 4:
            continue
        if rsp[1] != dev_idx:
            continue

        # 에러 응답: [id, devidx, 0x8F, featidx, func|swid, errcode]
        if rsp[2] in (ERR_FEATIDX_V2, ERR_FEATIDX_V1):
            if len(rsp) >= 6 and rsp[3] == feat_idx and rsp[4] == want_swid:
                return ("error", rsp[5], bytes(rsp))
            continue

        if rsp[2] == feat_idx and rsp[3] == want_swid:
            return ("ok", None, bytes(rsp))

    return ("timeout", None, None)


def get_feature_index(dev, dev_idx, feature_id, verbose=False):
    """IRoot.getFeature 로 feature index 를 알아낸다. 없으면 0."""
    status, err, rsp = transact(
        dev, REPORT_SHORT, dev_idx, 0x00, IROOT_GET_FEATURE,
        [(feature_id >> 8) & 0xFF, feature_id & 0xFF, 0x00],
        verbose=verbose)
    if status != "ok":
        return None if status == "timeout" else 0
    return rsp[4]


def ping(dev, dev_idx, verbose=False):
    """IRoot.getProtocolVersion. 응답이 오면 HID++ 2.0 통로가 살아있는 것."""
    status, err, rsp = transact(
        dev, REPORT_SHORT, dev_idx, 0x00, IROOT_PING,
        [0x00, 0x00, 0xAA], verbose=verbose)
    if status == "ok":
        return "HID++ %d.%d" % (rsp[4], rsp[5])
    if status == "error":
        # 1.0 전용 기기는 에러를 뱉지만 어쨌든 살아있는 것
        return "HID++ 1.0 (err 0x%02X)" % err
    return None


def candidate_interfaces():
    """로지텍 VID + 벤더 usage page(0xFF00~) 인터페이스만 추린다."""
    out = []
    for d in hid.enumerate(LOGITECH_VID, 0):
        up = d.get("usage_page") or 0
        if up >= 0xFF00:
            out.append(d)
    return out


def cmd_list(args):
    all_ifaces = hid.enumerate(LOGITECH_VID, 0)
    if not all_ifaces:
        print("로지텍 HID 기기가 안 보입니다. 리시버가 꽂혀 있나요?")
        print("(블루투스 직결이라면 맥에 페어링된 상태여야 합니다)")
        return
    print("로지텍 HID 인터페이스 %d개:\n" % len(all_ifaces))
    for d in all_ifaces:
        mark = " *" if (d.get("usage_page") or 0) >= 0xFF00 else "  "
        print("%s PID=0x%04X  usage_page=0x%04X usage=0x%02X  iface=%s" % (
            mark, d["product_id"], d.get("usage_page") or 0,
            d.get("usage") or 0, d.get("interface_number")))
        print("     product : %s" % (d.get("product_string") or "?"))
        print("     path    : %s" % d["path"].decode("utf-8", "replace"))
    print("\n* 표시가 HID++ 후보(벤더 usage page)입니다.")


def cmd_probe(args):
    cands = candidate_interfaces()
    if not cands:
        print("벤더 usage page 인터페이스를 못 찾았습니다. 먼저 list를 실행해보세요.")
        return

    found = []
    for c in cands:
        path = c["path"]
        print("\n=== PID=0x%04X  %s" % (
            c["product_id"], c.get("product_string") or "?"))
        print("    %s" % path.decode("utf-8", "replace"))
        try:
            dev = hid.device()
            dev.open_path(path)
        except Exception as e:
            print("    열기 실패: %s" % e)
            continue

        try:
            dev.set_nonblocking(0)
            for di in DEVICE_INDICES:
                ver = ping(dev, di, verbose=args.verbose)
                if not ver:
                    continue
                idx = get_feature_index(dev, di, FEAT_CHANGE_HOST,
                                        verbose=args.verbose)
                tag = ("ChangeHost featidx=0x%02X" % idx) if idx else \
                      "ChangeHost 미지원"
                print("    dev_index 0x%02X : %s / %s" % (di, ver, tag))
                if idx:
                    found.append((path, di, idx))
        finally:
            dev.close()

    print("\n" + "=" * 50)
    if found:
        print("ChangeHost 가능한 대상 %d개를 찾았습니다:" % len(found))
        for path, di, idx in found:
            print("  --path \"%s\" --dev %d"
                  % (path.decode("utf-8", "replace"), di))
        print("\n다음: python3 hidpp_probe.py hosts")
    else:
        print("ChangeHost(0x1814)를 지원하는 대상이 없습니다.")
        print("리시버 경유가 아니라 블루투스 직결이면 벤더 통로가")
        print("안 열려 있을 수 있습니다. --verbose 로 원시 패킷을 보세요.")


def resolve_target(args):
    """--path/--dev 가 없으면 자동 탐색."""
    if args.path:
        path = args.path.encode()
        dev = hid.device()
        dev.open_path(path)
        dev.set_nonblocking(0)
        di = args.dev
        idx = get_feature_index(dev, di, FEAT_CHANGE_HOST)
        if not idx:
            dev.close()
            sys.exit("지정한 대상이 ChangeHost를 지원하지 않습니다.")
        return dev, di, idx

    for c in candidate_interfaces():
        try:
            dev = hid.device()
            dev.open_path(c["path"])
            dev.set_nonblocking(0)
        except Exception:
            continue
        for di in DEVICE_INDICES:
            if not ping(dev, di):
                continue
            idx = get_feature_index(dev, di, FEAT_CHANGE_HOST)
            if idx:
                print("대상: PID=0x%04X dev_index=0x%02X featidx=0x%02X"
                      % (c["product_id"], di, idx))
                return dev, di, idx
        dev.close()

    sys.exit("ChangeHost 대상을 못 찾았습니다. probe를 먼저 실행해보세요.")


def cmd_hosts(args):
    dev, di, idx = resolve_target(args)
    try:
        status, err, rsp = transact(dev, REPORT_SHORT, di, idx,
                                    CH_GET_HOST_INFO, [0, 0, 0],
                                    verbose=args.verbose)
        if status != "ok":
            print("getHostInfo 실패: %s %s" % (status, err))
            return
        # 응답 payload: [nbHost, currHost, ...]  (currHost는 0-based)
        nb, cur = rsp[4], rsp[5]
        print("호스트 슬롯 수 : %d" % nb)
        print("현재 호스트    : %d (0-based) = 채널 %d" % (cur, cur + 1))
        print("원시 응답      : %s" % hexs(rsp))
    finally:
        dev.close()


def cmd_switch(args):
    target0 = args.channel - 1
    if target0 < 0:
        sys.exit("채널은 1부터 시작합니다.")
    dev, di, idx = resolve_target(args)
    try:
        print("채널 %d (host index %d) 로 전환 시도..." % (args.channel, target0))
        status, err, rsp = transact(dev, REPORT_SHORT, di, idx,
                                    CH_SET_CURRENT_HOST, [target0, 0, 0],
                                    timeout_ms=400, verbose=args.verbose)
        # 전환에 성공하면 기기가 곧바로 사라지므로 timeout 이 정상일 수 있다
        if status == "ok":
            print("명령 수락됨: %s" % hexs(rsp))
        elif status == "timeout":
            print("응답 없음 — 기기가 이미 넘어갔다면 정상입니다.")
            print("마우스가 실제로 이동했는지 눈으로 확인하세요.")
        else:
            print("에러 0x%02X — %s" % (err, hexs(rsp)))
    finally:
        dev.close()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true",
                   help="원시 HID++ 패킷 출력")
    p.add_argument("--path", help="HID 인터페이스 경로 직접 지정")
    p.add_argument("--dev", type=lambda x: int(x, 0), default=0xFF,
                   help="HID++ device index (직결 0xFF, 리시버 1~6)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="로지텍 HID 인터페이스 나열")
    sub.add_parser("probe", help="HID++ 응답 엔드포인트 탐색")
    sub.add_parser("hosts", help="호스트 개수/현재 호스트 조회")
    sw = sub.add_parser("switch", help="지정 채널로 전환")
    sw.add_argument("channel", type=int, help="채널 번호 (1-based)")

    args = p.parse_args()
    {"list": cmd_list, "probe": cmd_probe,
     "hosts": cmd_hosts, "switch": cmd_switch}[args.cmd](args)


if __name__ == "__main__":
    main()
