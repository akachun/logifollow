package dev.logifollow

import android.content.Context

/** 화면 경계선(Edge Switch) 설정 저장소. */
object EdgePrefs {
    private const val PREF = "edge_prefs"
    private const val KEY_ENABLED = "enabled"
    private const val KEY_LEFT = "left_host"     // -1 = 안 함, 0..2 = 채널 1..3
    private const val KEY_RIGHT = "right_host"
    private const val KEY_TOP = "top_host"
    private const val KEY_BOTTOM = "bottom_host"
    private const val KEY_DWELL = "dwell_ms"

    data class Config(
        val enabled: Boolean = false,
        val leftHost: Int = -1,
        val rightHost: Int = -1,
        val topHost: Int = -1,
        val bottomHost: Int = -1,
        val dwellMs: Long = 300L,
        val cooldownMs: Long = 1500L
    )

    fun load(ctx: Context): Config {
        val p = ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)
        return Config(
            enabled = p.getBoolean(KEY_ENABLED, false),
            leftHost = p.getInt(KEY_LEFT, -1),
            rightHost = p.getInt(KEY_RIGHT, -1),
            topHost = p.getInt(KEY_TOP, -1),
            bottomHost = p.getInt(KEY_BOTTOM, -1),
            dwellMs = p.getLong(KEY_DWELL, 300L)
        )
    }

    fun save(ctx: Context, cfg: Config) {
        ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit()
            .putBoolean(KEY_ENABLED, cfg.enabled)
            .putInt(KEY_LEFT, cfg.leftHost)
            .putInt(KEY_RIGHT, cfg.rightHost)
            .putInt(KEY_TOP, cfg.topHost)
            .putInt(KEY_BOTTOM, cfg.bottomHost)
            .putLong(KEY_DWELL, cfg.dwellMs)
            .apply()
    }
}
