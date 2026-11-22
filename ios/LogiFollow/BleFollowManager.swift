import Foundation
import CoreBluetooth
import Combine

/// 로지텍 BLE++ 커스텀 GATT(00010000-...) 기반 iOS/iPadOS 자동 추종 매니저.
///
/// 표준 BLE HID(0x1812)는 iOS CoreBluetooth 가 숨기지만,
/// 로지텍 벤더 커스텀 GATT 서비스(00010000-0000-1000-8000-011F2000046D)의
/// 특성(00010001-...)은 그대로 접근 가능하며 18바이트 BLE++ 포맷으로
/// HID++ 2.0/4.5 조회, ChangeHost(0x1814) Notify 수신, 마우스 전환이 가능하다.
final class BleFollowManager: NSObject, ObservableObject {

    static let logiServiceUUID = CBUUID(string: "00010000-0000-1000-8000-011F2000046D")
    static let logiCharUUID = CBUUID(string: "00010001-0000-1000-8000-011F2000046D")

    private static let featDeviceName: UInt16 = 0x0005
    private static let featChangeHost: UInt16 = 0x1814
    private static let featHostsInfo: UInt16 = 0x1815
    private static let typeKeyboard: UInt8 = 0
    private static let typeMouse: UInt8 = 3
    private static let swIDs: [UInt8] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 15]

    final class Peer {
        let id: UUID
        var peripheral: CBPeripheral
        var characteristic: CBCharacteristic?
        var connected = false
        var name: String?
        var typeCode: UInt8?
        var changeHostIdx: UInt8 = 0
        var hostsInfoIdx: UInt8 = 0
        var hostCount: Int = 0
        var currentHost: Int?
        var descriptors: [Int: String] = [:]

        init(peripheral: CBPeripheral) {
            self.id = peripheral.identifier
            self.peripheral = peripheral
        }

        var label: String {
            let kind: String
            switch typeCode {
            case 0: kind = "키보드"
            case 3: kind = "마우스"
            default: kind = "?"
            }
            return "\(name ?? peripheral.name ?? "?")(\(kind))"
        }

        func hosts(matching desc: String) -> [Int] {
            descriptors.filter { $0.value == desc }.map { $0.key }.sorted()
        }
    }

    private struct Waiter {
        let featIdx: UInt8
        let funcSw: UInt8
        let echo: UInt8?
        let continuation: CheckedContinuation<Data?, Never>
    }

    @Published var isRunning = false
    @Published var dryRun = false
    @Published var btStateText = "초기화 중"
    @Published var keyboardSummary = "미연결"
    @Published var mouseSummary = "미연결"
    @Published var mappingLines: [String] = []
    @Published var logs: [String] = []

    private let bleQueue = DispatchQueue(label: "dev.logifollow.ios.ble")
    private var central: CBCentralManager?
    private var peers: [UUID: Peer] = [:]
    private var waiters: [UUID: Waiter] = [:]
    private var swIndex = 0
    private var pollTimer: DispatchSourceTimer?

    private static let timeFmt: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()

    override init() {
        super.init()
        btStateText = "대기 중 (시작을 누르세요)"
    }

    private func ensureCentral() -> CBCentralManager {
        if let c = central { return c }
        let c = CBCentralManager(delegate: self, queue: bleQueue, options: nil)
        central = c
        return c
    }

    func start() {
        bleQueue.async { [weak self] in
            guard let self else { return }
            DispatchQueue.main.async { self.isRunning = true }
            self.log("BLE++ 감시를 시작합니다.")
            let c = self.ensureCentral()
            if c.state == .poweredOn {
                self.scanConnected()
            }
            self.startPollTimer()
        }
    }

    func stop() {
        bleQueue.async { [weak self] in
            guard let self else { return }
            self.pollTimer?.cancel()
            self.pollTimer = nil
            if let c = self.central {
                for p in self.peers.values {
                    c.cancelPeripheralConnection(p.peripheral)
                }
            }
            self.peers.removeAll()
            DispatchQueue.main.async {
                self.isRunning = false
                self.updateSummaries()
            }
            self.log("감시를 정지했습니다.")
        }
    }

    func rescan() {
        bleQueue.async { [weak self] in
            guard let self else { return }
            self.log("연결된 로지텍 BLE++ 기기 재검색...")
            self.scanConnected()
        }
    }

    func switchMouse(to host: Int) {
        bleQueue.async { [weak self] in
            guard let self else { return }
            guard let ms = self.peers.values.first(where: { $0.typeCode == Self.typeMouse && $0.connected && $0.changeHostIdx != 0 }) else {
                self.log("연결된 마우스가 없습니다.")
                return
            }
            self.sendSetHost(ms, host: host, reason: "수동")
        }
    }

    func clearLogs() {
        DispatchQueue.main.async { self.logs.removeAll() }
    }

    // MARK: - BLE Discovery & Polling

    private func startPollTimer() {
        pollTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: bleQueue)
        timer.schedule(deadline: .now() + 2.0, repeating: 2.0)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            let kbOk = self.peers.values.contains { $0.typeCode == Self.typeKeyboard && $0.connected }
            let msOk = self.peers.values.contains { $0.typeCode == Self.typeMouse && $0.connected }
            if !kbOk || !msOk {
                self.scanConnected()
            }
        }
        timer.resume()
        pollTimer = timer
    }

    private func scanConnected() {
        guard let central, central.state == .poweredOn else { return }
        let connected = central.retrieveConnectedPeripherals(withServices: [Self.logiServiceUUID])
        if connected.isEmpty && peers.isEmpty {
            log("연결된 로지텍 BLE++ 기기가 아직 없습니다. iOS 설정 > Bluetooth 연결을 확인하세요.")
            return
        }
        for peripheral in connected {
            let id = peripheral.identifier
            if let existing = peers[id], existing.connected, existing.characteristic != nil {
                continue
            }
            let peer = peers[id] ?? Peer(peripheral: peripheral)
            peer.peripheral = peripheral
            peers[id] = peer
            peripheral.delegate = self
            central.connect(peripheral, options: nil)
        }
    }

    // MARK: - HID++ 18B Transaction

    private func nextSw() -> UInt8 {
        swIndex = (swIndex + 1) % Self.swIDs.count
        return Self.swIDs[swIndex]
    }

    private func transact(peer: Peer, featIdx: UInt8, funcId: UInt8, params: [UInt8], echo: UInt8? = nil) async -> Data? {
        guard let chr = peer.characteristic, peer.connected else { return nil }
        let sw = nextSw()
        let fs: UInt8 = ((funcId & 0x0F) << 4) | (sw & 0x0F)
        var bytes = [UInt8](repeating: 0, count: 18)
        bytes[0] = featIdx
        bytes[1] = fs
        for (i, b) in params.enumerated() where 2 + i < 18 {
            bytes[2 + i] = b
        }
        let data = Data(bytes)

        return await withCheckedContinuation { cont in
            bleQueue.async { [weak self] in
                guard let self else {
                    cont.resume(returning: nil)
                    return
                }
                self.waiters[peer.id] = Waiter(featIdx: featIdx, funcSw: fs, echo: echo, continuation: cont)
                let writeType: CBCharacteristicWriteType = chr.properties.contains(.write) ? .withResponse : .withoutResponse
                peer.peripheral.writeValue(data, for: chr, type: writeType)

                self.bleQueue.asyncAfter(deadline: .now() + 1.2) { [weak self] in
                    guard let self, let w = self.waiters[peer.id], w.funcSw == fs, w.featIdx == featIdx else { return }
                    self.waiters.removeValue(forKey: peer.id)
                    w.continuation.resume(returning: nil)
                }
            }
        }
    }

    private func loadPeer(_ peer: Peer) {
        Task { [weak self] in
            guard let self else { return }
            guard let ping = await self.transact(peer: peer, featIdx: 0x00, funcId: 0x01, params: [0, 0, 0xAA]),
                  ping.count >= 4 else {
                self.log("Ping 응답 없음: \(peer.peripheral.name ?? "?")")
                return
            }

            if let rNi = await self.transact(peer: peer, featIdx: 0x00, funcId: 0x00, params: [0x00, 0x05, 0]),
               rNi.count >= 3, rNi[2] != 0 {
                let ni = rNi[2]
                if let c = await self.transact(peer: peer, featIdx: ni, funcId: 0x00, params: [0, 0, 0]),
                   c.count >= 3 {
                    let count = Int(c[2])
                    var nameBytes: [UInt8] = []
                    var pos = 0
                    while pos < count && pos < 64 {
                        guard let nr = await self.transact(peer: peer, featIdx: ni, funcId: 0x01, params: [UInt8(pos), 0, 0]),
                              nr.count >= 18 else { break }
                        let take = min(16, count - pos)
                        nameBytes.append(contentsOf: nr[2..<(2 + take)])
                        pos += 16
                    }
                    peer.name = String(bytes: nameBytes, encoding: .ascii)?.trimmingCharacters(in: .whitespacesAndNewlines)
                }
                if let t = await self.transact(peer: peer, featIdx: ni, funcId: 0x02, params: [0, 0, 0]),
                   t.count >= 3 {
                    peer.typeCode = t[2]
                }
            }

            if let rCh = await self.transact(peer: peer, featIdx: 0x00, funcId: 0x00, params: [0x18, 0x14, 0]),
               rCh.count >= 3 {
                peer.changeHostIdx = rCh[2]
            }
            if let rHi = await self.transact(peer: peer, featIdx: 0x00, funcId: 0x00, params: [0x18, 0x15, 0]),
               rHi.count >= 3 {
                peer.hostsInfoIdx = rHi[2]
            }

            if peer.changeHostIdx != 0,
               let ri = await self.transact(peer: peer, featIdx: peer.changeHostIdx, funcId: 0x00, params: [0, 0, 0]),
               ri.count >= 4 {
                peer.hostCount = Int(ri[2])
                peer.currentHost = Int(ri[3])
            }

            if peer.hostsInfoIdx != 0 && peer.hostCount > 0 {
                for h in 0..<peer.hostCount {
                    if let rd = await self.transact(peer: peer, featIdx: peer.hostsInfoIdx, funcId: 0x02, params: [UInt8(h), 0, 0], echo: UInt8(h)),
                       rd.count >= 11 {
                        var slice = Array(rd[3..<11])
                        while slice.last == 0 { slice.removeLast() }
                        peer.descriptors[h] = slice.map { String(format: "%02X", $0) }.joined(separator: " ")
                    }
                }
            }

            self.log("[연결] \(peer.label) HID++ \(ping[2]).\(ping[3]) 현재 \(self.chan(peer.currentHost))")
            DispatchQueue.main.async { self.updateSummaries() }
        }
    }

    private func sendSetHost(_ ms: Peer, host: Int, reason: String) {
        if dryRun {
            log("  [시험] \(reason): 마우스 -> \(chan(host)) (전송 안 함)")
            return
        }
        guard let chr = ms.characteristic, ms.connected else {
            log("  전송 실패: 마우스 미연결")
            return
        }
        let sw = nextSw()
        var bytes = [UInt8](repeating: 0, count: 18)
        bytes[0] = ms.changeHostIdx
        bytes[1] = (0x01 << 4) | (sw & 0x0F)
        bytes[2] = UInt8(host)
        let writeType: CBCharacteristicWriteType = chr.properties.contains(.writeWithoutResponse) ? .withoutResponse : .withResponse
        ms.peripheral.writeValue(Data(bytes), for: chr, type: writeType)
        ms.currentHost = host
        log("  => \(reason): 마우스 -> \(chan(host))")
        DispatchQueue.main.async { self.updateSummaries() }
    }

    private func handleEvent(peer: Peer, data: Data) {
        guard data.count >= 4 else { return }
        let fidx = data[0]
        let b1 = data[1]
        guard (b1 & 0x0F) == 0x00, fidx < 0x80 else { return }

        if peer.typeCode == Self.typeMouse && fidx == peer.changeHostIdx {
            let src = Int(data[2]), dst = Int(data[3])
            peer.currentHost = dst
            log("[이벤트] 마우스가 스스로 \(chan(src)) -> \(chan(dst))")
            DispatchQueue.main.async { self.updateSummaries() }
            return
        }

        if peer.typeCode == Self.typeKeyboard && fidx == peer.changeHostIdx {
            let src = Int(data[2]), dst = Int(data[3])
            peer.currentHost = dst
            guard let ms = peers.values.first(where: { $0.typeCode == Self.typeMouse && $0.connected && $0.changeHostIdx != 0 }) else {
                log("[이벤트] 키보드 \(chan(src)) -> \(chan(dst)) (연결된 마우스 없음)")
                return
            }
            let targetHost: Int?
            if let desc = peer.descriptors[dst] {
                targetHost = ms.hosts(matching: desc).first
            } else {
                targetHost = dst < ms.hostCount ? dst : nil
            }
            log("[이벤트] 키보드 \(chan(src)) -> \(chan(dst))")
            guard let target = targetHost else {
                log("  -> 대응하는 마우스 채널이 없습니다")
                return
            }
            if ms.currentHost == target {
                log("  -> 마우스는 이미 \(chan(target)) 입니다")
                return
            }
            sendSetHost(ms, host: target, reason: "따라감")
        }
    }

    // MARK: - Helpers

    private func chan(_ host: Int?) -> String {
        guard let h = host else { return "?" }
        return "채널\(h + 1)"
    }

    private func updateSummaries() {
        let kb = peers.values.first { $0.typeCode == Self.typeKeyboard && $0.connected }
        let ms = peers.values.first { $0.typeCode == Self.typeMouse && $0.connected }
        keyboardSummary = kb.map { "\($0.label)  현재 \(chan($0.currentHost))" } ?? "미연결"
        mouseSummary = ms.map { "\($0.label)  현재 \(chan($0.currentHost))" } ?? "미연결"

        if let kb, let ms {
            mappingLines = kb.descriptors.keys.sorted().map { kh in
                let target = kb.descriptors[kh] ?? ""
                let matches = ms.hosts(matching: target)
                if matches.isEmpty {
                    return "키보드 채널\(kh + 1)  ->  대응 없음"
                } else {
                    let dst = matches.map { "채널\($0 + 1)" }.joined(separator: ", ")
                    return "키보드 채널\(kh + 1)  ->  마우스 \(dst)"
                }
            }
        } else {
            mappingLines = []
        }
    }

    private func log(_ msg: String) {
        let line = "\(Self.timeFmt.string(from: Date()))  \(msg)"
        DispatchQueue.main.async {
            self.logs.append(line)
            if self.logs.count > 300 {
                self.logs.removeFirst(self.logs.count - 300)
            }
        }
    }
}

