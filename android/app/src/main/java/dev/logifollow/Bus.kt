package dev.logifollow

import android.os.Handler
import android.os.Looper
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** 엔진(백그라운드 스레드)과 화면 사이의 유일한 통로. */
object Bus {

    private const val MAX_LINES = 600

    data class State(
        val running: Boolean = false,
        val receiver: String? = null,
        val transport: String? = null,
        val keyboard: String? = null,
        val mouse: String? = null,
        val mapping: List<String> = emptyList(),
        val problem: String? = null
    )

    @Volatile
    var state = State()
        private set

    private val lines = ArrayDeque<String>()
    private val listeners = mutableSetOf<() -> Unit>()
    private val main = Handler(Looper.getMainLooper())
    private val stamp = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)

    fun log(s: String) {
        val line = stamp.format(Date()) + "  " + s
        synchronized(lines) {
            for (l in line.split("\n")) {
                if (lines.size >= MAX_LINES) lines.removeFirst()
                lines.addLast(l)
            }
        }
        android.util.Log.i("LogiFollow", s)
        notifyAll0()
    }

    fun snapshot(): String = synchronized(lines) { lines.joinToString("\n") }

    fun clear() {
        synchronized(lines) { lines.clear() }
        notifyAll0()
    }

    fun setState(block: (State) -> State) {
        state = block(state)
        notifyAll0()
    }

    fun addListener(l: () -> Unit) = synchronized(listeners) { listeners.add(l) }
    fun removeListener(l: () -> Unit) = synchronized(listeners) { listeners.remove(l) }

    // 검색 단계에서 로그가 한꺼번에 쏟아진다. 매 줄마다 화면을 다시 그리면
    // 메인 스레드가 놀아나므로 80ms 로 뭉쳐서 한 번만 알린다.
    private var notifyQueued = false
    private val flush = Runnable {
        synchronized(this) { notifyQueued = false }
        synchronized(listeners) { listeners.toList() }.forEach { it() }
    }

    private fun notifyAll0() {
        synchronized(this) {
            if (notifyQueued) return
            notifyQueued = true
        }
        main.postDelayed(flush, 80)
    }
}
