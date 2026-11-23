package dev.logifollow

import android.content.Context
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.TypedValue
import android.view.Display
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager

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

    private fun rebuild() {
        clearAttached()
        val cfg = EdgePrefs.load(ctx)
        if (!cfg.enabled) return
        if (cfg.leftHost < 0 && cfg.rightHost < 0) return
        if (!Settings.canDrawOverlays(ctx)) {
            Bus.log("[엣지] '다른 앱 위에 표시' 권한이 없어 화면 경계 감지를 켤 수 없습니다.")
            return
        }

        val dm = ctx.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager
        val displays = dm.displays.filter { it.state != Display.STATE_OFF }
        val targets = if (displays.isEmpty()) listOf(null) else displays

        var count = 0
        for (disp in targets) {
            val dCtx = if (disp != null && disp.displayId != Display.DEFAULT_DISPLAY) {
                try { ctx.createDisplayContext(disp) } catch (_: Exception) { continue }
            } else {
                ctx
            }
            val wm = dCtx.getSystemService(Context.WINDOW_SERVICE) as? WindowManager ?: continue
            if (cfg.leftHost >= 0) {
                if (attachEdge(dCtx, wm, Gravity.START, "왼쪽", cfg.leftHost, cfg.dwellMs, cfg.cooldownMs)) count++
            }
            if (cfg.rightHost >= 0) {
                if (attachEdge(dCtx, wm, Gravity.END, "오른쪽", cfg.rightHost, cfg.dwellMs, cfg.cooldownMs)) count++
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
        gravity: Int,
        edgeLabel: String,
        targetHost: Int,
        dwellMs: Long,
        cooldownMs: Long
    ): Boolean {
        val metrics = dCtx.resources.displayMetrics
        val deadzone = dp(dCtx, 60f)
        val stripWidth = maxOf(dp(dCtx, 3f), 3)
        val stripHeight = maxOf(metrics.heightPixels - deadzone * 2, dp(dCtx, 120f))

        val lp = WindowManager.LayoutParams(
            stripWidth,
            stripHeight,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT
        ).apply {
            this.gravity = gravity or Gravity.CENTER_VERTICAL
        }

        val view = View(dCtx)
        view.setOnHoverListener { _, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_HOVER_ENTER, MotionEvent.ACTION_HOVER_MOVE -> {
                    if (event.buttonState != 0) {
                        cancelPending()
                        return@setOnHoverListener false
                    }
                    val now = System.currentTimeMillis()
                    if (now < cooldownUntil) return@setOnHoverListener false
                    if (pendingRunnable == null) {
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

        return try {
            wm.addView(view, lp)
            attached.add(wm to view)
            true
        } catch (e: Exception) {
            Bus.log("[엣지] 오버레이 부착 실패($edgeLabel): ${e.message}")
            false
        }
    }

    private fun cancelPending() {
        pendingRunnable?.let { main.removeCallbacks(it) }
        pendingRunnable = null
    }
}
