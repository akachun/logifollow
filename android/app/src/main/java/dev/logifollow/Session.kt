package dev.logifollow

/** 리시버 슬롯 하나에 붙어 있는 기기. */
class DevInfo(val slot: Int) {
    var name: String? = null
    var typeCode: Int? = null
    var changeHostIdx = 0
    var hostsInfoIdx = 0
    var hostCount: Int? = null
    /** 0-based. 채널 표기는 +1 해서 보여준다. */
    var currentHost: Int? = null
    /** host index -> 무선 엔드포인트 식별자(hex 문자열) */
    val descriptors = LinkedHashMap<Int, String>()

    val label: String
        get() = "%s(%s)".format(name ?: "?", Hidpp.deviceType(typeCode))

    val isKeyboard get() = typeCode == Hidpp.TYPE_KEYBOARD
    val isMouse get() = typeCode == Hidpp.TYPE_MOUSE

    /** 이 식별자를 가진 채널들. */
    fun hostsWith(descriptor: String): List<Int> =
        descriptors.entries.filter { it.value == descriptor }.map { it.key }
}

/**
 * HID++ 요청/응답 계층.
 *
 * transact 중에 도착한 이벤트/알림은 버리지 않고 [pending] 에 쌓는다.
 * 채널 전환 이벤트가 조회 요청과 겹쳐 유실되면 앱이 통째로 쓸모없어진다.
 */
class Session(private val t: UsbTransport, private val log: (String) -> Unit) {

    val pending = ArrayDeque<ByteArray>()

    var lastError = 0
        private set

    private var swIndex = 0

    private fun nextSw(): Int {
        swIndex = (swIndex + 1) % Hidpp.SW_IDS.size
        return Hidpp.SW_IDS[swIndex]
    }

    /**
     * 요청 직전에 큐에 남은 것을 치운다.
     *
     * 앞 요청이 타임아웃된 뒤 늦게 온 응답이 남아 있으면 곤란하다.
     * 이벤트는 절대 버리면 안 되므로 pending 으로 옮기고 지난 응답만 버린다.
     */
    private fun flushStale() {
        while (true) {
            val r = t.read(1) ?: return
            if (r.size >= 4 && isEventLike(r)) stash(r)
        }
    }

    fun read(timeoutMs: Int): ByteArray? = pending.removeFirstOrNull() ?: t.read(timeoutMs)

    fun transact(
        index: Int,
        featIdx: Int,
        func: Int,
        params: IntArray,
        reportId: Int = Hidpp.REPORT_SHORT,
        timeoutMs: Int = NORMAL_TIMEOUT_MS
    ): ByteArray? {
        lastError = 0
        flushStale()
        val sw = nextSw()
        if (!t.write(Hidpp.request(reportId, index, featIdx, func, params, sw))) {
            log("전송 실패 (idx=0x%02X feat=0x%02X func=%d)".format(index, featIdx, func))
            return null
        }
        val want = ((func and 0x0F) shl 4) or sw
        val end = System.currentTimeMillis() + timeoutMs
        while (true) {
            val left = (end - System.currentTimeMillis()).toInt()
            if (left <= 0) return null
            val r = t.read(left) ?: continue
            if (r.size < 4) continue

            // 우리 요청과 무관한 것은 보존한다
            if (r.u(1) != index || isEventLike(r)) {
                stash(r)
                continue
            }
            if (r.u(2) == Hidpp.ERR_V2 || r.u(2) == Hidpp.ERR_V1) {
                lastError = r.u(4)
                return null
            }
            if (r.u(2) == featIdx && r.u(3) == want) return r
        }
    }

    private fun stash(r: ByteArray) {
        if (pending.size > 64) pending.removeFirst()
        pending.addLast(r)
    }

    /** 리시버 알림이거나 swID 0 인 기기 이벤트인가. */
    private fun isEventLike(r: ByteArray): Boolean {
        val b2 = r.u(2)
        if (b2 == Hidpp.NOTIF_CONNECTION || b2 == Hidpp.NOTIF_DISCONNECTION) return true
        return (r.u(3) and 0x0F) == 0x00 && b2 < 0x80
    }

    fun featureIndex(index: Int, featureId: Int): Int {
        val r = transact(
            index, 0x00, Hidpp.IROOT_GET_FEATURE,
            intArrayOf((featureId shr 8) and 0xFF, featureId and 0xFF, 0)
        )
        return r?.u(4) ?: 0
    }

    fun ping(slot: Int, timeoutMs: Int): Boolean =
        transact(slot, 0x00, Hidpp.IROOT_PING, intArrayOf(0, 0, 0xAA), timeoutMs = timeoutMs) != null

