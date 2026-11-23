package dev.logifollow

import android.app.Activity
import android.content.Context
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.TypedValue
import android.view.Display
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import java.lang.ref.WeakReference

/**
 * 화면 좌/우 가장자리에 3dp 폭의 투명 오버레이를 배치해
 * 마우스 커서 Hover 체류(dwell)를 감지하는 Edge Switch 매니저.
 *
 * - 상·하단 모서리 60dp 는 창 닫기/알림바 오작동 방지를 위해 제외한다.
 * - 마우스 버튼이 눌린 드래그 상태(buttonState != 0)는 무시한다.
 * - 삼성 DeX 외부 디스플레이가 연결돼 있으면 DeX 화면에도 자동 부착한다.
 */
class EdgeOverlayManager(
    private val ctx: Context,
    private val onTrigger: (edge: String, targetHost: Int) -> Unit
) {
    companion object {
        /** DeX 화면에서 실행 중인 Activity 컨텍스트 (Samsung DeX 는 applicationContext 오버레이를 무시할 수 있음). */
        @Volatile
        var hostActivity: WeakReference<Activity>? = null
    }

    private val main = Handler(Looper.getMainLooper())
    private val attached = mutableListOf<Pair<WindowManager, View>>()
    private var pendingRunnable: Runnable? = null
    private var cooldownUntil = 0L

    fun update() {
        main.post { rebuild() }
    }

    fun removeAll() {
        main.post { clearAttached() }
    }

    private fun clearAttached() {
        pendingRunnable?.let { main.removeCallbacks(it) }
        pendingRunnable = null
        for ((wm, v) in attached) {
            try { wm.removeViewImmediate(v) } catch (_: Exception) {}
        }
        attached.clear()
    }

    @Suppress("DEPRECATION")
    private fun rebuild() {
        clearAttached()
        val cfg = EdgePrefs.load(ctx)
        val canDraw = Settings.canDrawOverlays(ctx)
        Bus.log("[엣지] 갱신: enabled=${cfg.enabled} 권한=$canDraw 좌=${cfg.leftHost + 1} 우=${cfg.rightHost + 1} 상=${cfg.topHost + 1} 하=${cfg.bottomHost + 1}")
        if (!cfg.enabled) return
        if (cfg.leftHost < 0 && cfg.rightHost < 0 && cfg.topHost < 0 && cfg.bottomHost < 0) {
            Bus.log("[엣지] 설정된 방향이 없습니다. 좌/우/상/하 중 채널을 선택하세요.")
            return
        }
        if (!canDraw) {
            Bus.log("[엣지] '다른 앱 위에 표시' 권한이 없어 화면 경계 감지를 켤 수 없습니다.")
            return
        }

        val dm = ctx.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager
        val allDisplays = dm.displays
        Bus.log("[엣지] 디스플레이 목록: " + allDisplays.joinToString(", ") {
            "id=${it.displayId}(${it.name},state=${it.state})"
        })

        // 1순위: 현재 DeX/폰 화면에 떠 있는 Activity 의 WindowManager 직접 사용
        // Samsung DeX (Android 10) 에서는 applicationContext 기반 오버레이가 DeX 화면에
        // 렌더링되지 않고 폰 화면으로 빠지는 경우가 있어 Activity 의 WindowManager 가 가장 확실하다.
        val act = hostActivity?.get()?.takeIf { !it.isFinishing && !it.isDestroyed }
        val targets = mutableListOf<Triple<Context, WindowManager, Int>>()

        if (act != null) {
            val actDisp = if (Build.VERSION.SDK_INT >= 30) act.display else act.windowManager.defaultDisplay
            val dispId = actDisp?.displayId ?: 0
            val dCtx = if (actDisp != null) act.createDisplayContext(actDisp) else act
            val wm = dCtx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
            targets.add(Triple(dCtx, wm, dispId))
        } else {
            val active = allDisplays.filter { it.state != Display.STATE_OFF }
            val list = if (active.isEmpty()) listOfNotNull(dm.getDisplay(Display.DEFAULT_DISPLAY)) else active
            for (disp in list) {
                try {
                    val base = ctx.createDisplayContext(disp)
                    val dCtx = if (Build.VERSION.SDK_INT >= 30) {
                        base.createWindowContext(WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY, null)
                    } else {
                        base
                    }
                    val wm = dCtx.getSystemService(Context.WINDOW_SERVICE) as? WindowManager ?: continue
                    targets.add(Triple(dCtx, wm, disp.displayId))
                } catch (e: Exception) {
                    Bus.log("[엣지] 디스플레이(${disp.displayId}) 컨텍스트 생성 실패: ${e.message}")
                }
            }
        }

        var count = 0
        for ((dCtx, wm, dispId) in targets) {
            if (cfg.leftHost >= 0) {
                if (attachEdge(dCtx, wm, dispId, Gravity.START, false, "왼쪽", cfg.leftHost, cfg.dwellMs, cfg.cooldownMs)) count++
            }
            if (cfg.rightHost >= 0) {
                if (attachEdge(dCtx, wm, dispId, Gravity.END, false, "오른쪽", cfg.rightHost, cfg.dwellMs, cfg.cooldownMs)) count++
            }
            if (cfg.topHost >= 0) {
                if (attachEdge(dCtx, wm, dispId, Gravity.TOP, true, "위쪽", cfg.topHost, cfg.dwellMs, cfg.cooldownMs)) count++
            }
            if (cfg.bottomHost >= 0) {
                if (attachEdge(dCtx, wm, dispId, Gravity.BOTTOM, true, "아래쪽", cfg.bottomHost, cfg.dwellMs, cfg.cooldownMs)) count++
            }
        }
        if (count > 0) {
            Bus.log("[엣지] 화면 경계 감지 활성화 (오버레이 ${count}개, 체류 ${cfg.dwellMs}ms)")
        }
    }

    private fun dp(dCtx: Context, v: Float): Int =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, dCtx.resources.displayMetrics).toInt()

    private fun attachEdge(
        dCtx: Context,
        wm: WindowManager,
        displayId: Int,
        edgeGravity: Int,
        horizontal: Boolean,
        edgeLabel: String,
        targetHost: Int,
        dwellMs: Long,
        cooldownMs: Long
    ): Boolean {
        val metrics = dCtx.resources.displayMetrics
        val deadzone = dp(dCtx, 60f)
        val thickness = maxOf(dp(dCtx, 10f), 14)
        val w = if (horizontal) maxOf(metrics.widthPixels - deadzone * 2, dp(dCtx, 120f)) else thickness
        val h = if (horizontal) thickness else maxOf(metrics.heightPixels - deadzone * 2, dp(dCtx, 120f))
        val grav = edgeGravity or (if (horizontal) Gravity.CENTER_HORIZONTAL else Gravity.CENTER_VERTICAL)

        val lp = WindowManager.LayoutParams(
            w,
            h,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT
        ).apply {
            this.gravity = grav
            this.x = 0
            this.y = 0
            if (Build.VERSION.SDK_INT >= 28) {
                layoutInDisplayCutoutMode =
                    WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
            }
        }

        val view = View(dCtx)
        // 위치 확인용 반투명 초록 가이드선 (동작 확인 후 투명화 예정)
        view.setBackgroundColor(0x5500E676)

        val hoverHandler: (MotionEvent) -> Boolean = { event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_HOVER_ENTER, MotionEvent.ACTION_HOVER_MOVE -> {
                    if (event.buttonState != 0) {
                        cancelPending()
                    } else if (System.currentTimeMillis() >= cooldownUntil && pendingRunnable == null) {
                        Bus.log("[엣지] ${edgeLabel} 경계 진입 (${dwellMs}ms 대기)")
                        val r = Runnable {
                            pendingRunnable = null
                            cooldownUntil = System.currentTimeMillis() + cooldownMs
                            onTrigger(edgeLabel, targetHost)
                        }
                        pendingRunnable = r
                        main.postDelayed(r, dwellMs)
                    }
                }
                MotionEvent.ACTION_HOVER_EXIT -> cancelPending()
            }
            false
        }
        view.setOnHoverListener { _, event -> hoverHandler(event) }
        view.setOnGenericMotionListener { _, event -> hoverHandler(event) }

        return try {
            wm.addView(view, lp)
            attached.add(wm to view)
            Bus.log("[엣지] 부착 성공: $edgeLabel (${w}x${h}px, display=$displayId)")
            true
        } catch (e: Exception) {
            Bus.log("[엣지] 오버레이 부착 실패($edgeLabel, display=$displayId): ${e.javaClass.simpleName}: ${e.message}")
            false
        }
    }

    private fun cancelPending() {
        pendingRunnable?.let { main.removeCallbacks(it) }
        pendingRunnable = null
    }
}
