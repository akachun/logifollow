package dev.logifollow

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.hardware.usb.UsbManager
import android.os.Build
import android.os.IBinder

/**
 * 리시버 연결을 붙들고 이벤트 루프를 돌리는 포그라운드 서비스.
 * 화면을 꺼도, 다른 앱을 써도 계속 감시해야 하므로 액티비티에 둘 수 없다.
 */
class FollowService : Service() {

    companion object {
        const val ACTION_START = "dev.logifollow.START"
        const val ACTION_STOP = "dev.logifollow.STOP"
        const val ACTION_RESCAN = "dev.logifollow.RESCAN"
        const val ACTION_SYNC = "dev.logifollow.SYNC"
        const val ACTION_SWITCH = "dev.logifollow.SWITCH"
        const val EXTRA_DEVICE_NAME = "deviceName"
        const val EXTRA_HOST = "host"
        const val EXTRA_DRY_RUN = "dryRun"
        const val EXTRA_VERBOSE = "verbose"

        private const val CHANNEL_ID = "follow"
        private const val NOTIF_ID = 1

        @Volatile
        private var engine: FollowEngine? = null

        val isRunning: Boolean get() = engine != null

        fun start(ctx: Context, deviceName: String?, dryRun: Boolean, verbose: Boolean) {
            val i = Intent(ctx, FollowService::class.java).setAction(ACTION_START)
                .putExtra(EXTRA_DEVICE_NAME, deviceName)
                .putExtra(EXTRA_DRY_RUN, dryRun)
                .putExtra(EXTRA_VERBOSE, verbose)
            ctx.startForegroundService(i)
        }

        fun command(ctx: Context, action: String, host: Int = -1) {
            val i = Intent(ctx, FollowService::class.java).setAction(action)
            if (host >= 0) i.putExtra(EXTRA_HOST, host)
            ctx.startForegroundService(i)
        }
    }

    private var thread: Thread? = null

    /** 엔진이 아직 없을 때 들어온 명령. 엔진이 서면 그때 흘려보낸다. */
    private val deferred = mutableListOf<FollowEngine.Cmd>()

    private val detachReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context?, i: Intent?) {
            if (i?.action == UsbManager.ACTION_USB_DEVICE_DETACHED) {
                Bus.log("리시버가 뽑혔습니다.")
                shutdown()
            }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        val f = IntentFilter(UsbManager.ACTION_USB_DEVICE_DETACHED)
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(detachReceiver, f, Context.RECEIVER_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            registerReceiver(detachReceiver, f)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        goForeground("준비 중")
        when (intent?.action) {
            ACTION_START -> startEngine(intent)
            ACTION_STOP -> { shutdown(); return START_NOT_STICKY }
            ACTION_RESCAN -> submit(FollowEngine.Cmd.Rescan, intent)
            ACTION_SYNC -> submit(FollowEngine.Cmd.Sync, intent)
            ACTION_SWITCH -> {
                val h = intent.getIntExtra(EXTRA_HOST, -1)
                if (h >= 0) submit(FollowEngine.Cmd.Switch(h), intent)
            }
        }
        return START_NOT_STICKY
    }

    /**
     * 바로가기로 명령이 먼저 들어올 수 있다. 그때는 엔진을 띄우고
     * 발견이 끝난 뒤 명령이 실행되도록 큐에 걸어둔다.
     */
    private fun submit(cmd: FollowEngine.Cmd, intent: Intent) {
        val e = engine
        if (e != null) {
            e.post(cmd)
            return
        }
        Bus.log("엔진이 아직 없습니다. 먼저 리시버를 엽니다.")
        deferred.add(cmd)
        startEngine(intent)
    }

    private fun startEngine(intent: Intent) {
        if (engine != null) {
            Bus.log("이미 실행 중입니다.")
            return
        }
        val usb = getSystemService(Context.USB_SERVICE) as UsbManager
        val dev = Usb.find(usb, intent.getStringExtra(EXTRA_DEVICE_NAME))
        if (dev == null) {
            Bus.log("로지텍 리시버를 찾지 못했습니다. USB 허브 연결을 확인하세요.")
            Bus.setState { it.copy(problem = "리시버가 보이지 않습니다") }
            stopSelf()
            return
        }
        if (!usb.hasPermission(dev)) {
            Bus.log("USB 권한이 없습니다. 앱을 열어 권한을 허용하세요.")
            Bus.setState { it.copy(problem = "USB 권한 필요") }
            stopSelf()
            return
        }
        val e = FollowEngine(usb, dev).apply {
            dryRun = intent.getBooleanExtra(EXTRA_DRY_RUN, false)
            verbose = intent.getBooleanExtra(EXTRA_VERBOSE, false)
        }
        engine = e
        deferred.forEach { e.post(it) }
        deferred.clear()
        goForeground(dev.productName ?: "리시버 감시 중")
        thread = Thread(e, "logifollow-engine").apply {
            isDaemon = true
            start()
        }
        // 엔진이 스스로 끝나면 서비스도 접는다
        Thread {
            thread?.join()
            if (engine === e) {
                engine = null
                stopSelf()
            }
        }.apply { isDaemon = true }.start()
    }

    private fun shutdown() {
        engine?.stop()
        engine = null
        stopSelf()
    }

    override fun onDestroy() {
        try { unregisterReceiver(detachReceiver) } catch (_: Exception) {}
        engine?.stop()
        engine = null
        super.onDestroy()
    }

    private fun createChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL_ID) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "채널 추종", NotificationManager.IMPORTANCE_LOW)
                    .apply { setShowBadge(false) }
            )
        }
    }

    private fun goForeground(text: String) {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val n: Notification = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("LogiFollow")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_notify)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(
                Notification.Action.Builder(
                    null as android.graphics.drawable.Icon?, "정지",
                    PendingIntent.getService(
                        this, 1,
                        Intent(this, FollowService::class.java).setAction(ACTION_STOP),
                        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                    )
                ).build()
            )
            .build()
        startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
    }
}
