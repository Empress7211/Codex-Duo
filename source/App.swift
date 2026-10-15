import AppKit
import Darwin
import Foundation

private final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

private final class ActionButton: NSButton {
    var handler: (() -> Void)?
    init(_ title: String, action: @escaping () -> Void) {
        super.init(frame: .zero)
        self.title = title
        self.handler = action
        bezelStyle = .rounded
        target = self
        self.action = #selector(invoke)
    }
    required init?(coder: NSCoder) { fatalError("Not supported") }
    @objc private func invoke() { handler?() }
}

private enum Page: String, CaseIterable {
    case instances = "实例", activity = "实时会话", settings = "设置"
    var symbol: String {
        switch self { case .instances: return "square.stack.3d.up"; case .activity: return "waveform.path"; case .settings: return "slider.horizontal.3" }
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private let content = NSStackView()
    private let scroll = NSScrollView()
    private let activityList = NSStackView()
    private let activitySummary = NSTextField(labelWithString: "")
    private let activityTimestamp = NSTextField(labelWithString: "等待状态")
    private let activityNotice = NSTextField(wrappingLabelWithString: "")
    private let status = NSTextField(wrappingLabelWithString: "正在读取本地状态…")
    private let progress = NSProgressIndicator()
    private var navButtons: [Page: NSButton] = [:]
    private var controls: [NSControl] = []
    private var state: [String: Any] = [:]
    private var providers: [[String: Any]] = []
    private var page = Page.instances
    private var busy = false
    private var activityLoading = false
    private var activitySignature: Data?
    private var showUnnamedActivity = false
    private var activityTimer: Timer?
    private var instanceTimer: Timer?
    private var instanceLoading = false
    private var pythonPath: String?
    private var initialized: Bool { state["initialized"] as? Bool ?? false }
    private var profiles: [[String: Any]] { state["profiles"] as? [[String: Any]] ?? [] }
    private var backend: URL {
        let bundled = Bundle.main.resourceURL?.appendingPathComponent("source/backend.py")
        if let bundled, FileManager.default.fileExists(atPath: bundled.path) { return bundled }
        return URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL.deletingLastPathComponent().appendingPathComponent("backend.py")
    }
    private var root: String {
        if let value = ProcessInfo.processInfo.environment["CODEX_DUAL_ROOT"], !value.isEmpty { return (value as NSString).expandingTildeInPath }
        if let location = Bundle.main.resourceURL?.appendingPathComponent("runtime-location.json"),
           let data = try? Data(contentsOf: location),
           let record = try? JSONSerialization.jsonObject(with: data) as? [String: String],
           let value = record["root"], !value.isEmpty { return value }
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return support.appendingPathComponent("Codex Duo/runtime").path
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.appearance = NSAppearance(named: .aqua)
        if let iconURL = Bundle.main.url(forResource: "Orbit", withExtension: "png"),
           let appIcon = NSImage(contentsOf: iconURL) {
            NSApp.applicationIconImage = appIcon
        }
        setupMenu()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1080, height: 760), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Codex Duo"; window.titlebarAppearsTransparent = true
        window.minSize = NSSize(width: 900, height: 620)
        window.isReleasedWhenClosed = false; window.delegate = self; window.center()
        let canvas = NSView(); canvas.wantsLayer = true; canvas.layer?.backgroundColor = NSColor.white.cgColor
        window.contentView = canvas
        let sidebar = NSView(); sidebar.wantsLayer = true
        sidebar.layer?.backgroundColor = NSColor(calibratedRed: 0.954, green: 0.960, blue: 0.969, alpha: 1).cgColor
        sidebar.translatesAutoresizingMaskIntoConstraints = false; canvas.addSubview(sidebar)
        let navigation = vertical(spacing: 12); navigation.translatesAutoresizingMaskIntoConstraints = false
        sidebar.addSubview(navigation)
        let brand = horizontal()
        let icon = NSImageView(); icon.image = NSApp.applicationIconImage; icon.imageScaling = .scaleProportionallyUpOrDown
        icon.widthAnchor.constraint(equalToConstant: 40).isActive = true; icon.heightAnchor.constraint(equalToConstant: 40).isActive = true
        brand.addArrangedSubview(icon); brand.addArrangedSubview(label("Codex Duo", size: 17, weight: .semibold))
        navigation.addArrangedSubview(brand)
        let tagline = label("一个工作区，多个 Codex 窗口", size: 11, secondary: true)
        navigation.addArrangedSubview(tagline); navigation.setCustomSpacing(24, after: tagline)
        for item in Page.allCases {
            let button = ActionButton(item.rawValue) { [weak self] in self?.showPage(item) }
            button.image = NSImage(systemSymbolName: item.symbol, accessibilityDescription: item.rawValue)
            button.imagePosition = .imageLeading; button.alignment = .left; button.isBordered = false
            button.font = .systemFont(ofSize: 13, weight: .medium)
            button.wantsLayer = true; button.layer?.cornerRadius = 8
            button.heightAnchor.constraint(equalToConstant: 42).isActive = true
            navigation.addArrangedSubview(button); button.widthAnchor.constraint(equalTo: navigation.widthAnchor).isActive = true
            navButtons[item] = button
        }
        let sideFooter = label("独立登录 · 本地管理\n原有配置保持只读", size: 11, secondary: true)
        sideFooter.translatesAutoresizingMaskIntoConstraints = false; sidebar.addSubview(sideFooter)
        scroll.translatesAutoresizingMaskIntoConstraints = false; scroll.hasVerticalScroller = true; scroll.drawsBackground = false
        canvas.addSubview(scroll)
        let document = FlippedView(); document.translatesAutoresizingMaskIntoConstraints = false; scroll.documentView = document
        content.orientation = .vertical; content.alignment = .leading; content.spacing = 18
        content.distribution = .fill
        content.setContentHuggingPriority(.required, for: .vertical); content.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(content)
        let fit = document.heightAnchor.constraint(equalTo: content.heightAnchor, constant: 64); fit.priority = NSLayoutConstraint.Priority(999)
        let preferredWidth = content.widthAnchor.constraint(equalTo: document.widthAnchor, constant: -64)
        preferredWidth.priority = .defaultHigh
        let statusBar = horizontal(spacing: 8); statusBar.translatesAutoresizingMaskIntoConstraints = false
        progress.style = .spinning; progress.controlSize = .small; progress.isDisplayedWhenStopped = false
        status.font = .systemFont(ofSize: 11); status.textColor = .secondaryLabelColor
        statusBar.addArrangedSubview(progress); statusBar.addArrangedSubview(status); canvas.addSubview(statusBar)
        NSLayoutConstraint.activate([
            sidebar.leadingAnchor.constraint(equalTo: canvas.leadingAnchor), sidebar.topAnchor.constraint(equalTo: canvas.topAnchor), sidebar.bottomAnchor.constraint(equalTo: canvas.bottomAnchor), sidebar.widthAnchor.constraint(equalToConstant: 230),
            navigation.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 20), navigation.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -16), navigation.topAnchor.constraint(equalTo: sidebar.topAnchor, constant: 30),
            sideFooter.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 20), sideFooter.bottomAnchor.constraint(equalTo: sidebar.bottomAnchor, constant: -24),
            scroll.leadingAnchor.constraint(equalTo: sidebar.trailingAnchor), scroll.trailingAnchor.constraint(equalTo: canvas.trailingAnchor), scroll.topAnchor.constraint(equalTo: canvas.topAnchor), scroll.bottomAnchor.constraint(equalTo: statusBar.topAnchor, constant: -8),
            statusBar.leadingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: 32), statusBar.trailingAnchor.constraint(equalTo: canvas.trailingAnchor, constant: -24), statusBar.bottomAnchor.constraint(equalTo: canvas.bottomAnchor, constant: -15),
            document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor), document.heightAnchor.constraint(greaterThanOrEqualTo: scroll.contentView.heightAnchor), fit,
            content.leadingAnchor.constraint(greaterThanOrEqualTo: document.leadingAnchor, constant: 32),
            content.trailingAnchor.constraint(lessThanOrEqualTo: document.trailingAnchor, constant: -32),
            content.centerXAnchor.constraint(equalTo: document.centerXAnchor),
            content.widthAnchor.constraint(lessThanOrEqualToConstant: 1060), preferredWidth,
            content.topAnchor.constraint(equalTo: document.topAnchor, constant: 30), content.bottomAnchor.constraint(lessThanOrEqualTo: document.bottomAnchor, constant: -34)
        ])
        activityList.orientation = .vertical; activityList.alignment = .leading; activityList.spacing = 10
        activityList.setContentHuggingPriority(.required, for: .vertical)
        activitySummary.font = .systemFont(ofSize: 13, weight: .medium)
        activitySummary.textColor = .secondaryLabelColor
        activityTimestamp.font = .systemFont(ofSize: 11); activityTimestamp.textColor = .secondaryLabelColor
        activityNotice.font = .systemFont(ofSize: 12); activityNotice.textColor = .secondaryLabelColor
        showPage(.instances); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); refresh()
        activityTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self, self.page == .activity, self.window.isVisible, !self.window.isMiniaturized, self.window.occlusionState.contains(.visible) else { return }
            self.pollActivity()
        }
        instanceTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            guard let self, self.page == .instances, self.window.isVisible, !self.window.isMiniaturized, self.window.occlusionState.contains(.visible) else { return }
            self.pollInstances()
        }
    }

    private func setupMenu() {
        let main = NSMenu(); let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu(); appMenu.addItem(withTitle: "退出 Codex Duo", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"); appItem.submenu = appMenu
        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: ""); let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit; main.addItem(editItem); NSApp.mainMenu = main
    }
    private func label(_ text: String, size: CGFloat = 13, weight: NSFont.Weight = .regular, secondary: Bool = false) -> NSTextField {
        let view = NSTextField(wrappingLabelWithString: text); view.font = .systemFont(ofSize: size, weight: weight)
        view.textColor = secondary ? .secondaryLabelColor : .labelColor
        view.setContentHuggingPriority(.required, for: .vertical)
        return view
    }
    private func vertical(spacing: CGFloat = 10) -> NSStackView {
        let view = NSStackView(); view.orientation = .vertical; view.alignment = .leading; view.spacing = spacing; return view
    }
    private func horizontal(spacing: CGFloat = 10) -> NSStackView {
        let view = NSStackView(); view.orientation = .horizontal; view.alignment = .centerY; view.spacing = spacing; return view
    }
    private func add(_ view: NSView, fullWidth: Bool = true) {
        content.addArrangedSubview(view); if fullWidth { view.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true }
    }
    private func button(_ title: String, enabled: Bool = true, action: @escaping () -> Void) -> NSButton {
        let control = ActionButton(title, action: action); control.isEnabled = enabled && !busy; controls.append(control); return control
    }
    private func card() -> NSStackView {
        let view = vertical(spacing: 12); view.wantsLayer = true
        view.distribution = .fill
        view.setContentHuggingPriority(.required, for: .vertical)
        view.setContentCompressionResistancePriority(.required, for: .vertical)
        view.layer?.backgroundColor = NSColor(calibratedRed: 0.990, green: 0.992, blue: 0.996, alpha: 1).cgColor
        view.layer?.borderColor = NSColor(calibratedWhite: 0.895, alpha: 1).cgColor; view.layer?.borderWidth = 1; view.layer?.cornerRadius = 14
        view.edgeInsets = NSEdgeInsets(top: 16, left: 18, bottom: 16, right: 18); return view
    }
    private func pill(_ title: String, color: NSColor, fill: NSColor) -> NSView {
        let box = NSView(); box.wantsLayer = true
        box.layer?.backgroundColor = fill.cgColor; box.layer?.cornerRadius = 9
        let text = label(title, size: 11, weight: .medium); text.textColor = color
        text.translatesAutoresizingMaskIntoConstraints = false; box.addSubview(text)
        NSLayoutConstraint.activate([
            text.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: 9),
            text.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -9),
            text.topAnchor.constraint(equalTo: box.topAnchor, constant: 5),
            text.bottomAnchor.constraint(equalTo: box.bottomAnchor, constant: -5)
        ])
        return box
    }
    private func clear(_ stack: NSStackView) {
        for view in stack.arrangedSubviews { stack.removeArrangedSubview(view); view.removeFromSuperview() }
    }
    private func showPage(_ selected: Page) {
        page = selected
        for (key, button) in navButtons {
            button.layer?.backgroundColor = (key == page ? NSColor(calibratedRed: 0.855, green: 0.900, blue: 0.972, alpha: 1) : .clear).cgColor
            button.contentTintColor = key == page ? NSColor(calibratedRed: 0.13, green: 0.31, blue: 0.60, alpha: 1) : .secondaryLabelColor
        }
        renderPage(); scroll.contentView.scroll(to: .zero); if page == .activity { pollActivity() }
    }
    private func renderPage() {
        clear(content); controls.removeAll()
        let heading = horizontal(); heading.addArrangedSubview(label(page.rawValue, size: 27, weight: .semibold))
        let spacer = NSView(); spacer.setContentHuggingPriority(.defaultLow, for: .horizontal); heading.addArrangedSubview(spacer)
        heading.addArrangedSubview(button("刷新") { [weak self] in self?.refresh() })
        if page == .instances { heading.addArrangedSubview(button("＋ 新增实例", enabled: initialized) { [weak self] in self?.createProfile() }) }
        add(heading)
        let description: String
        switch page {
        case .instances: description = "添加不同登录方式的 Codex 窗口，共用原生 Codex 的项目与本地会话。"
        case .activity: description = "分别查看任务执行状态和会话写入权。两者可以不同。"
        case .settings: description = "管理同步、供应商和本地存储位置。"
        }
        add(label(description, size: 12, secondary: true))
        content.setCustomSpacing(22, after: content.arrangedSubviews.last!)
        switch page {
        case .instances: renderInstances()
        case .activity:
            add(activitySummary); add(activityTimestamp); add(activityList); add(activityNotice)
        case .settings: renderSettings()
        }
    }
    private func renderInstances() {
        let running = profiles.filter { $0["running"] as? Bool == true }.count; let history = state["history_count"] as? Int ?? 0
        add(label(initialized ? "\(profiles.count) 个额外窗口  ·  \(running) 个已打开  ·  \(history) 条共享本地会话" : "先连接原生 Codex 工作区，再选择要添加的登录方式。", size: 13, weight: .medium, secondary: true))
        add(label("这里只显示 Duo 创建的额外窗口；原生 Codex 的订阅登录不在此列表中。", size: 12, secondary: true))
        if !initialized { add(button("连接共享工作区") { [weak self] in self?.runAction(["action": "initialize_shared"], message: "正在连接原生 Codex 工作区…") }, fullWidth: false) }
        if initialized && profiles.isEmpty {
            let empty = card()
            empty.addArrangedSubview(label("还没有额外窗口", size: 16, weight: .semibold))
            empty.addArrangedSubview(label("按需要创建 API 或订阅实例；每个实例保存自己的登录，项目和会话仍在同一工作区。", size: 12, secondary: true))
            empty.addArrangedSubview(button("＋ 新增实例") { [weak self] in self?.createProfile() })
            add(empty)
        }
        for profile in profiles { add(profileCard(profile)) }
        add(label("关闭 Duo 管理窗口不会关闭已打开的 Codex 窗口。", size: 11, secondary: true))
    }
    private func profileCard(_ profile: [String: Any]) -> NSStackView {
        let id = profile["id"] as? String ?? "", name = profile["name"] as? String ?? "Codex"
        let kind = profile["kind"] as? String ?? (id == "subscription" ? "subscription" : "api")
        let running = profile["running"] as? Bool ?? false, configured = profile["configured"] as? Bool ?? false
        let view = card(), header = horizontal(spacing: 10)
        let symbol = NSImageView(image: NSImage(systemSymbolName: kind == "api" ? "key.horizontal" : "person.crop.circle", accessibilityDescription: kind) ?? NSImage())
        symbol.contentTintColor = NSColor(calibratedRed: 0.27, green: 0.39, blue: 0.57, alpha: 1)
        symbol.widthAnchor.constraint(equalToConstant: 26).isActive = true
        symbol.heightAnchor.constraint(equalToConstant: 26).isActive = true
        let title = label(name, size: 16, weight: .semibold)
        title.maximumNumberOfLines = 1
        title.lineBreakMode = .byTruncatingTail
        title.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        header.addArrangedSubview(symbol); header.addArrangedSubview(title)
        header.addArrangedSubview(pill(kind == "api" ? "API" : "订阅", color: NSColor(calibratedRed: 0.31, green: 0.39, blue: 0.52, alpha: 1), fill: NSColor(calibratedRed: 0.91, green: 0.93, blue: 0.96, alpha: 1)))
        let spacer = NSView(); spacer.setContentHuggingPriority(.defaultLow, for: .horizontal); header.addArrangedSubview(spacer)
        header.addArrangedSubview(button(running ? "显示窗口" : "启动", enabled: initialized && (running || kind == "subscription" || configured)) { [weak self] in self?.runAction(["action": "launch", "profile": id], message: "正在打开 \(name)…") })
        header.addArrangedSubview(button("改名", enabled: initialized && !running) { [weak self] in
            self?.renameProfile(id: id, name: name)
        })
        header.addArrangedSubview(button("关闭", enabled: running) { [weak self] in
            self?.confirm("关闭 \(name)？", detail: "请先确认该实例没有正在执行的任务。只关闭这个工具实例。", confirm: "关闭实例") { self?.runAction(["action": "stop", "profile": id], message: "正在关闭 \(name)…") }
        })
        let remove = button("删除", enabled: initialized && !running) { [weak self] in
            self?.confirm("删除 \(name)？", detail: "将移除此实例的受管应用包、独立登录、配置和缓存。原生 Codex 与共享项目、会话不会删除。", confirm: "删除实例") {
                self?.runAction(["action": "delete_profile", "profile": id], message: "正在删除 \(name)…")
            }
        }
        remove.contentTintColor = NSColor.systemRed
        header.addArrangedSubview(remove)
        view.addArrangedSubview(header); header.widthAnchor.constraint(equalTo: view.widthAnchor, constant: -36).isActive = true
        let details = horizontal(spacing: 10)
        details.addArrangedSubview(pill(running ? "窗口已打开" : "未打开", color: running ? NSColor(calibratedRed: 0.12, green: 0.43, blue: 0.29, alpha: 1) : .secondaryLabelColor, fill: running ? NSColor(calibratedRed: 0.88, green: 0.95, blue: 0.90, alpha: 1) : NSColor(calibratedWhite: 0.93, alpha: 1)))
        let authDescription = kind == "subscription" ? (configured ? "此额外窗口已单独登录" : "启动后可单独登录") : (profile["auth_label"] as? String ?? "待配置")
        let authentication = label(authDescription, size: 12, secondary: true)
        authentication.maximumNumberOfLines = 1
        authentication.lineBreakMode = .byTruncatingTail
        details.addArrangedSubview(authentication)
        view.addArrangedSubview(details)
        if kind == "api" {
            let options = horizontal(); let popup = providerPopup(selected: profile["provider_id"] as? String, allowEmpty: false)
            popup.isEnabled = !busy && !running; popup.setContentHuggingPriority(.defaultLow, for: .horizontal); controls.append(popup); options.addArrangedSubview(popup)
            options.addArrangedSubview(button("保存 API 配置", enabled: initialized && !running && !providers.isEmpty) { [weak self, weak popup] in
                guard let self, let selected = popup?.selectedItem?.representedObject as? String else { return }
                self.confirm("保存 \(name) 的 API 配置？", detail: "仅将所选 CC Switch 供应商的 API key 保存到此实例的专用目录；不复制官方 OAuth 登录。", confirm: "保存 API 配置") { self.runAction(["action": "configure_api", "profile": id, "provider_id": selected], message: "正在保存 \(name) 的 API 配置…") }
            }); view.addArrangedSubview(options); options.widthAnchor.constraint(equalTo: view.widthAnchor, constant: -36).isActive = true
        }
        view.heightAnchor.constraint(equalToConstant: kind == "api" ? 146 : 110).isActive = true
        return view
    }
    private func providerPopup(selected: String?, allowEmpty: Bool) -> NSPopUpButton {
        let popup = NSPopUpButton(frame: .zero, pullsDown: false)
        if allowEmpty { popup.addItem(withTitle: "稍后选择 API 供应商") }
        for provider in providers {
            let name = provider["name"] as? String ?? "API", model = provider["model"] as? String ?? ""
            popup.addItem(withTitle: model.isEmpty ? name : name + " · " + model); popup.lastItem?.representedObject = provider["id"] as? String
        }
        if popup.numberOfItems == 0 { popup.addItem(withTitle: "CC Switch 中暂无 API 供应商") }
        let current = providers.first { $0["is_current"] as? Bool == true }?["id"] as? String
        for id in [selected, allowEmpty ? nil : current].compactMap({ $0 }) {
            if let item = popup.itemArray.first(where: { $0.representedObject as? String == id }) { popup.select(item); break }
        }
        return popup
    }
    private func createProfile() {
        let alert = NSAlert(); alert.messageText = "新增 Codex 实例"
        alert.informativeText = "优先为不同登录方式创建额外窗口。若选择 API 供应商，点击创建即同意将该 API key 保存到新实例的本地目录；订阅实例需单独登录。"
        alert.addButton(withTitle: "创建实例"); alert.addButton(withTitle: "取消")
        let form = vertical(spacing: 10); form.frame = NSRect(x: 0, y: 0, width: 450, height: 178)
        let name = NSTextField(); name.placeholderString = "名称（可选，例如：研究 API）"
        let kind = NSPopUpButton(frame: .zero, pullsDown: false); kind.addItems(withTitles: ["API 实例", "受管订阅实例"])
        let provider = providerPopup(selected: nil, allowEmpty: true)
        form.addArrangedSubview(label("实例名称", size: 12, secondary: true)); form.addArrangedSubview(name); form.addArrangedSubview(kind); form.addArrangedSubview(provider); form.addArrangedSubview(label("供应商选项仅用于 API 实例。", size: 11, secondary: true))
        for control in [name, kind, provider] as [NSView] { control.widthAnchor.constraint(equalTo: form.widthAnchor).isActive = true }
        alert.accessoryView = form
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .alertFirstButtonReturn else { return }
            let selectedKind = kind.indexOfSelectedItem == 0 ? "api" : "subscription"
            var payload: [String: Any] = ["action": "create_profile", "kind": selectedKind]
            let value = name.stringValue.trimmingCharacters(in: .whitespacesAndNewlines); if !value.isEmpty { payload["name"] = value }
            if selectedKind == "api", let providerID = provider.selectedItem?.representedObject as? String { payload["provider_id"] = providerID }
            self.runAction(payload, message: "正在创建独立实例…")
        }
    }

    private func renameProfile(id: String, name: String) {
        let alert = NSAlert(); alert.messageText = "实例改名"
        alert.informativeText = "Dock、应用菜单和窗口标题将在下次打开时使用新名称。已有登录与会话保留。"
        alert.addButton(withTitle: "保存名称"); alert.addButton(withTitle: "取消")
        let field = NSTextField(string: name)
        field.frame = NSRect(x: 0, y: 0, width: 380, height: 26)
        alert.accessoryView = field
        alert.beginSheetModal(for: window) { [weak self] response in
            guard response == .alertFirstButtonReturn else { return }
            self?.runAction(["action": "rename_profile", "profile": id, "name": field.stringValue],
                            message: "正在保存实例名称…")
        }
    }
    private func renderSettings() {
        add(label("共享工作区", size: 15, weight: .semibold)); let config = card()
        config.addArrangedSubview(label("项目与会话使用原生 Codex 工作区；额外窗口分别保存自己的登录和 API 配置。", secondary: true))
        let synced = state["sync_enabled"] as? Bool == true
        if synced {
            config.addArrangedSubview(pill("已连接并启用受管同步", color: NSColor(calibratedRed: 0.12, green: 0.43, blue: 0.29, alpha: 1), fill: NSColor(calibratedRed: 0.88, green: 0.95, blue: 0.90, alpha: 1)))
        }
        let row = horizontal()
        if !initialized {
            row.addArrangedSubview(button("连接共享工作区") { [weak self] in self?.runAction(["action": "initialize_shared"], message: "正在连接原生 Codex 工作区…") })
        } else {
            let anyRunning = profiles.contains { $0["running"] as? Bool == true }
            row.addArrangedSubview(button("同步普通配置", enabled: !anyRunning) { [weak self] in self?.runAction(["action": "sync_settings"], message: "正在同步普通配置…") })
        }
        if initialized && !synced {
            row.addArrangedSubview(button("检查同步条件") { [weak self] in self?.runAction(["action": "sync_preflight"], message: "正在检查同步条件…") })
            row.addArrangedSubview(button("启用受管同步") { [weak self] in self?.runAction(["action": "enable_sync"], message: "正在检查历史并准备受管应用副本…") })
        }
        config.addArrangedSubview(row)
        config.heightAnchor.constraint(equalToConstant: synced ? 132 : 150).isActive = true
        add(config)
        let paths = card(); paths.addArrangedSubview(label("存储位置", size: 14, weight: .semibold))
        let locations = label("额外窗口的登录与缓存\n\(state["root"] as? String ?? root)\n\n共享项目与会话\n\(state["source_home"] as? String ?? "—")", size: 12, secondary: true)
        locations.isSelectable = true; paths.addArrangedSubview(locations); locations.widthAnchor.constraint(equalTo: paths.widthAnchor, constant: -40).isActive = true
        paths.heightAnchor.constraint(equalToConstant: 142).isActive = true
        add(paths)
        if let notice = state["notice"] as? String, !notice.isEmpty {
            let explanation = card()
            explanation.addArrangedSubview(label("当前同步说明", size: 14, weight: .semibold))
            let text = label(notice, size: 11, secondary: true)
            explanation.addArrangedSubview(text)
            text.widthAnchor.constraint(equalTo: explanation.widthAnchor, constant: -36).isActive = true
            explanation.heightAnchor.constraint(equalToConstant: 132).isActive = true
            add(explanation)
        }
    }
    private func pollActivity() {
        guard page == .activity, initialized, !busy, !activityLoading else { return }; activityLoading = true
        request(["action": "activity"]) { [weak self] result in
            guard let self else { return }; self.activityLoading = false; guard self.page == .activity else { return }
            switch result {
            case .failure(let error): self.activityTimestamp.stringValue = "状态暂不可用"; self.activityNotice.stringValue = error.localizedDescription
            case .success(let data):
                let payload = data as? [String: Any] ?? [:], threads = (data as? [String: Any])?["threads"] as? [[String: Any]] ?? []
                let signature = try? JSONSerialization.data(withJSONObject: threads, options: [.sortedKeys])
                let unnamed = threads.filter { ($0["state"] as? String) == "unknown" && ($0["name"] as? String) == ($0["thread_id"] as? String) }
                let running = threads.filter { ($0["state"] as? String) == "running" && ($0["source"] as? String) != "history_and_live_writer" }.count
                let inferred = threads.filter { ($0["state"] as? String) == "running" && ($0["source"] as? String) == "history_and_live_writer" }.count
                let occupied = threads.filter { ($0["busy"] as? Bool) != false }.count
                self.activitySummary.stringValue = "\(running) 个正在执行  ·  \(inferred) 个可能执行中  ·  \(occupied) 个写入权被占有"
                if self.activitySignature != signature {
                    self.activitySignature = signature; self.clear(self.activityList)
                    if threads.isEmpty { self.activityList.addArrangedSubview(self.label("当前没有被占有的会话。", secondary: true)) }
                    let ordered = threads.sorted { lhs, rhs in
                        func rank(_ value: [String: Any]) -> Int {
                            let state = value["state"] as? String ?? "unknown"
                            if state == "running" && value["source"] as? String != "history_and_live_writer" { return 0 }
                            if state == "running" { return 1 }
                            if state == "idle" { return 2 }
                            return 3
                        }
                        let left = rank(lhs), right = rank(rhs)
                        return left == right ? (lhs["name"] as? String ?? "") < (rhs["name"] as? String ?? "") : left < right
                    }
                    for thread in ordered where self.showUnnamedActivity || !unnamed.contains(where: { ($0["thread_id"] as? String) == (thread["thread_id"] as? String) }) {
                        let view = self.card(), row = self.horizontal(), current = thread["state"] as? String ?? "unknown"
                        let source = thread["source"] as? String ?? "observer"
                        if current == "running" && source != "history_and_live_writer" { let spinner = NSProgressIndicator(); spinner.style = .spinning; spinner.controlSize = .small; spinner.startAnimation(nil); row.addArrangedSubview(spinner) }
                        let threadName = self.label(thread["name"] as? String ?? "未命名会话", size: 14, weight: .semibold)
                        threadName.maximumNumberOfLines = 1
                        threadName.lineBreakMode = .byTruncatingTail
                        threadName.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
                        row.addArrangedSubview(threadName)
                        view.addArrangedSubview(row)
                        let execution = current == "running" ? (source == "history_and_live_writer" ? "执行：可能进行中" : "执行：进行中") : current == "idle" ? "执行：空闲" : "执行：未确认"
                        let executionColor = current == "running" ? NSColor(calibratedRed: 0.13, green: 0.35, blue: 0.68, alpha: 1) : .secondaryLabelColor
                        let executionFill = current == "running" ? NSColor(calibratedRed: 0.87, green: 0.92, blue: 0.99, alpha: 1) : NSColor(calibratedWhite: 0.93, alpha: 1)
                        let busy = thread["busy"] as? Bool
                        let writer = busy == true ? "写入权：占有" : busy == false ? "写入权：已释放" : "写入权：未确认"
                        let writerColor = busy == true ? NSColor(calibratedRed: 0.53, green: 0.31, blue: 0.09, alpha: 1) : .secondaryLabelColor
                        let writerFill = busy == true ? NSColor(calibratedRed: 0.99, green: 0.93, blue: 0.82, alpha: 1) : NSColor(calibratedWhite: 0.93, alpha: 1)
                        let details = self.horizontal(spacing: 8)
                        details.addArrangedSubview(self.pill(execution, color: executionColor, fill: executionFill))
                        details.addArrangedSubview(self.pill(writer, color: writerColor, fill: writerFill))
                        details.addArrangedSubview(self.label("占有者：" + (thread["profile_name"] as? String ?? thread["profile"] as? String ?? "未知实例"), size: 11, secondary: true))
                        view.addArrangedSubview(details)
                        if source == "history_and_live_writer" { view.addArrangedSubview(self.label("根据未完成任务和真实写入权推断；原窗口未提供实时执行事件。", size: 11, secondary: true)) }
                        view.heightAnchor.constraint(equalToConstant: source == "history_and_live_writer" ? 126 : 100).isActive = true
                        self.activityList.addArrangedSubview(view); view.widthAnchor.constraint(equalTo: self.activityList.widthAnchor).isActive = true
                    }
                    if !unnamed.isEmpty {
                        let toggle = self.button(self.showUnnamedActivity ? "收起 \(unnamed.count) 个未命名会话" : "显示 \(unnamed.count) 个状态未确认的会话") { [weak self] in
                            guard let self else { return }
                            self.showUnnamedActivity.toggle()
                            self.activitySignature = nil
                            self.pollActivity()
                        }
                        self.activityList.addArrangedSubview(toggle)
                    }
                }
                let formatter = DateFormatter(); formatter.dateFormat = "HH:mm:ss"
                self.activityTimestamp.stringValue = "最近检查 \(formatter.string(from: Date())) · 此页面可见时每 2 秒刷新"
                let shared = self.state["sync_enabled"] as? Bool ?? false
                self.activityNotice.stringValue = (payload["notice"] as? String ?? "") + (shared
                    ? "\n任务空闲后，后端仍可能占有写入权；只有写入权释放，其他窗口才能继续此会话。"
                    : "\n任务空闲和会话写入权分别判断。")
            }
        }
    }
    private func setBusy(_ value: Bool, message: String? = nil) {
        busy = value; if value { progress.startAnimation(nil); for control in controls { control.isEnabled = false } } else { progress.stopAnimation(nil) }
        if let message { status.stringValue = message }
    }
    private func instanceSignature(_ value: [String: Any]) -> Data? {
        let snapshot: [String: Any] = [
            "profiles": value["profiles"] ?? [],
            "history_count": value["history_count"] ?? 0,
            "sync_enabled": value["sync_enabled"] ?? false
        ]
        return try? JSONSerialization.data(withJSONObject: snapshot, options: [.sortedKeys])
    }
    private func renderKeepingScrollPosition() {
        let previousPosition = scroll.contentView.bounds.origin
        renderPage()
        window.contentView?.layoutSubtreeIfNeeded()
        scroll.contentView.scroll(to: previousPosition)
        scroll.reflectScrolledClipView(scroll.contentView)
    }
    private func pollInstances() {
        guard initialized, !busy, !instanceLoading else { return }
        instanceLoading = true
        request(["action": "status"]) { [weak self] result in
            guard let self else { return }
            self.instanceLoading = false
            guard self.page == .instances, !self.busy else { return }
            switch result {
            case .failure:
                self.status.stringValue = "自动状态更新暂不可用，可点击刷新重试。"
            case .success(let data):
                guard let next = data as? [String: Any] else { return }
                let changed = self.instanceSignature(next) != self.instanceSignature(self.state)
                self.state = next
                if changed { self.renderKeepingScrollPosition() }
            }
        }
    }
    private func refresh(finalMessage: String? = nil) {
        guard !busy else { return }; setBusy(true, message: "正在读取本地状态…")
        request(["action": "status"]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error): self.finishError(error)
            case .success(let data):
                self.state = data as? [String: Any] ?? [:]
                self.request(["action": "providers"]) { result in
                    switch result {
                    case .failure(let error): self.finishError(error)
                    case .success(let data):
                        self.providers = (data as? [[String: Any]] ?? []).filter { $0["kind"] as? String == "api_key" }
                        self.setBusy(false, message: finalMessage ?? "状态已更新。"); self.renderKeepingScrollPosition(); if self.page == .activity { self.pollActivity() }
                    }
                }
            }
        }
    }
    private func runAction(_ payload: [String: Any], message: String) {
        guard !busy else { return }; setBusy(true, message: message)
        request(payload) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error): self.finishError(error)
            case .success(let data): let final = (data as? [String: Any])?["message"] as? String ?? "操作完成。"; self.setBusy(false); self.refresh(finalMessage: final)
            }
        }
    }
    private func confirm(_ title: String, detail: String, confirm: String, action: @escaping () -> Void) {
        let alert = NSAlert(); alert.messageText = title; alert.informativeText = detail; alert.addButton(withTitle: confirm); alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { if $0 == .alertFirstButtonReturn { action() } }
    }
    private func finishError(_ error: Error) {
        setBusy(false, message: "操作未完成。"); renderPage()
        let alert = NSAlert(); alert.alertStyle = .warning; alert.messageText = "操作未完成"; alert.informativeText = error.localizedDescription
        alert.addButton(withTitle: "知道了"); alert.beginSheetModal(for: window)
    }
    private func failure(_ message: String) -> NSError { NSError(domain: "CodexOrbit", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    private func request(_ payload: [String: Any], completion: @escaping (Result<Any, Error>) -> Void) {
        let backendPath = backend.path, rootPath = root, cachedPython = pythonPath
        DispatchQueue.global(qos: .userInitiated).async {
            let result: Result<Any, Error>; var selectedPython = cachedPython
            do {
                if selectedPython == nil {
                    selectedPython = Bundle.main.resourceURL?.appendingPathComponent("python/bin/python3").path
                }
                guard let executable = selectedPython, FileManager.default.isExecutableFile(atPath: executable) else {
                    throw self.failure("应用包缺少内置 Python，请重新安装完整的 Codex Duo。")
                }
                guard FileManager.default.fileExists(atPath: backendPath) else { throw self.failure("未找到随应用提供的 backend.py，请使用完整应用包。") }
                let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = ["-E", "-s", "-B", backendPath, "--root", rootPath]
                var environment = ProcessInfo.processInfo.environment
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                process.environment = environment
                let input = Pipe(), output = Pipe(); process.standardInput = input; process.standardOutput = output; process.standardError = FileHandle.nullDevice
                try process.run(); try input.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: payload)); try input.fileHandleForWriting.close()
                let readOnly = ["status", "providers", "activity", "locks"].contains(payload["action"] as? String ?? "")
                let timeout = DispatchWorkItem { if process.isRunning { Darwin.kill(process.processIdentifier, SIGKILL) } }
                if readOnly { DispatchQueue.global().asyncAfter(deadline: .now() + 15, execute: timeout) }
                let bytes = output.fileHandleForReading.readDataToEndOfFile(); process.waitUntilExit()
                timeout.cancel()
                if readOnly && process.terminationReason == .uncaughtSignal && process.terminationStatus == SIGKILL {
                    throw self.failure("读取本地状态超时。请检查 Codex Duo 对工作区目录的访问权限，然后重试。")
                }
                guard let reply = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any] else { throw self.failure("后台未返回有效状态（退出码 \(process.terminationStatus)）。") }
                if reply["ok"] as? Bool != true { throw self.failure(reply["error"] as? String ?? "操作未完成。") }
                result = .success(reply["data"] ?? NSNull())
            } catch { result = .failure(error) }
            let python = selectedPython; DispatchQueue.main.async { self.pythonPath = python; completion(result) }
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { activityTimer?.invalidate(); instanceTimer?.invalidate() }
}

let app = NSApplication.shared
private let delegate = AppDelegate()
app.delegate = delegate
app.run()
