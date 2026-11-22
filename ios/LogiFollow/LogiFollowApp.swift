import SwiftUI

@main
struct LogiFollowApp: App {
    @StateObject private var manager = BleFollowManager()

    var body: some Scene {
        WindowGroup {
            ContentView(manager: manager)
        }
    }
}
