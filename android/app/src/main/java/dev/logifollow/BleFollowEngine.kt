package dev.logifollow

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.os.Build
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * 블루투스(BLE++) 직결 엔진.
 *
 * 표준 BLE HID 서비스(0x1812)는 안드로이드가 보안상 차단(SecurityException)하지만,
 * 로지텍 벤더 커스텀 GATT 서비스(00010000-0000-1000-8000-011F2000046D)와 특성
 * (00010001-...)은 커스텀 UUID 라 차단되지 않는다.
 *
 * 18바이트 BLE++ 포맷([featIdx, (func<<4)|swID, params(16B)])으로
 * 키보드의 ChangeHost(0x1814) Notify 를 수신하고 즉시 마우스에 setCurrentHost 를 전송한다.
 */
@SuppressLint("MissingPermission")
class BleFollowEngine(private val ctx: Context) : Engine {

    companion object {
        val LOGI_SVC_UUID: UUID = UUID.fromString("00010000-0000-1000-8000-011f2000046d")
        val LOGI_CHR_UUID: UUID = UUID.fromString("00010001-0000-1000-8000-011f2000046d")
        val CCCD_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        private val SW_IDS = intArrayOf(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 15)
        private const val BLE_PKT_LEN = 18
    }

    @Volatile override var dryRun = false
    @Volatile override var verbose = false
    @Volatile private var alive = true

    private val queue = LinkedBlockingQueue<EngineCmd>()
    private val swSeq = AtomicInteger(0)

    private class BlePeer(
        val btDevice: BluetoothDevice,
        var gatt: BluetoothGatt? = null,
        var chr: BluetoothGattCharacteristic? = null
    ) {
        val address: String get() = btDevice.address
        val btName: String get() = try { btDevice.name ?: address } catch (_: Exception) { address }
        @Volatile var connected = false
        @Volatile var notifyReady = false
        @Volatile var isLogi = false
        @Volatile var failed = false

        val info = DevInfo(0xFF)
        val writeLock = Any()
        @Volatile var writeLatch: CountDownLatch? = null
        @Volatile var descLatch: CountDownLatch? = null
        @Volatile var waiter: RespWaiter? = null
    }

    private class RespWaiter(
        val featIdx: Int,
        val funcSw: Int,
        val echo: Int?
    ) {
        val latch = CountDownLatch(1)
        @Volatile var resp: ByteArray? = null
    }

    private val peers = ConcurrentHashMap<String, BlePeer>()
    private val knownLogi = ConcurrentHashMap.newKeySet<String>()
    private val newlyReady = LinkedBlockingQueue<String>()

    @Volatile private var keyboard: BlePeer? = null
    @Volatile private var mouse: BlePeer? = null
    @Volatile private var kbLastHost: Int? = null
    @Volatile private var suppressFollowUntil: Long = 0L

    override fun post(c: EngineCmd): Boolean = queue.offer(c)

    override fun stop() {
        alive = false
        queue.offer(EngineCmd.Stop)
    }

    private fun nextSw(): Int = SW_IDS[Math.floorMod(swSeq.incrementAndGet(), SW_IDS.size)]

    override fun run() {
        Bus.setState {
            it.copy(
                running = true,
                problem = null,
                receiver = "블루투스 직결 (BLE++)",
                transport = "GATT 00010000-... (18B)"
            )
        }
        Bus.log("블루투스 직결(BLE++) 모드로 시작합니다.")

        val bm = ctx.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager
        val adapter = bm?.adapter
        if (adapter == null || !adapter.isEnabled) {
            fail("블루투스가 꺼져 있습니다.")
            return
        }

        try {
            scanBonded(adapter, onlyKnown = false)
            loop(adapter)
        } catch (e: SecurityException) {
            fail("블루투스 권한(근처 기기)이 필요합니다: ${e.message}")
        } catch (e: Exception) {
            Bus.log("BLE 엔진 예외: ${e.javaClass.simpleName}: ${e.message}")
            fail(e.message ?: e.javaClass.simpleName)
        } finally {
            closeAll()
            Bus.setState { it.copy(running = false) }
            Bus.log("BLE 감시를 정지했습니다.")
        }
    }

