package dev.logifollow

import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import java.util.concurrent.LinkedBlockingQueue

/**
 * 리시버를 열고, 키보드의 채널 전환 이벤트를 받아 마우스를 따라 보낸다.
 *
 * 방향이 하나뿐인 이유:
 * 마우스는 자기가 선택한 채널로만 수신한다. 마우스가 다른 컴퓨터에 가 있으면
 * 우리 리시버는 말을 걸 수 없다. 그래서 "둘 다 여기 있을 때 키보드가 떠나면
 * 마우스도 딸려 보낸다" 만 가능하다. 덱스에서 빠져나가는 경우가 정확히 이것이다.
 * 돌아오는 방향은 마우스 바닥 버튼으로 수동 전환해야 한다.
 */
class FollowEngine(
    private val usb: UsbManager,
    private val device: UsbDevice
) : Runnable {

    sealed class Cmd {
        object Rescan : Cmd()
        object Sync : Cmd()
        object Stop : Cmd()
        data class Switch(val host: Int) : Cmd()
    }

    @Volatile var dryRun = false
    @Volatile var verbose = false

    private val queue = LinkedBlockingQueue<Cmd>()
    @Volatile private var alive = true

    private var transport: UsbTransport? = null
    private var session: Session? = null
    private var devices: Map<Int, DevInfo> = emptyMap()
    private var keyboard: DevInfo? = null
    private var mouse: DevInfo? = null

    /** 키보드가 마지막으로 향한 채널. 링크가 끊긴 뒤에도 기억해 둔다. */
    private var kbLastHost: Int? = null

    private var originalFlags: Int? = null

    fun post(c: Cmd) = queue.offer(c)

    fun stop() {
        alive = false
        queue.offer(Cmd.Stop)
    }

    override fun run() {
        Bus.setState { it.copy(running = true, problem = null, receiver = deviceLabel()) }
        Bus.log("리시버: " + deviceLabel())

        val t = UsbTransport.open(usb, device, Bus::log)
        if (t == null) {
            fail("HID++ 인터페이스를 열지 못했습니다.")
            return
        }
        transport = t
        Bus.setState { it.copy(transport = t.description) }
        val s = Session(t, Bus::log)
        session = s

        try {
            enableNotifications(s)
            rescan(s)
            loop(s, t)
        } catch (e: Exception) {
            Bus.log("엔진 예외: ${e.javaClass.simpleName}: ${e.message}")
            fail(e.message ?: e.javaClass.simpleName)
        } finally {
            restoreNotifications(s)
            t.close()
            transport = null
            session = null
            Bus.setState { it.copy(running = false) }
            Bus.log("정지했습니다.")
        }
    }

    private fun deviceLabel() =
        "%s  VID=0x%04X PID=0x%04X".format(
            device.productName ?: device.deviceName, device.vendorId, device.productId
        )

    private fun fail(msg: String) {
        Bus.setState { it.copy(running = false, problem = msg) }
        Bus.log("실패: $msg")
    }

    // ---- 준비 ---------------------------------------------------------

    private fun enableNotifications(s: Session) {
        val cur = s.getNotificationFlags()
        originalFlags = cur
        if (cur == null) {
            Bus.log("알림 플래그를 읽지 못했습니다. 그대로 진행합니다.")
            return
        }
        val want = cur or Hidpp.FLAG_WIRELESS or Hidpp.FLAG_SW_PRESENT
        Bus.log("알림 플래그 0x%06X".format(cur) + if (want != cur) " -> 0x%06X".format(want) else " (그대로)")
        if (want != cur) s.setNotificationFlags(want)
    }

    private fun restoreNotifications(s: Session?) {
        val orig = originalFlags ?: return
        try { s?.setNotificationFlags(orig) } catch (_: Exception) {}
    }

    private fun rescan(s: Session) {
        Bus.log("슬롯을 훑습니다...")
        devices = s.scan(listOf(Hidpp.TYPE_KEYBOARD, Hidpp.TYPE_MOUSE))
        if (devices.isEmpty()) {
            fail("리시버에 붙은 기기가 없습니다.")
            return
        }
        keyboard = devices.values.firstOrNull { it.isKeyboard && it.changeHostIdx != 0 }
        mouse = devices.values.firstOrNull { it.isMouse && it.changeHostIdx != 0 }
        kbLastHost = keyboard?.currentHost

        for (d in devices.values) showDevice(d)

        val kb = keyboard
        val m = mouse
        Bus.setState {
            it.copy(
                keyboard = kb?.let { k -> "슬롯 ${k.slot}  ${k.label}  현재 ${chan(k.currentHost)}" },
                mouse = m?.let { x -> "슬롯 ${x.slot}  ${x.label}  현재 ${chan(x.currentHost)}" },
                mapping = if (kb != null && m != null) mappingLines(kb, m) else emptyList(),
                problem = when {
                    kb == null -> "키보드를 못 찾았습니다. 키보드가 이 리시버 채널에 있어야 합니다."
                    m == null -> "마우스를 못 찾았습니다. 마우스를 움직여 깨운 뒤 다시 검색하세요."
                    else -> null
                }
            )
        }
        if (kb != null && m != null) {
            Bus.log("감시: ${kb.label} (슬롯 ${kb.slot})   따라감: ${m.label} (슬롯 ${m.slot})")
            mappingLines(kb, m).forEach { Bus.log("  $it") }
            if (dryRun) Bus.log("*** 시험 모드 — 실제로 전송하지 않습니다 ***")
        } else {
            Bus.log("키보드와 마우스가 모두 이 리시버 채널에 있어야 동작합니다.")
        }
    }

    private fun chan(host: Int?) = if (host == null) "?" else "채널${host + 1}"

    private fun showDevice(d: DevInfo) {
        Bus.log(
            "  슬롯 %d = %-30s 현재 %s  ChangeHost=0x%02X HostsInfo=0x%02X".format(
                d.slot, d.label, chan(d.currentHost), d.changeHostIdx, d.hostsInfoIdx
            )
        )
        for ((h, desc) in d.descriptors) {
            Bus.log("           채널${h + 1}  $desc" + if (h == d.currentHost) "  <- 현재" else "")
        }
    }

    private fun mappingLines(kb: DevInfo, m: DevInfo): List<String> {
        if (kb.descriptors.isEmpty() || m.descriptors.isEmpty()) {
            return listOf("호스트 식별자를 못 읽었습니다. 채널 번호 그대로 따라갑니다.")
        }
        return kb.descriptors.keys.sorted().map { kh ->
            val target = kb.descriptors[kh]!!
            val matches = m.hostsWith(target)
            when {
                matches.isEmpty() -> "키보드 채널${kh + 1}  ->  대응 없음"
                matches.size == 1 -> "키보드 채널${kh + 1}  ->  마우스 채널${matches[0] + 1}"
                else -> "키보드 채널${kh + 1}  ->  마우스 채널 " +
                    matches.joinToString(", ") { (it + 1).toString() } + " (중복 페어링)"
            }
        }
    }

    // ---- 이벤트 루프 ---------------------------------------------------

    private fun loop(s: Session, t: UsbTransport) {
        Bus.log("대기 중. 키보드 Easy-Switch 키를 누르면 마우스가 따라갑니다.")
        while (alive) {
            while (true) {
                val c = queue.poll() ?: break
                handleCommand(s, c)
                if (!alive) return
            }
            val r = s.read(200) ?: continue
            handlePacket(s, r)
        }
    }

    private fun handleCommand(s: Session, c: Cmd) {
        when (c) {
            is Cmd.Stop -> alive = false
            is Cmd.Rescan -> rescan(s)
            is Cmd.Switch -> {
                val m = mouse
                if (m == null) Bus.log("마우스를 모릅니다. 먼저 검색하세요.")
                else sendMouse(s, m, c.host, "수동")
            }
            is Cmd.Sync -> syncNow(s)
        }
    }

    private fun syncNow(s: Session) {
        val kb = keyboard
        val m = mouse ?: run { Bus.log("마우스를 모릅니다."); return }
        // 키보드가 아직 여기 있으면 현재 채널을 다시 읽고, 떠났으면 마지막 관측치를 쓴다
        var dest = kbLastHost
        if (kb != null && s.ping(kb.slot, Session.NORMAL_TIMEOUT_MS)) {
            val fresh = s.readSlot(kb.slot)
            fresh.currentHost?.let { dest = it; kb.currentHost = it }
            if (fresh.descriptors.isNotEmpty()) {
                kb.descriptors.clear(); kb.descriptors.putAll(fresh.descriptors)
            }
        }
        val d = dest
        if (d == null) {
            Bus.log("키보드가 어느 채널에 있는지 모릅니다.")
            return
        }
        val target = resolveMouseHost(kb, m, d)
        if (target == null) {
            Bus.log("키보드 채널${d + 1} 에 대응하는 마우스 채널이 없습니다.")
            return
        }
        if (m.currentHost == target) {
            Bus.log("마우스는 이미 채널${target + 1} 입니다.")
            return
        }
        sendMouse(s, m, target, "맞추기")
    }

    private fun handlePacket(s: Session, r: ByteArray) {
        if (r.size < 4) return
        val slot = r.u(1)
        val b2 = r.u(2)
        val b3 = r.u(3)

        // 리시버 알림
        if (b2 == Hidpp.NOTIF_CONNECTION || b2 == Hidpp.NOTIF_DISCONNECTION) {
            val st = r.u(4)
            val down = (st and Hidpp.LINK_DOWN_BIT) != 0
            val who = devices[slot]?.label ?: "슬롯$slot"
            Bus.log("[알림] $who  링크 " + (if (down) "끊김" else "연결됨") +
                " (${Hidpp.notifKind(st and 0x0F)})")
            // 마우스가 돌아왔으면 현재 채널이 바뀌었을 수 있다
            val m = mouse
            if (!down && m != null && slot == m.slot) refreshMouse(s, m)
            return
        }

        // swID 0 = 요청 응답이 아니라 기기 이벤트
        if ((b3 and 0x0F) == 0x00 && b2 < 0x80) {
            val kb = keyboard
            val m = mouse
            if (kb != null && slot == kb.slot && b2 == kb.changeHostIdx && r.size >= 6) {
                onKeyboardChangeHost(s, kb, m, r.u(4), r.u(5))
                return
            }
            if (m != null && slot == m.slot && b2 == m.changeHostIdx && r.size >= 6) {
                m.currentHost = r.u(5)
                Bus.log("[이벤트] 마우스가 스스로 채널${r.u(4) + 1} -> 채널${r.u(5) + 1}")
                return
            }
            if (verbose) Bus.log("[이벤트] 슬롯$slot feat=0x%02X  %s".format(b2, Hidpp.hex(r, 4)))
            return
        }

        if (verbose) Bus.log("[기타] " + Hidpp.hex(r))
    }

    /**
     * 트리거. 순서가 중요하다 — 계산하고 곧바로 쏘고, 기록은 그다음이다.
     * 키보드가 떠나면서 링크 정리가 시작되므로 늑장 부리면 안 된다.
     */
    private fun onKeyboardChangeHost(s: Session, kb: DevInfo, m: DevInfo?, from: Int, dest: Int) {
        kbLastHost = dest
        kb.currentHost = dest

        if (m == null) {
            Bus.log("[이벤트] 키보드 채널${from + 1} -> 채널${dest + 1} (마우스를 몰라 못 따라감)")
            return
        }
        val target = resolveMouseHost(kb, m, dest)
        val already = target != null && m.currentHost == target

        if (target != null && !already) {
            sendMouse(s, m, target, "따라감")
        }

        Bus.log("[이벤트] 키보드 채널${from + 1} -> 채널${dest + 1}")
        when {
            target == null ->
                Bus.log("      -> 대응하는 마우스 채널이 없습니다 (키보드 채널${dest + 1})")
            already ->
                Bus.log("      -> 마우스는 이미 채널${target + 1} 입니다")
        }
    }

    /** 키보드가 간 호스트와 같은 무선 엔드포인트를 가진 마우스 채널. */
    private fun resolveMouseHost(kb: DevInfo?, m: DevInfo, kbHost: Int): Int? {
        val desc = kb?.descriptors?.get(kbHost)
        if (desc != null) {
            val matches = m.hostsWith(desc)
            if (matches.isNotEmpty()) return matches[0]
        }
        // 식별자를 못 읽었거나 대응이 없으면 채널 번호 그대로 시도한다.
        val n = m.hostCount ?: 0
        return if (kbHost < n) kbHost else null
    }

    private fun sendMouse(s: Session, m: DevInfo, host: Int, why: String) {
        if (dryRun) {
            Bus.log("      [시험] $why: 마우스 -> 채널${host + 1} (전송 안 함)")
            return
        }
        val ok = s.setCurrentHost(m, host)
        if (ok) {
            m.currentHost = host
            Bus.log("      $why: 마우스 -> 채널${host + 1}")
            Bus.setState { it.copy(mouse = "슬롯 ${m.slot}  ${m.label}  현재 채널${host + 1}") }
        } else {
            Bus.log("      전송 실패: 마우스 -> 채널${host + 1}")
        }
    }

    private fun refreshMouse(s: Session, m: DevInfo) {
        val fresh = s.readSlot(m.slot)
        fresh.currentHost?.let { m.currentHost = it }
        if (fresh.descriptors.isNotEmpty()) {
            m.descriptors.clear()
            m.descriptors.putAll(fresh.descriptors)
        }
        Bus.log("  마우스 상태 갱신: 현재 ${chan(m.currentHost)}")
        Bus.setState { it.copy(mouse = "슬롯 ${m.slot}  ${m.label}  현재 ${chan(m.currentHost)}") }
    }
}
