package dev.logifollow

import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbManager
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.CheckBox
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

class MainActivity : Activity() {

    private lateinit var usb: UsbManager
    private lateinit var statusView: TextView
    private lateinit var logView: TextView
    private lateinit var logScroll: ScrollView
    private lateinit var chkDry: CheckBox
    private lateinit var chkVerbose: CheckBox

    private val onBus = { render() }

    private val permReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context?, i: Intent?) {
            if (i?.action != ACTION_PERMISSION) return
            val granted = i.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
            if (granted) {
                Bus.log("USB 권한을 받았습니다.")
                startService()
            } else {
                Bus.log("USB 권한이 거부되었습니다.")
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        usb = getSystemService(Context.USB_SERVICE) as UsbManager

        statusView = findViewById(R.id.status)
        logView = findViewById(R.id.log)
        logScroll = findViewById(R.id.logScroll)
        chkDry = findViewById(R.id.chkDry)
        chkVerbose = findViewById(R.id.chkVerbose)

        findViewById<Button>(R.id.btnStart).setOnClickListener { requestAndStart() }
        findViewById<Button>(R.id.btnStop).setOnClickListener {
            FollowService.command(this, FollowService.ACTION_STOP)
        }
        findViewById<Button>(R.id.btnRescan).setOnClickListener {
            FollowService.command(this, FollowService.ACTION_RESCAN)
        }
        findViewById<Button>(R.id.btnSync).setOnClickListener {
            FollowService.command(this, FollowService.ACTION_SYNC)
        }
        findViewById<Button>(R.id.btnCh1).setOnClickListener { switchMouse(0) }
        findViewById<Button>(R.id.btnCh2).setOnClickListener { switchMouse(1) }
        findViewById<Button>(R.id.btnCh3).setOnClickListener { switchMouse(2) }
        findViewById<Button>(R.id.btnDiag).setOnClickListener { diagnose() }
        findViewById<Button>(R.id.btnClear).setOnClickListener { Bus.clear() }
        findViewById<Button>(R.id.btnCopy).setOnClickListener { copyLog() }

        registerRuntimeReceiver()
        askNotificationPermission()

        // 리시버를 꽂아서 열린 경우 바로 시작한다
        if (intent?.action == UsbManager.ACTION_USB_DEVICE_ATTACHED) {
            Bus.log("리시버가 연결되었습니다.")
            requestAndStart()
        }
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        if (intent?.action == UsbManager.ACTION_USB_DEVICE_ATTACHED) requestAndStart()
    }

    override fun onResume() {
        super.onResume()
        Bus.addListener(onBus)
        render()
    }

    override fun onPause() {
        Bus.removeListener(onBus)
        super.onPause()
    }

    override fun onDestroy() {
        try { unregisterReceiver(permReceiver) } catch (_: Exception) {}
        super.onDestroy()
    }

    // ---- 동작 ---------------------------------------------------------

    private fun requestAndStart() {
        val dev = Usb.find(usb)
        if (dev == null) {
            Bus.log("로지텍 리시버가 보이지 않습니다. 허브와 리시버 연결을 확인하세요.")
            Bus.setState { it.copy(problem = "리시버 없음") }
            return
        }
        if (usb.hasPermission(dev)) {
            startService()
            return
        }
        Bus.log("USB 권한을 요청합니다: ${dev.productName ?: dev.deviceName}")
        val pi = PendingIntent.getBroadcast(
            this, 0,
            Intent(ACTION_PERMISSION).setPackage(packageName),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
        )
        usb.requestPermission(dev, pi)
    }

    private fun startService() {
        val dev = Usb.find(usb) ?: return
        FollowService.start(this, dev.deviceName, chkDry.isChecked, chkVerbose.isChecked)
    }

    private fun switchMouse(host: Int) {
        FollowService.command(this, FollowService.ACTION_SWITCH, host)
    }

    /**
     * 인터페이스와 엔드포인트 구조만 적는다. 점유하지 않으므로
     * 마우스 입력에 영향이 없다. 클레임 전에 무엇이 있는지 보는 용도.
     */
    private fun diagnose() {
        val list = Usb.candidates(usb)
        if (list.isEmpty()) {
            Bus.log("로지텍 HID 기기가 없습니다. 연결된 USB 기기 " +
                "${usb.deviceList.size}개.")
            usb.deviceList.values.forEach {
                Bus.log("  %s VID=0x%04X PID=0x%04X".format(
                    it.productName ?: it.deviceName, it.vendorId, it.productId))
            }
            return
        }
        list.forEach {
            Bus.log(UsbTransport.describe(it))
            Bus.log("  권한: " + if (usb.hasPermission(it)) "있음" else "없음")
        }
    }

    private fun copyLog() {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("LogiFollow", Bus.snapshot()))
        Toast.makeText(this, "로그를 복사했습니다", Toast.LENGTH_SHORT).show()
    }

    // ---- 화면 ---------------------------------------------------------

    private val versionLabel: String by lazy {
        try {
            "v" + packageManager.getPackageInfo(packageName, 0).versionName
        } catch (e: Exception) {
            "v?"
        }
    }

    private fun render() {
        val s = Bus.state
        val sb = StringBuilder()
        sb.append(if (s.running) "● 감시 중" else "○ 정지")
        sb.append("   ").append(versionLabel)
        s.receiver?.let { sb.append("\n리시버   ").append(it) }
        s.transport?.let { sb.append("\n전송     ").append(it) }
        s.keyboard?.let { sb.append("\n키보드   ").append(it) }
        s.mouse?.let { sb.append("\n마우스   ").append(it) }
        if (s.mapping.isNotEmpty()) {
            sb.append("\n")
            s.mapping.forEach { sb.append("\n").append(it) }
        }
        s.problem?.let { sb.append("\n\n⚠ ").append(it) }
        statusView.text = sb.toString()

        logView.text = Bus.snapshot()
        logScroll.post { logScroll.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    private fun registerRuntimeReceiver() {
        val f = IntentFilter(ACTION_PERMISSION)
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(permReceiver, f, Context.RECEIVER_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            registerReceiver(permReceiver, f)
        }
    }

    private fun askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 7)
        }
    }

    companion object {
        private const val ACTION_PERMISSION = "dev.logifollow.USB_PERMISSION"
    }
}
