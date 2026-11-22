import SwiftUI

struct ContentView: View {
    @ObservedObject var manager: BleFollowManager

    var body: some View {
        NavigationView {
            VStack(spacing: 12) {
                statusCard
                controlBar
                manualSwitchBar
                logBox
            }
            .padding()
            .navigationTitle("LogiFollow BLE++")
            .navigationBarTitleDisplayMode(.inline)
        }
        .navigationViewStyle(.stack)
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Circle()
                    .fill(manager.isRunning ? Color.green : Color.gray)
                    .frame(width: 10, height: 10)
                Text(manager.isRunning ? "감시 중" : "정지")
                    .font(.headline)
                Spacer()
                Text(manager.btStateText)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            Divider()
            LabeledContent("키보드", value: manager.keyboardSummary)
                .font(.subheadline)
            LabeledContent("마우스", value: manager.mouseSummary)
                .font(.subheadline)

            if !manager.mappingLines.isEmpty {
                Divider()
                ForEach(manager.mappingLines, id: \.self) { line in
                    Text(line)
                        .font(.caption.monospaced())
                        .foregroundColor(.secondary)
                }
            }
        }
        .padding(12)
        .background(Color(.secondarySystemBackground))
        .cornerRadius(12)
    }

    private var controlBar: some View {
        HStack(spacing: 10) {
            Button(manager.isRunning ? "정지" : "시작") {
                if manager.isRunning {
                    manager.stop()
                } else {
                    manager.start()
                }
            }
            .buttonStyle(.borderedProminent)
            .tint(manager.isRunning ? .red : .blue)

            Button("재검색") {
                manager.rescan()
            }
            .buttonStyle(.bordered)
            .disabled(!manager.isRunning)

            Spacer()

            Toggle("시험 모드", isOn: $manager.dryRun)
                .labelsHidden()
            Text("시험 모드")
                .font(.subheadline)
        }
    }

    private var manualSwitchBar: some View {
        HStack(spacing: 8) {
            Text("마우스 수동:")
                .font(.caption)
                .foregroundColor(.secondary)
            ForEach(0..<3, id: \.self) { idx in
                Button("채널 \(idx + 1)") {
                    manager.switchMouse(to: idx)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
            Spacer()
        }
    }

    private var logBox: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("로그")
                    .font(.caption.bold())
                    .foregroundColor(.secondary)
                Spacer()
                Button("복사") {
                    UIPasteboard.general.string = manager.logs.joined(separator: "\n")
                }
                .font(.caption)
                Button("지우기") {
                    manager.clearLogs()
                }
                .font(.caption)
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(manager.logs.enumerated()), id: \.offset) { idx, line in
                            Text(line)
                                .font(.system(size: 11, design: .monospaced))
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id(idx)
                        }
                    }
                    .padding(8)
                }
                .background(Color(.systemBackground))
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(Color(.separator), lineWidth: 1)
                )
                .onChange(of: manager.logs.count) { _ in
                    if let last = manager.logs.indices.last {
                        proxy.scrollTo(last, anchor: .bottom)
                    }
                }
            }
        }
    }
}
