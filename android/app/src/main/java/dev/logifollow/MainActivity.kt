package dev.logifollow

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.PendingIntent
import android.bluetooth.BluetoothManager
import android.content.BroadcastReceiver
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.hardware.usb.UsbManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast

class MainActivity : Activity() {

    private lateinit var usb: UsbManager
    private lateinit var statusView: TextView
    private lateinit var logView: TextView
    private lateinit var logScroll: ScrollView
    private lateinit var chkDry: CheckBox
    private lateinit var chkVerbose: CheckBox
    private lateinit var chkEdge: CheckBox
    private lateinit var spEdgeLeft: Spinner
    private lateinit var spEdgeRight: Spinner
    private lateinit var spEdgeTop: Spinner
    private lateinit var spEdgeBottom: Spinner
    private var loadingEdgeUi = false

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
        chkEdge = findViewById(R.id.chkEdge)
        spEdgeLeft = findViewById(R.id.spEdgeLeft)
        spEdgeRight = findViewById(R.id.spEdgeRight)
        spEdgeTop = findViewById(R.id.spEdgeTop)
        spEdgeBottom = findViewById(R.id.spEdgeBottom)
        setupEdgeControls()

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
        if (dev != null) {
            if (usb.hasPermission(dev)) {
                startUsbService()
                return
            }
            Bus.log("USB 권한을 요청합니다: ${dev.productName ?: dev.deviceName}")
            val pi = PendingIntent.getBroadcast(
                this, 0,
                Intent(ACTION_PERMISSION).setPackage(packageName),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
            )
            usb.requestPermission(dev, pi)
            return
        }

        // USB 리시버가 없으면 블루투스 직결(BLE++) 모드로 시작
        if (!hasBlePermission()) {
            Bus.log("USB 리시버가 없어 블루투스(BLE++) 모드로 시작합니다. 근처 기기 권한을 허용하세요.")
            requestBlePermission()
            return
        }
        startBleService()
    }

    private fun startService() = startUsbService()

    private fun startUsbService() {
        val dev = Usb.find(usb) ?: return
        FollowService.start(this, dev.deviceName, chkDry.isChecked, chkVerbose.isChecked, FollowService.MODE_USB)
    }

    private fun startBleService() {
        FollowService.start(this, null, chkDry.isChecked, chkVerbose.isChecked, FollowService.MODE_BLE)
    }

    private fun hasBlePermission(): Boolean {
        if (Build.VERSION.SDK_INT < 31) return true
        return checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED
    }

    private fun requestBlePermission() {
        if (Build.VERSION.SDK_INT >= 31) {
            requestPermissions(arrayOf(Manifest.permission.BLUETOOTH_CONNECT), REQ_BLE_PERM)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_BLE_PERM) {
            if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                Bus.log("블루투스 권한을 받았습니다.")
                startBleService()
            } else {
                Bus.log("블루투스 권한이 거부되었습니다.")
            }
        }
    }

    private fun switchMouse(host: Int) {
        FollowService.command(this, FollowService.ACTION_SWITCH, host)
    }

    /**
     * USB 인터페이스 구조 및 페어링된 BLE 기기 목록을 출력한다.
     */
    @SuppressLint("MissingPermission")
    private fun diagnose() {
        val list = Usb.candidates(usb)
        if (list.isEmpty()) {
            Bus.log("[USB] 로지텍 리시버 없음 (연결된 USB 기기 ${usb.deviceList.size}개)")
        } else {
            list.forEach {
                Bus.log("[USB] " + UsbTransport.describe(it))
                Bus.log("  권한: " + if (usb.hasPermission(it)) "있음" else "없음")
            }
        }

        if (!hasBlePermission()) {
            Bus.log("[BLE] 근처 기기(BLUETOOTH_CONNECT) 권한이 없습니다.")
            return
        }
        val bm = getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager
        val bonded = bm?.adapter?.bondedDevices ?: emptySet()
        Bus.log("[BLE] 페어링된 블루투스 기기 ${bonded.size}개:")
        bonded.forEach { d ->
            Bus.log("  ${d.name ?: "(이름없음)"}  ${d.address}  type=${d.type}")
        }
    }

    private fun copyLog() {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("LogiFollow", Bus.snapshot()))
        Toast.makeText(this, "로그를 복사했습니다", Toast.LENGTH_SHORT).show()
    }

    private fun setupEdgeControls() {
        val choices = listOf("안함", "ch1", "ch2", "ch3")
        val adapter = ArrayAdapter(this, android.R.layout.simple_spinner_item, choices).apply {
            setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
        }
        spEdgeLeft.adapter = adapter
        spEdgeRight.adapter = adapter
        spEdgeTop.adapter = adapter
        spEdgeBottom.adapter = adapter

        val cfg = EdgePrefs.load(this)
        loadingEdgeUi = true
        chkEdge.isChecked = cfg.enabled
        spEdgeLeft.setSelection(if (cfg.leftHost in 0..2) cfg.leftHost + 1 else 0)
        spEdgeRight.setSelection(if (cfg.rightHost in 0..2) cfg.rightHost + 1 else 0)
        spEdgeTop.setSelection(if (cfg.topHost in 0..2) cfg.topHost + 1 else 0)
        spEdgeBottom.setSelection(if (cfg.bottomHost in 0..2) cfg.bottomHost + 1 else 0)
        loadingEdgeUi = false

        chkEdge.setOnCheckedChangeListener { _, checked ->
            if (loadingEdgeUi) return@setOnCheckedChangeListener
            if (checked && !Settings.canDrawOverlays(this)) {
                Bus.log("[엣지] '다른 앱 위에 표시' 권한을 허용해 주세요.")
                Toast.makeText(this, "'다른 앱 위에 표시' 권한을 허용한 뒤 다시 켜주세요", Toast.LENGTH_LONG).show()
                loadingEdgeUi = true
                chkEdge.isChecked = false
                loadingEdgeUi = false
                val intent = Intent(
                    Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:$packageName")
                )
                startActivity(intent)
                return@setOnCheckedChangeListener
            }
            saveAndApplyEdge()
        }

        val itemListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) {
                if (!loadingEdgeUi) saveAndApplyEdge()
            }
            override fun onNothingSelected(p: AdapterView<*>?) {}
        }
        spEdgeLeft.onItemSelectedListener = itemListener
        spEdgeRight.onItemSelectedListener = itemListener
        spEdgeTop.onItemSelectedListener = itemListener
        spEdgeBottom.onItemSelectedListener = itemListener
    }

    private fun saveAndApplyEdge() {
        val cfg = EdgePrefs.load(this).copy(
            enabled = chkEdge.isChecked,
            leftHost = spEdgeLeft.selectedItemPosition - 1,
            rightHost = spEdgeRight.selectedItemPosition - 1,
            topHost = spEdgeTop.selectedItemPosition - 1,
            bottomHost = spEdgeBottom.selectedItemPosition - 1
        )
        EdgePrefs.save(this, cfg)
        if (FollowService.isRunning) {
            FollowService.command(this, FollowService.ACTION_UPDATE_EDGE)
        } else if (cfg.enabled) {
            Bus.log("[엣지] 설정 저장됨 — '시작' 버튼을 누르면 화면 경계 감지가 활성화됩니다.")
        }
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
        private const val REQ_BLE_PERM = 8
    }
}