    fun readSlot(slot: Int): DevInfo {
        val d = DevInfo(slot)

        val nameIdx = featureIndex(slot, Hidpp.FEAT_DEVICE_NAME)
        if (nameIdx != 0) {
            transact(slot, nameIdx, Hidpp.NAME_GET_COUNT, intArrayOf(0, 0, 0))?.let { c ->
                val count = c.u(4)
                val sb = StringBuilder()
                var pos = 0
                while (pos < count && pos < 64) {
                    val nr = transact(
                        slot, nameIdx, Hidpp.NAME_GET_NAME, intArrayOf(pos, 0, 0),
                        reportId = Hidpp.REPORT_LONG
                    ) ?: break
                    val take = minOf(16, count - pos)
                    for (i in 0 until take) {
                        val ch = nr.u(4 + i)
                        if (ch in 0x20..0x7E) sb.append(ch.toChar())
                    }
                    pos += 16
                }
                d.name = sb.toString().trim()
            }
            transact(slot, nameIdx, Hidpp.NAME_GET_TYPE, intArrayOf(0, 0, 0))?.let {
                d.typeCode = it.u(4)
            }
        }

        d.changeHostIdx = featureIndex(slot, Hidpp.FEAT_CHANGE_HOST)
        d.hostsInfoIdx = featureIndex(slot, Hidpp.FEAT_HOSTS_INFO)

        if (d.changeHostIdx != 0) {
            transact(slot, d.changeHostIdx, Hidpp.CH_GET_HOST_INFO, intArrayOf(0, 0, 0))?.let {
                d.hostCount = it.u(4)
                d.currentHost = it.u(5)
            }
        }

        val n = d.hostCount ?: 0
        if (d.hostsInfoIdx != 0 && n > 0) {
            for (host in 0 until n) {
                val r = transact(
                    slot, d.hostsInfoIdx, Hidpp.HI_GET_HOST_DESC, intArrayOf(host, 0, 0),
                    reportId = Hidpp.REPORT_LONG
                ) ?: continue
                // payload = r[4..], 그중 [1:9] 가 무선 엔드포인트 식별자
                var to = 13
                while (to > 5 && r.u(to - 1) == 0) to--
                if (to > 5) d.descriptors[host] = Hidpp.hex(r, 5, to)
            }
        }
        return d
    }

    /**
     * 슬롯 1~6 을 훑는다. 절전 중인 마우스는 첫 요청에 응답이 없거나 아주 느려서,
     * 필요한 종류가 다 나올 때까지 타임아웃을 늘려가며 재시도한다.
     */
    fun scan(need: List<Int>, tries: Int = 4): Map<Int, DevInfo> {
        val devs = LinkedHashMap<Int, DevInfo>()
        for (attempt in 0 until tries) {
            val timeout = if (attempt == 0) NORMAL_TIMEOUT_MS * 2 else DISCOVER_TIMEOUT_MS
            for (slot in 1..6) {
                if (devs.containsKey(slot)) continue
                if (ping(slot, timeout)) {
                    val d = readSlot(slot)
                    devs[slot] = d
                    log("  슬롯 %d = %s".format(slot, d.label))
                }
            }
            val have = devs.values.mapNotNull { it.typeCode }.toSet()
            if (need.all { it in have }) return devs
            if (attempt < tries - 1) {
                val missing = need.filter { it !in have }.joinToString("/") { Hidpp.deviceType(it) }
                log("  $missing 를 못 찾았습니다. 살짝 움직여 깨워주세요. (${attempt + 2}/$tries)")
                Thread.sleep(1200)
            }
        }
        return devs
    }

    fun getNotificationFlags(): Int? {
        if (!t.write(
                Hidpp.build(
                    Hidpp.REPORT_SHORT, Hidpp.RECEIVER_IDX,
                    Hidpp.SUB_GET_REGISTER, Hidpp.REG_NOTIFICATIONS, intArrayOf(0, 0, 0)
                )
            )
        ) return null
        val end = System.currentTimeMillis() + 700
        while (System.currentTimeMillis() < end) {
            val r = t.read((end - System.currentTimeMillis()).toInt()) ?: continue
            if (r.size >= 7 && r.u(1) == Hidpp.RECEIVER_IDX && r.u(2) == Hidpp.SUB_GET_REGISTER) {
                return (r.u(4) shl 16) or (r.u(5) shl 8) or r.u(6)
            }
            stash(r)
        }
        return null
    }

    fun setNotificationFlags(flags: Int): Boolean {
        if (!t.write(
                Hidpp.build(
                    Hidpp.REPORT_SHORT, Hidpp.RECEIVER_IDX,
                    Hidpp.SUB_SET_REGISTER, Hidpp.REG_NOTIFICATIONS,
                    intArrayOf((flags shr 16) and 0xFF, (flags shr 8) and 0xFF, flags and 0xFF)
                )
            )
        ) return false
        val end = System.currentTimeMillis() + 700
        while (System.currentTimeMillis() < end) {
            val r = t.read((end - System.currentTimeMillis()).toInt()) ?: continue
            if (r.size >= 4 && r.u(1) == Hidpp.RECEIVER_IDX && r.u(2) == Hidpp.SUB_SET_REGISTER) {
                return true
            }
            stash(r)
        }
        return false
    }

    /**
     * 채널 전환. 기기가 즉시 링크를 끊으므로 응답을 기다리지 않는다.
     * 전송에 성공했으면 성공으로 본다.
     */
    fun setCurrentHost(d: DevInfo, host: Int): Boolean {
        val pkt = Hidpp.request(
            Hidpp.REPORT_SHORT, d.slot, d.changeHostIdx,
            Hidpp.CH_SET_CURRENT_HOST, intArrayOf(host, 0, 0), nextSw()
        )
        log("      => %s  %s".format(d.label, Hidpp.hex(pkt)))
        return t.write(pkt)
    }

    companion object {
        /** 절전 중인 기기를 기다려야 하는 발견 단계. */
        const val DISCOVER_TIMEOUT_MS = 2500
        const val NORMAL_TIMEOUT_MS = 600
    }
}
