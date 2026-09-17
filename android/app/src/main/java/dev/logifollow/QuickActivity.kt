package dev.logifollow

import android.app.Activity
import android.os.Bundle
import android.widget.Toast

/**
 * 화면 없는 바로가기 진입점. 덱스 작업표시줄이나 런처 롱프레스에서
 * "마우스 → 채널N", "지금 맞추기" 를 한 번에 실행한다.
 */
class QuickActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val cmd = intent?.getStringExtra("cmd")
        when (cmd) {
            "sync" -> {
                FollowService.command(this, FollowService.ACTION_SYNC)
                toast("마우스를 키보드 채널로 맞춥니다")
            }
            "switch" -> {
                val host = hostExtra()
                if (host >= 0) {
                    FollowService.command(this, FollowService.ACTION_SWITCH, host)
                    toast("마우스 → 채널${host + 1}")
                } else {
                    toast("채널을 알 수 없습니다")
                }
            }
            "stop" -> {
                FollowService.command(this, FollowService.ACTION_STOP)
                toast("정지")
            }
            else -> toast("알 수 없는 명령")
        }
        finish()
    }

    /** shortcuts.xml 의 extra 는 문자열로 올 수도, 정수로 올 수도 있다. */
    private fun hostExtra(): Int {
        val i = intent.getIntExtra("host", -1)
        if (i >= 0) return i
        return intent.getStringExtra("host")?.toIntOrNull() ?: -1
    }

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()
}
