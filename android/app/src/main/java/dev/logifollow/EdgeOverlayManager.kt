package dev.logifollow

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
        val displays = dm.displays.filter { it.state != Display.STATE_OFF }
        val targets = if (displays.isEmpty()) listOf(dm.getDisplay(Display.DEFAULT_DISPLAY)) else displays

        var count = 0
        for (disp in targets) {
            if (disp == null) continue
            val dCtx = try {
                val base = ctx.createDisplayContext(disp)
                if (Build.VERSION.SDK_INT >= 30) {
                    base.createWindowContext(WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY, null)
                } else {
                    base
                }
            } catch (e: Exception) {
                Bus.log("[엣지] 디스플레이(${disp.displayId}) 컨텍스트 생성 실패: ${e.message}")
                continue
            }
            val wm = dCtx.getSystemService(Context.WINDOW_SERVICE) as? WindowManager ?: continue
            if (cfg.leftHost >= 0) {
                if (attachEdge(dCtx, wm, disp.displayId, Gravity.START, false, "왼쪽", cfg.leftHost, cfg.dwellMs, cfg.cooldownMs)) count++
            }
            if (cfg.rightHost >= 0) {
                if (attachEdge(dCtx, wm, disp.displayId, Gravity.END, false, "오른쪽", cfg.rightHost, cfg.dwellMs, cfg.cooldownMs)) count++
            }
            if (cfg.topHost >= 0) {
                if (attachEdge(dCtx, wm, disp.displayId, Gravity.TOP, true, "위쪽", cfg.topHost, cfg.dwellMs, cfg.cooldownMs)) count++
            }
            if (cfg.bottomHost >= 0) {
                if (attachEdge(dCtx, wm, disp.displayId, Gravity.BOTTOM, true, "아래쪽", cfg.bottomHost, cfg.dwellMs, cfg.cooldownMs)) count++
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