    private fun fail(msg: String) {
        Bus.setState { it.copy(running = false, problem = msg) }
        Bus.log("실패: $msg")
    }

    // ---- 탐색 및 연결 -------------------------------------------------

    private fun isDeviceConnected(dev: BluetoothDevice): Boolean {
        return try {
            val m = dev.javaClass.getMethod("isConnected")
            (m.invoke(dev) as? Boolean) == true
        } catch (_: Exception) {
            true // 판별 불가 시 후보에 포함
        }
    }

    private fun scanBonded(adapter: BluetoothAdapter, onlyKnown: Boolean) {
        val bonded = try { adapter.bondedDevices } catch (e: SecurityException) {
            fail("블루투스 권한이 필요합니다.")
            return
        } ?: emptySet()

        val candidates = bonded.filter { dev ->
            val typeOk = dev.type == BluetoothDevice.DEVICE_TYPE_LE ||
                dev.type == BluetoothDevice.DEVICE_TYPE_DUAL ||
                dev.type == BluetoothDevice.DEVICE_TYPE_UNKNOWN
            if (!typeOk) return@filter false
            if (onlyKnown && knownLogi.isNotEmpty() && dev.address !in knownLogi) return@filter false
            isDeviceConnected(dev)
        }

        if (!onlyKnown) {
            Bus.log("연결된 페어링 BLE 기기 ${candidates.size}개 확인 중...")
        }

        for (dev in candidates) {
            val existing = peers[dev.address]
            if (existing != null && existing.connected && existing.notifyReady) continue
            connectPeer(dev)
        }
    }

    private fun connectPeer(dev: BluetoothDevice) {
        val old = peers.remove(dev.address)
        try { old?.gatt?.close() } catch (_: Exception) {}

        val peer = BlePeer(dev)
        peers[dev.address] = peer
        Bus.log("  GATT 연결 시도: ${peer.btName} (${dev.address})")

        val cb = object : BluetoothGattCallback() {
            override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
                if (newState == BluetoothProfile.STATE_CONNECTED && status == BluetoothGatt.GATT_SUCCESS) {
                    peer.connected = true
                    gatt.discoverServices()
                } else {
                    val wasLogi = peer.isLogi
                    peer.connected = false
                    peer.notifyReady = false
                    peer.failed = true
                    peer.writeLatch?.countDown()
                    peer.descLatch?.countDown()
                    peer.waiter?.latch?.countDown()
                    try { gatt.close() } catch (_: Exception) {}
                    if (wasLogi) {
                        Bus.log("[연결 끊김] ${peer.info.label}")
                        updateUiState()
                    }
                }
            }

            override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    closeNonLogi(peer, gatt)
                    return
                }
                val svc = gatt.getService(LOGI_SVC_UUID)
                val chr = svc?.getCharacteristic(LOGI_CHR_UUID)
                if (chr == null) {
                    if (verbose) Bus.log("  비로지텍 기기 제외: ${peer.btName}")
                    closeNonLogi(peer, gatt)
                    return
                }
                peer.isLogi = true
                peer.chr = chr
                knownLogi.add(peer.address)
                Bus.log("  BLE++ 서비스 발견: ${peer.btName}")

                gatt.setCharacteristicNotification(chr, true)
                val cccd = chr.getDescriptor(CCCD_UUID)
                if (cccd == null) {
                    peer.notifyReady = true
                    newlyReady.offer(peer.address)
                    return
                }
                peer.descLatch = CountDownLatch(1)
                val ok = writeDescriptorCompat(gatt, cccd, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
                if (!ok) {
                    Bus.log("  Notify CCCD 쓰기 실패: ${peer.btName}")
                }
            }

            override fun onDescriptorWrite(gatt: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) {
                peer.descLatch?.countDown()
                if (descriptor.uuid == CCCD_UUID && status == BluetoothGatt.GATT_SUCCESS) {
                    peer.notifyReady = true
                    newlyReady.offer(peer.address)
                } else if (descriptor.uuid == CCCD_UUID) {
                    Bus.log("  Notify 구독 실패(${peer.btName}): status=$status")
                }
            }

