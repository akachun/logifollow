package dev.logifollow

/**
 * HID++ 상수와 패킷 조립/해석.
 *
 * 값은 전부 맥에서 실제 장비로 검증한 것이다 (HANDOFF.md 참조).
 * feature index 와 리시버 슬롯 번호는 기기마다 다르므로 절대 여기에 넣지 않는다.
 */
object Hidpp {

    const val VID_LOGITECH = 0x046D

    // 리포트
    const val REPORT_SHORT = 0x10
    const val REPORT_LONG = 0x11
    const val SHORT_LEN = 7
    const val LONG_LEN = 20

    /** 0 이 아닌 4비트 값. 응답의 swID 가 0이면 요청 응답이 아니라 이벤트다. */
    const val SW_ID = 0x05

    /** 리시버 자신을 가리키는 device index. 블루투스 직결에서는 기기 자신. */
    const val RECEIVER_IDX = 0xFF

    // HID++ 1.0 레지스터 (리시버 전용)
    const val SUB_SET_REGISTER = 0x80
    const val SUB_GET_REGISTER = 0x81
    const val REG_NOTIFICATIONS = 0x00
    const val FLAG_WIRELESS = 0x000100
    const val FLAG_SW_PRESENT = 0x000800

    // 리시버 알림
    const val NOTIF_DISCONNECTION = 0x40
    const val NOTIF_CONNECTION = 0x41
    const val LINK_DOWN_BIT = 0x40

    // feature id
    const val FEAT_ROOT = 0x0000
    const val FEAT_FEATURE_SET = 0x0001
    const val FEAT_DEVICE_NAME = 0x0005
    const val FEAT_CHANGE_HOST = 0x1814
    const val FEAT_HOSTS_INFO = 0x1815

    // function 번호
    const val IROOT_GET_FEATURE = 0
    const val IROOT_PING = 1
    const val FSET_GET_COUNT = 0
    const val FSET_GET_FEATURE_ID = 1
    const val NAME_GET_COUNT = 0
    const val NAME_GET_NAME = 1
    const val NAME_GET_TYPE = 2
    const val CH_GET_HOST_INFO = 0
    const val CH_SET_CURRENT_HOST = 1
    const val HI_GET_HOST_INFO = 1
    const val HI_GET_HOST_DESC = 2

    // 에러 응답의 featureIndex 자리
    const val ERR_V2 = 0x8F
    const val ERR_V1 = 0xFF

    const val TYPE_KEYBOARD = 0
    const val TYPE_MOUSE = 3

    private val DEVICE_TYPES = mapOf(
        0 to "키보드", 1 to "리모컨", 2 to "숫자패드", 3 to "마우스",
        4 to "터치패드", 5 to "트랙볼", 6 to "프리젠터", 7 to "리시버"
    )
    private val NOTIF_KIND = mapOf(
        1 to "키보드", 2 to "마우스", 3 to "숫자패드", 8 to "트랙볼", 9 to "터치패드"
    )
    private val ERR_NAMES = mapOf(
        0x01 to "UNKNOWN", 0x02 to "INVALID_ARGUMENT", 0x03 to "OUT_OF_RANGE",
        0x04 to "HW_ERROR/CONNECT_FAIL", 0x05 to "LOGITECH_INTERNAL",
        0x06 to "INVALID_FEATURE_INDEX", 0x07 to "INVALID_FUNCTION_ID",
        0x08 to "BUSY", 0x09 to "UNSUPPORTED"
    )

    fun deviceType(code: Int?): String = DEVICE_TYPES[code] ?: "?"
    fun notifKind(code: Int): String = NOTIF_KIND[code] ?: "?"
    fun errName(code: Int): String = ERR_NAMES[code] ?: "0x%02X".format(code)

    /** [reportID, deviceIndex, b2, b3, ...params] 를 고정 길이로 채운다. */
    fun build(reportId: Int, index: Int, b2: Int, b3: Int, params: IntArray): ByteArray {
        val len = if (reportId == REPORT_SHORT) SHORT_LEN else LONG_LEN
        val out = ByteArray(len)
        out[0] = reportId.toByte()
        out[1] = index.toByte()
        out[2] = b2.toByte()
        out[3] = b3.toByte()
        for (i in params.indices) {
            if (4 + i >= len) break
            out[4 + i] = params[i].toByte()
        }
        return out
    }

    /** HID++ 2.0 요청. b3 = (func << 4) | swID */
    fun request(reportId: Int, index: Int, featIdx: Int, func: Int, params: IntArray): ByteArray =
        build(reportId, index, featIdx, ((func and 0x0F) shl 4) or SW_ID, params)

    fun hex(b: ByteArray, from: Int = 0, to: Int = b.size): String {
        val sb = StringBuilder()
        for (i in from until minOf(to, b.size)) {
            if (sb.isNotEmpty()) sb.append(' ')
            sb.append("%02X".format(b[i].toInt() and 0xFF))
        }
        return sb.toString()
    }
}

/** 부호 없는 바이트 읽기. 코틀린 Byte 는 signed 라 매번 필요하다. */
fun ByteArray.u(i: Int): Int = if (i < size) this[i].toInt() and 0xFF else 0
