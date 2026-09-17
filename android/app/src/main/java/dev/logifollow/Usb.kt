package dev.logifollow

import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager

object Usb {

    /** HID++ 리시버로 보이는 로지텍 기기. 이름이 주어지면 그것을 우선한다. */
    fun find(usb: UsbManager, preferName: String? = null): UsbDevice? {
        val all = usb.deviceList.values
        if (preferName != null) {
            all.firstOrNull { it.deviceName == preferName }?.let { return it }
        }
        val logi = all.filter { it.vendorId == Hidpp.VID_LOGITECH && hasHid(it) }
        // 권한이 이미 있는 쪽을 먼저 고른다
        return logi.firstOrNull { usb.hasPermission(it) } ?: logi.firstOrNull()
    }

    fun candidates(usb: UsbManager): List<UsbDevice> =
        usb.deviceList.values.filter { it.vendorId == Hidpp.VID_LOGITECH && hasHid(it) }

    private fun hasHid(d: UsbDevice): Boolean =
        (0 until d.interfaceCount).any { d.getInterface(it).interfaceClass == UsbConstants.USB_CLASS_HID }
}
