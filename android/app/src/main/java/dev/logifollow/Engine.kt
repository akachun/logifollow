package dev.logifollow

/** USB 리시버 엔진과 블루투스(BLE++) 엔진의 공통 제어 인터페이스. */
interface Engine : Runnable {
    var dryRun: Boolean
    var verbose: Boolean
    fun post(c: EngineCmd): Boolean
    fun stop()
}

sealed class EngineCmd {
    object Rescan : EngineCmd()
    object Sync : EngineCmd()
    object Stop : EngineCmd()
    data class Switch(val host: Int) : EngineCmd()
    /** 화면 경계 전환: 마우스와 키보드를 함께 지정 채널(0-based)로 전환한다. */
    data class SwitchPair(val host: Int, val why: String = "엣지") : EngineCmd()
}