            override fun onCharacteristicWrite(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic, status: Int) {
                peer.writeLatch?.countDown()
            }

            @Deprecated("Deprecated in API 33")
            override fun onCharacteristicChanged(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
                if (Build.VERSION.SDK_INT < 33) {
                    val v = characteristic.value ?: return
                    onNotify(peer, v.copyOf())
                }
            }

            override fun onCharacteristicChanged(
                gatt: BluetoothGatt,
                characteristic: BluetoothGattCharacteristic,
                value: ByteArray
            ) {
                onNotify(peer, value.copyOf())
            }
        }

        peer.gatt = dev.connectGatt(ctx, false, cb, BluetoothDevice.TRANSPORT_LE)
    }

    private fun closeNonLogi(peer: BlePeer, gatt: BluetoothGatt) {
        peer.connected = false
        peer.failed = true
        peers.remove(peer.address)
        try { gatt.disconnect() } catch (_: Exception) {}
        try { gatt.close() } catch (_: Exception) {}
    }

    @Suppress("DEPRECATION")
    private fun writeDescriptorCompat(gatt: BluetoothGatt, desc: BluetoothGattDescriptor, value: ByteArray): Boolean {
        return if (Build.VERSION.SDK_INT >= 33) {
            gatt.writeDescriptor(desc, value) == android.bluetooth.BluetoothStatusCodes.SUCCESS
        } else {
            desc.value = value
            gatt.writeDescriptor(desc)
        }
    }

    @Suppress("DEPRECATION")
    private fun writeCharCompat(
        gatt: BluetoothGatt,
        chr: BluetoothGattCharacteristic,
        data: ByteArray,
        writeType: Int
    ): Boolean {
        return if (Build.VERSION.SDK_INT >= 33) {
            gatt.writeCharacteristic(chr, data, writeType) == android.bluetooth.BluetoothStatusCodes.SUCCESS
        } else {
            chr.writeType = writeType
            chr.value = data
            gatt.writeCharacteristic(chr)
        }
    }

    // ---- HID++ 18B 트랜잭션 및 이벤트 ----------------------------------

    private fun onNotify(peer: BlePeer, v: ByteArray) {
        if (verbose) Bus.log("[BLE<-] ${peer.btName} ${Hidpp.hex(v)}")

        // 1) 진행 중인 동기 요청 응답 매칭
        val w = peer.waiter
        if (w != null && v.size >= 2) {
            val fidx = v.u(0)
            val fs = v.u(1)
            if (fidx == Hidpp.ERR_V2 || fidx == Hidpp.ERR_V1) {
                w.resp = null
                w.latch.countDown()
                return
            }
            if (fidx == w.featIdx && fs == w.funcSw) {
                if (w.echo == null || (v.size >= 3 && v.u(2) == w.echo)) {
                    w.resp = v
                    w.latch.countDown()
                    return
                }
            }
        }

        // 2) 비동기 이벤트 (swID == 0)
        if (v.size >= 4 && (v.u(1) and 0x0F) == 0x00 && v.u(0) < 0x80) {
            val fidx = v.u(0)
            val kb = keyboard
            val ms = mouse
            if (kb != null && peer === kb && fidx == kb.info.changeHostIdx) {
                onKeyboardChangeHost(kb, ms, v.u(2), v.u(3))
                return
            }
            if (ms != null && peer === ms && fidx == ms.info.changeHostIdx) {
                ms.info.currentHost = v.u(3)
                Bus.log("[이벤트] 마우스가 스스로 채널${v.u(2) + 1} -> 채널${v.u(3) + 1}")
                updateUiState()
                return
            }
        }
    }

    private fun transact(
        peer: BlePeer,
        featIdx: Int,
        func: Int,
        params: IntArray,
        timeoutMs: Long = 1200,
        echo: Int? = null
    ): ByteArray? {
        val gatt = peer.gatt ?: return null
        val chr = peer.chr ?: return null
        if (!peer.connected) return null

        val sw = nextSw()
        val fs = ((func and 0x0F) shl 4) or (sw and 0x0F)
        val pkt = ByteArray(BLE_PKT_LEN)
        pkt[0] = featIdx.toByte()
        pkt[1] = fs.toByte()
        for (i in params.indices) {
            if (2 + i >= BLE_PKT_LEN) break
            pkt[2 + i] = params[i].toByte()
        }

        synchronized(peer.writeLock) {
            val waiter = RespWaiter(featIdx, fs, echo)
            peer.waiter = waiter
            val writeLatch = CountDownLatch(1)
            peer.writeLatch = writeLatch
            try {
                val ok = writeCharCompat(gatt, chr, pkt, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT)
                if (!ok) return null
                writeLatch.await(500, TimeUnit.MILLISECONDS)
                return if (waiter.latch.await(timeoutMs, TimeUnit.MILLISECONDS)) waiter.resp else null
            } finally {
                if (peer.waiter === waiter) peer.waiter = null
            }
        }
    }

    private fun sendSetHost(peer: BlePeer, host: Int): Boolean {
        val gatt = peer.gatt ?: return false
        val chr = peer.chr ?: return false
        if (!peer.connected) return false

        val sw = nextSw()
        val pkt = ByteArray(BLE_PKT_LEN)
        pkt[0] = peer.info.changeHostIdx.toByte()
        pkt[1] = (((Hidpp.CH_SET_CURRENT_HOST and 0x0F) shl 4) or (sw and 0x0F)).toByte()
        pkt[2] = host.toByte()

        val writeType = if ((chr.properties and BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE) != 0) {
            BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
        } else {
            BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
        }
        return writeCharCompat(gatt, chr, pkt, writeType)
    }

    private fun loadPeer(peer: BlePeer): Boolean {
        val ping = transact(peer, 0x00, Hidpp.IROOT_PING, intArrayOf(0, 0, 0xAA), timeoutMs = 1500)
            ?: return false
        val d = peer.info
        val ni = transact(peer, 0x00, Hidpp.IROOT_GET_FEATURE, intArrayOf(0x00, 0x05, 0))?.u(2) ?: 0
        if (ni != 0) {
            transact(peer, ni, Hidpp.NAME_GET_COUNT, intArrayOf(0, 0, 0))?.let { c ->
                val count = c.u(2)
                val sb = StringBuilder()
                var pos = 0
                while (pos < count && pos < 64) {
                    val nr = transact(peer, ni, Hidpp.NAME_GET_NAME, intArrayOf(pos, 0, 0)) ?: break
                    val take = minOf(16, count - pos)
                    for (i in 0 until take) {
                        val ch = nr.u(2 + i)
                        if (ch in 0x20..0x7E) sb.append(ch.toChar())
                    }
                    pos += 16
                }
                d.name = sb.toString().trim()
            }
            transact(peer, ni, Hidpp.NAME_GET_TYPE, intArrayOf(0, 0, 0))?.let {
                d.typeCode = it.u(2)
            }
        }
        d.changeHostIdx = transact(peer, 0x00, Hidpp.IROOT_GET_FEATURE, intArrayOf(0x18, 0x14, 0))?.u(2) ?: 0
        d.hostsInfoIdx = transact(peer, 0x00, Hidpp.IROOT_GET_FEATURE, intArrayOf(0x18, 0x15, 0))?.u(2) ?: 0

        if (d.changeHostIdx != 0) {
            transact(peer, d.changeHostIdx, Hidpp.CH_GET_HOST_INFO, intArrayOf(0, 0, 0))?.let {
                d.hostCount = it.u(2)
                d.currentHost = it.u(3)
            }
        }
        val n = d.hostCount ?: 0
        if (d.hostsInfoIdx != 0 && n > 0) {
            for (h in 0 until n) {
                val r = transact(peer, d.hostsInfoIdx, Hidpp.HI_GET_HOST_DESC, intArrayOf(h, 0, 0), echo = h)
                    ?: continue
                var to = 11
                while (to > 3 && r.u(to - 1) == 0) to--
                if (to > 3) d.descriptors[h] = Hidpp.hex(r, 3, to)
            }
        }

        if (d.isKeyboard && d.changeHostIdx != 0) {
            keyboard = peer
            kbLastHost = d.currentHost
        } else if (d.isMouse && d.changeHostIdx != 0) {
            mouse = peer
        }

        Bus.log("  [BLE++] %s  HID++ %d.%d  현재 %s  ChangeHost=0x%02X".format(
            d.label, ping.u(2), ping.u(3), chan(d.currentHost), d.changeHostIdx
        ))
        for ((h, desc) in d.descriptors) {
            Bus.log("           채널${h + 1}  $desc" + if (h == d.currentHost) "  <- 현재" else "")
        }
        updateUiState()
        return true
    }

    // ---- 추종 판단 -----------------------------------------------------

    private fun onKeyboardChangeHost(kb: BlePeer, ms: BlePeer?, from: Int, dest: Int) {
        kbLastHost = dest
        kb.info.currentHost = dest
        if (System.currentTimeMillis() < suppressFollowUntil) return

        if (ms == null || !ms.connected) {
            Bus.log("[이벤트] 키보드 채널${from + 1} -> 채널${dest + 1} (연결된 마우스 없음)")
            return
        }
        val target = resolveMouseHost(kb.info, ms.info, dest)
        val already = target != null && ms.info.currentHost == target

        if (target != null && !already) {
            switchMouse(ms, target, "따라감")
        }

        Bus.log("[이벤트] 키보드 채널${from + 1} -> 채널${dest + 1}")
        when {
            target == null -> Bus.log("      -> 대응하는 마우스 채널이 없습니다 (키보드 채널${dest + 1})")
            already -> Bus.log("      -> 마우스는 이미 채널${target + 1} 입니다")
        }
    }

    private fun resolveMouseHost(kb: DevInfo?, m: DevInfo, kbHost: Int): Int? {
        val desc = kb?.descriptors?.get(kbHost)
        if (desc != null) {
            val matches = m.hostsWith(desc)
            if (matches.isNotEmpty()) return matches[0]
            return null
        }
        val n = m.hostCount ?: 0
        return if (kbHost < n) kbHost else null
    }

    private fun switchMouse(ms: BlePeer, host: Int, why: String) {
        if (dryRun) {
            Bus.log("      [시험] $why: 마우스 -> 채널${host + 1} (전송 안 함)")
            return
        }
        val ok = sendSetHost(ms, host)
        if (ok) {
            ms.info.currentHost = host
            Bus.log("      $why: 마우스 -> 채널${host + 1}")
            updateUiState()
        } else {
            Bus.log("      전송 실패: 마우스 -> 채널${host + 1}")
        }
    }

    // ---- 메인 루프 -----------------------------------------------------

    private fun loop(adapter: BluetoothAdapter) {
        Bus.log("BLE++ 대기 중. 키보드 Easy-Switch 키를 누르면 마우스가 따라갑니다.")
        var lastScan = System.currentTimeMillis()
        while (alive) {
            val addr = newlyReady.poll(200, TimeUnit.MILLISECONDS)
            if (addr != null) {
                peers[addr]?.let { if (it.connected) loadPeer(it) }
            }

            while (true) {
                val c = queue.poll() ?: break
                handleCommand(adapter, c)
                if (!alive) return
            }

            // 끊겼다가 돌아온 기기 자동 재연결 (2초 간격)
            val now = System.currentTimeMillis()
            if (now - lastScan >= 2000) {
                lastScan = now
                val kbOk = keyboard?.connected == true
                val msOk = mouse?.connected == true
                if (!kbOk || !msOk) {
                    scanBonded(adapter, onlyKnown = knownLogi.isNotEmpty())
                }
            }
        }
    }

    private fun handleCommand(adapter: BluetoothAdapter, c: EngineCmd) {
        when (c) {
            is EngineCmd.Stop -> alive = false
            is EngineCmd.Rescan -> {
                Bus.log("블루투스 기기 재검색...")
                scanBonded(adapter, onlyKnown = false)
            }
            is EngineCmd.Switch -> {
                val ms = mouse
                if (ms == null || !ms.connected) Bus.log("연결된 마우스가 없습니다.")
                else switchMouse(ms, c.host, "수동")
            }
            is EngineCmd.SwitchPair -> switchPair(c.host, c.why)
            is EngineCmd.Sync -> {
                val kb = keyboard
                val ms = mouse
                if (ms == null || !ms.connected) {
                    Bus.log("연결된 마우스가 없습니다.")
                    return
                }
                val dest = kbLastHost
                if (dest == null) {
                    Bus.log("키보드 채널 정보가 없습니다.")
                    return
                }
                val target = resolveMouseHost(kb?.info, ms.info, dest)
                if (target == null) {
                    Bus.log("키보드 채널${dest + 1} 에 대응하는 마우스 채널이 없습니다.")
                    return
                }
                if (ms.info.currentHost == target) {
                    Bus.log("마우스는 이미 채널${target + 1} 입니다.")
                    return
                }
                switchMouse(ms, target, "맞추기")
            }
        }
    }

    private fun switchPair(kbHost: Int, why: String) {
        val kb = keyboard?.takeIf { it.connected }
        val ms = mouse?.takeIf { it.connected }
        if (kb == null && ms == null) {
            Bus.log("[$why] 연결된 BLE 기기가 없습니다.")
            return
        }
        suppressFollowUntil = System.currentTimeMillis() + 1500L
        val msHost = if (ms != null) resolveMouseHost(kb?.info, ms.info, kbHost) ?: kbHost else kbHost
        if (dryRun) {
            Bus.log("[$why/시험] BLE 마우스 -> 채널${msHost + 1}, 키보드 -> 채널${kbHost + 1} (전송 안 함)")
            return
        }
        if (ms != null) {
            switchMouse(ms, msHost, why)
            if (kb != null) Thread.sleep(30)
        }
        if (kb != null) {
            val ok = sendSetHost(kb, kbHost)
            if (ok) {
                kb.info.currentHost = kbHost
                kbLastHost = kbHost
                Bus.log("      $why: 키보드 -> 채널${kbHost + 1}")
                updateUiState()
            } else {
                Bus.log("      전송 실패: 키보드 -> 채널${kbHost + 1}")
            }
        }
    }

    private fun chan(host: Int?) = if (host == null) "?" else "채널${host + 1}"

    private fun updateUiState() {
        val kb = keyboard?.takeIf { it.connected }?.info
        val ms = mouse?.takeIf { it.connected }?.info
        val mapping = if (kb != null && ms != null) {
            kb.descriptors.keys.sorted().map { kh ->
                val target = kb.descriptors[kh]!!
                val matches = ms.hostsWith(target)
                when {
                    matches.isEmpty() -> "키보드 채널${kh + 1}  ->  대응 없음"
                    matches.size == 1 -> "키보드 채널${kh + 1}  ->  마우스 채널${matches[0] + 1}"
                    else -> "키보드 채널${kh + 1}  ->  마우스 채널 " +
                        matches.joinToString(", ") { (it + 1).toString() } + " (중복)"
                }
            }
        } else emptyList()

        Bus.setState {
            it.copy(
                keyboard = kb?.let { k -> "BLE  ${k.label}  현재 ${chan(k.currentHost)}" },
                mouse = ms?.let { m -> "BLE  ${m.label}  현재 ${chan(m.currentHost)}" },
                mapping = mapping,
                problem = when {
                    kb == null && ms == null -> "블루투스로 연결된 로지텍 기기를 기다리는 중..."
                    kb == null -> "키보드가 이 폰에 블루투스로 연결돼 있어야 합니다."
                    ms == null -> "마우스가 이 폰에 블루투스로 연결돼 있어야 합니다."
                    else -> null
                }
            )
        }
    }

    private fun closeAll() {
        for (p in peers.values) {
            try { p.gatt?.disconnect() } catch (_: Exception) {}
            try { p.gatt?.close() } catch (_: Exception) {}
        }
        peers.clear()
    }
}