// MARK: - CBCentralManagerDelegate & CBPeripheralDelegate

extension BleFollowManager: CBCentralManagerDelegate, CBPeripheralDelegate {

    func centralManager(_ central: CBCentralManager, willRestoreState dict: [String: Any]) {
        if let restored = dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral] {
            for p in restored {
                let peer = Peer(peripheral: p)
                peer.connected = (p.state == .connected)
                peers[p.identifier] = peer
                p.delegate = self
            }
            log("백그라운드 상태 복원: 기기 \(restored.count)개")
        }
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        let text: String
        switch central.state {
        case .poweredOn: text = "켜짐 (PoweredOn)"
        case .poweredOff: text = "꺼짐 (블루투스를 켜주세요)"
        case .unauthorized: text = "권한 없음 (설정에서 블루투스 허용 필요)"
        case .unsupported: text = "BLE 미지원"
        default: text = "대기 중"
        }
        DispatchQueue.main.async { self.btStateText = text }
        if central.state == .poweredOn && isRunning {
            scanConnected()
        }
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        guard let peer = peers[peripheral.identifier] else { return }
        peer.connected = true
        peripheral.discoverServices([Self.logiServiceUUID])
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        guard let peer = peers[peripheral.identifier] else { return }
        peer.connected = false
        if let w = waiters.removeValue(forKey: peer.id) {
            w.continuation.resume(returning: nil)
        }
        log("[연결 끊김] \(peer.label)")
        DispatchQueue.main.async { self.updateSummaries() }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard error == nil, let svcs = peripheral.services else { return }
        for s in svcs where s.uuid == Self.logiServiceUUID {
            peripheral.discoverCharacteristics([Self.logiCharUUID], for: s)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        guard error == nil, let peer = peers[peripheral.identifier], let chars = service.characteristics else { return }
        for c in chars where c.uuid == Self.logiCharUUID {
            peer.characteristic = c
            peripheral.setNotifyValue(true, for: c)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil, let peer = peers[peripheral.identifier], characteristic.isNotifying else { return }
        loadPeer(peer)
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil, let data = characteristic.value, let peer = peers[peripheral.identifier] else { return }

        if let w = waiters[peer.id], data.count >= 2 {
            if data[0] == 0x8F || data[0] == 0xFF {
                waiters.removeValue(forKey: peer.id)
                w.continuation.resume(returning: nil)
                return
            }
            if data[0] == w.featIdx && data[1] == w.funcSw {
                if w.echo == nil || (data.count >= 3 && data[2] == w.echo) {
                    waiters.removeValue(forKey: peer.id)
                    w.continuation.resume(returning: data)
                    return
                }
            }
        }

        handleEvent(peer: peer, data: data)
    }
}
