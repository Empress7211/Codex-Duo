import AppKit
import Darwin
import Foundation

private struct InstanceMarker: Decodable {
    let profile_id: String
    let python: String
    let backend: String
    let source_home: String
    let engine_executable: String
}

private func fail(_ message: String) -> Never {
    NSApplication.shared.setActivationPolicy(.accessory)
    let alert = NSAlert()
    alert.messageText = "无法打开 Codex 实例"
    alert.informativeText = message
    alert.addButton(withTitle: "好")
    alert.runModal()
    exit(1)
}

guard let resolvedExecutable = realpath(CommandLine.arguments[0], nil) else {
    fail("无法定位实例启动入口。")
}
let executable = String(cString: resolvedExecutable)
free(resolvedExecutable)
let contents = ((executable as NSString).deletingLastPathComponent as NSString).deletingLastPathComponent
let resources = contents + "/Resources"
let environment = ProcessInfo.processInfo.environment
do {
    let marker = try JSONDecoder().decode(InstanceMarker.self,
        from: Data(contentsOf: URL(fileURLWithPath: resources + "/duo-instance.json")))
    var root = resources
    for _ in 0..<6 { root = (root as NSString).deletingLastPathComponent }
    let profile = root + "/profiles/" + marker.profile_id
    let home = profile + "/codex-home"
    let ui = profile + "/user-data"
    if environment["ORBIT_ROOT"] == root && environment["ORBIT_PROFILE_ID"] == marker.profile_id &&
       environment["CODEX_HOME"] == home && environment["CODEX_ELECTRON_USER_DATA_PATH"] == ui &&
       !(environment["ORBIT_SOURCE_HOME"] ?? "").isEmpty && environment["ORBIT_SOURCE_HOME"] != home {
        // The native browser lock runs before JS. Preserve the official loader,
        // but always supply this instance's directory before entering it.
        let engine = contents + "/MacOS/" + marker.engine_executable
        let arguments = [engine] + CommandLine.arguments.dropFirst().filter {
            !$0.hasPrefix("--user-data-dir=")
        } + ["--user-data-dir=" + ui]
        var pointers = arguments.map { strdup($0) }
        pointers.append(nil)
        execv(engine, &pointers)
        fail("系统未能执行实例客户端（错误码 \(errno)）。")
    }
    let process = Process()
    process.executableURL = URL(fileURLWithPath: marker.python)
    process.arguments = ["-E", "-s", "-B", marker.backend, "--root", root, "--source-home", marker.source_home]
    process.environment = environment.merging(["PYTHONDONTWRITEBYTECODE": "1"]) { _, new in new }
    let input = Pipe(), output = Pipe()
    process.standardInput = input
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    try process.run()
    input.fileHandleForWriting.write(try JSONSerialization.data(withJSONObject:
        ["action": "launch", "profile": marker.profile_id]))
    try input.fileHandleForWriting.close()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    guard let response = try JSONSerialization.jsonObject(with: data) as? [String: Any],
          response["ok"] as? Bool == true else {
        let response = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        fail(response?["error"] as? String ?? "实例启动后端未返回成功结果。")
    }
    exit(0)
} catch {
    fail("实例启动入口不可用：\(error.localizedDescription)")
}
