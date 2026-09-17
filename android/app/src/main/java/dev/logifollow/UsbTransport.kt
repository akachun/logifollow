package dev.logifollow

import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager

/**
 * 리시버의 HID++ 인터페이스를 찾아 열고, 리포트를 주고받는다.
 *
 * 인터페이스 번호도 쓰기 방식도 하드코딩하지 않는다. 후보를 하나씩
 * 잡아 실제로 리시버에게 말을 걸어보고, 대답하는 조합을 채택한다.
 * 맥에서 2번이었다고 안드로이드에서도 2번이라는 보장이 없다.
 */
class UsbTransport private constructor(
    val device: UsbDevice,
    private val conn: UsbDeviceConnection,
    private val iface: UsbInterface,
    private val epIn: UsbEndpoint,
    private val epOut: UsbEndpoint?,
    private val mode: WriteMode
) {
    enum class WriteMode { INTERRUPT_OUT, CONTROL_WITH_ID, CONTROL_NO_ID }

    val description: String
        get() = "인터페이스 %d, %s, IN=0x%02X(%dB)".format(
            iface.id, modeLabel(mode), epIn.address, epIn.maxPacketSize
        )

    @Volatile private var closed = false

    /** 한 리포트를 읽는다. 타임아웃이면 null. */
    fun read(timeoutMs: Int): ByteArray? {
        if (closed) return null
        val buf = ByteArray(maxOf(epIn.maxPacketSize, 32))
        val n = try {
            conn.bulkTransfer(epIn, buf, buf.size, timeoutMs)
        } catch (e: Exception) {
            -1
        }
        if (n <= 0) return null
        return buf.copyOf(n)
    }

    /** 큐에 남은 오래된 리포트를 버린다. */
    fun drain() {
        while (read(1) != null) { /* 버린다 */ }
    }

    fun write(pkt: ByteArray): Boolean {
        if (closed) return false
        return try {
            when (mode) {
                WriteMode.INTERRUPT_OUT ->
                    conn.bulkTransfer(epOut!!, pkt, pkt.size, WRITE_TIMEOUT_MS) >= 0
                WriteMode.CONTROL_WITH_ID ->
                    ctrl(pkt.u(0), pkt) >= 0
                WriteMode.CONTROL_NO_ID ->
                    ctrl(pkt.u(0), pkt.copyOfRange(1, pkt.size)) >= 0
            }
        } catch (e: Exception) {
            false
        }
    }

    private fun ctrl(reportId: Int, data: ByteArray): Int = conn.controlTransfer(
        REQ_TYPE_SET, HID_SET_REPORT,
        (HID_REPORT_TYPE_OUTPUT shl 8) or reportId,
        iface.id, data, data.size, WRITE_TIMEOUT_MS
    )

    fun close() {
        if (closed) return
        closed = true
        try { conn.releaseInterface(iface) } catch (_: Exception) {}
        try { conn.close() } catch (_: Exception) {}
    }

    companion object {
        private const val WRITE_TIMEOUT_MS = 500
        private const val HID_SET_REPORT = 0x09
        private const val HID_REPORT_TYPE_OUTPUT = 0x02
        private const val REQ_TYPE_SET = 0x21 // host->device | class | interface

        fun modeLabel(m: WriteMode) = when (m) {
            WriteMode.INTERRUPT_OUT -> "인터럽트 OUT"
            WriteMode.CONTROL_WITH_ID -> "컨트롤 SET_REPORT(ID 포함)"
            WriteMode.CONTROL_NO_ID -> "컨트롤 SET_REPORT(ID 제외)"
        }

        /** 클레임 없이 읽을 수 있는 정보만으로 기기 구조를 적는다. 진단용. */
        fun describe(dev: UsbDevice): String {
            val sb = StringBuilder()
            sb.append("%s  VID=0x%04X PID=0x%04X\n".format(
                dev.productName ?: dev.deviceName, dev.vendorId, dev.productId))
            for (i in 0 until dev.interfaceCount) {
                val f = dev.getInterface(i)
                sb.append("  iface %d  class=%d sub=%d proto=%d  ep=%d\n".format(
                    f.id, f.interfaceClass, f.interfaceSubclass,
                    f.interfaceProtocol, f.endpointCount))
                for (j in 0 until f.endpointCount) {
                    val e = f.getEndpoint(j)
                    sb.append("      0x%02X %s %s max=%d\n".format(
                        e.address,
                        if (e.direction == UsbConstants.USB_DIR_IN) "IN " else "OUT",
                        epTypeName(e.type), e.maxPacketSize))
                }
            }
            return sb.toString().trimEnd()
        }

        private fun epTypeName(t: Int) = when (t) {
            UsbConstants.USB_ENDPOINT_XFER_INT -> "interrupt"
            UsbConstants.USB_ENDPOINT_XFER_BULK -> "bulk"
            UsbConstants.USB_ENDPOINT_XFER_CONTROL -> "control"
            else -> "iso"
        }

        /**
         * HID++ 로 대화가 되는 인터페이스를 찾는다.
         *
         * 부팅 키보드/마우스 인터페이스를 잡으면 그동안 입력이 죽으므로,
         * 벤더 인터페이스일 가능성이 높은 순서(32바이트 IN, 높은 번호)로
         * 시도하고 실패하면 즉시 놓아준다.
         */
        fun open(usb: UsbManager, dev: UsbDevice, log: (String) -> Unit): UsbTransport? {
            val conn = usb.openDevice(dev)
            if (conn == null) {
                log("기기를 열지 못했습니다. USB 권한을 확인하세요.")
                return null
            }

            val candidates = (0 until dev.interfaceCount)
                .map { dev.getInterface(it) }
                .filter { it.interfaceClass == UsbConstants.USB_CLASS_HID }
                .mapNotNull { f ->
                    val ins = (0 until f.endpointCount).map { f.getEndpoint(it) }
                        .filter {
                            it.direction == UsbConstants.USB_DIR_IN &&
                                it.type == UsbConstants.USB_ENDPOINT_XFER_INT
                        }
                    val outs = (0 until f.endpointCount).map { f.getEndpoint(it) }
                        .filter {
                            it.direction == UsbConstants.USB_DIR_OUT &&
                                it.type == UsbConstants.USB_ENDPOINT_XFER_INT
                        }
                    if (ins.isEmpty()) null else Triple(f, ins[0], outs.firstOrNull())
                }
                // HID++ 벤더 인터페이스는 long 리포트(20B)를 받아야 해서 IN 이 크다.
                // 부팅 키보드는 8, 마우스는 8~16 이다.
                .sortedWith(
                    compareByDescending<Triple<UsbInterface, UsbEndpoint, UsbEndpoint?>> {
                        if (it.second.maxPacketSize >= 20) 1 else 0
                    }.thenByDescending { it.first.id }
                )

            if (candidates.isEmpty()) {
                log("HID 인터페이스가 없습니다.")
                conn.close()
                return null
            }

            // 1차: 리시버 레지스터 읽기만. 빠르다.
            // 2차: 그래도 못 찾으면 슬롯 ping 까지 훑는다.
            //
            // 부팅 키보드/마우스 인터페이스를 잡고 있는 동안은 그 입력이 죽으므로
            // 느린 탐색을 처음부터 돌리지 않는다.
            for (deep in listOf(false, true)) {
                for ((f, epIn, epOut) in candidates) {
                    if (!conn.claimInterface(f, true)) {
                        log("iface %d 점유 실패".format(f.id))
                        continue
                    }
                    val modes = buildList {
                        if (epOut != null) add(WriteMode.INTERRUPT_OUT)
                        add(WriteMode.CONTROL_WITH_ID)
                        add(WriteMode.CONTROL_NO_ID)
                    }
                    for (m in modes) {
                        val t = UsbTransport(dev, conn, f, epIn, epOut, m)
                        if (t.handshake(deep)) {
                            log("HID++ 연결: " + t.description)
                            return t
                        }
                    }
                    conn.releaseInterface(f)
                }
                if (!deep) log("빠른 탐색 실패. 슬롯 ping 까지 훑어봅니다.")
            }
            log("HID++ 인터페이스를 찾지 못했습니다.")
            conn.close()
            return null
        }
    }

    /**
     * 이 조합으로 리시버가 대답하는지 본다.
     *
     * 알림 레지스터 읽기는 기기가 자고 있어도 리시버 자신이 답하므로
     * 판별에 가장 확실하다. [deep] 일 때만 슬롯 ping 까지 간다.
     */
    private fun handshake(deep: Boolean): Boolean {
        drain()
        val pkt = Hidpp.build(
            Hidpp.REPORT_SHORT, Hidpp.RECEIVER_IDX,
            Hidpp.SUB_GET_REGISTER, Hidpp.REG_NOTIFICATIONS, intArrayOf(0, 0, 0)
        )
        if (write(pkt) && awaitAny(400) { it.u(1) == Hidpp.RECEIVER_IDX }) return true
        if (!deep) return false

        for (slot in 1..6) {
            drain()
            val ping = Hidpp.request(
                Hidpp.REPORT_SHORT, slot, 0x00, Hidpp.IROOT_PING, intArrayOf(0, 0, 0xAA)
            )
            if (write(ping) && awaitAny(250) { it.u(1) == slot }) return true
        }
        return false
    }

    private inline fun awaitAny(timeoutMs: Int, match: (ByteArray) -> Boolean): Boolean {
        val end = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < end) {
            val r = read(maxOf(1, (end - System.currentTimeMillis()).toInt())) ?: continue
            if (r.size >= 4 && (r.u(0) == Hidpp.REPORT_SHORT || r.u(0) == Hidpp.REPORT_LONG)
                && match(r)
            ) return true
        }
        return false
    }
}
