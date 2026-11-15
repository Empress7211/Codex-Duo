# Codex Duo 0.7.2 / build 21

2026-11-15 发布源码与 `Codex-Duo-0.7.2-macos-arm64.zip`，适用于 Apple Silicon、macOS 14+。解压后将 `Codex Duo.app` 放到 `/Applications`。另一台电脑须安装自己的官方 Codex，API 模式还需在 CC Switch 配置供应商，订阅模式在实例中登录。

相对 GitHub 上的 0.6.0，本次包含以下更新。

- 从本机原生 Codex 按需生成受管副本，按接口结构定位补丁，已停止的实例在原生更新后重建。
- API 窗口提供速度档位选择，配置同步保留已有选择。实际服务档位、费用与速度取决于供应商。
- 修复历史加载产生重复完成提醒，完善写入权释放、接管和异步探测期间的事件转发。
- 增加 `duo_history` 只读 MCP，支持查找与分页读取共享本地会话，修正内置插件市场实例路径。
- 使用原生签名 Node 托管 CLI，修复内置工具进程链鉴权问题。
- 应用包附带 portable Python 3.12.14 与许可文件，使用默认数据目录。实例、登录和历史在目标电脑上自行建立。

安装包使用 ad-hoc 签名，尚未完成 Developer ID 签名与 Apple 公证。macOS 阻止打开时，可在“系统设置 → 隐私与安全性”中选择“仍要打开”。本次构建、定向测试与签名检查的证明范围见 [验证记录](docs/validation.md)。全新 Mac 安装与真实模型调用尚未验证。

---

# Codex Duo 0.7.1 / build 20

修复受管窗口加载共享历史时，旧会话被误标为未读、Dock 提示数字增长的问题。

- `thread/read includeTurns:false` 只作为元数据查询，不再触发历史正文的事件同步。
- 完整读取、恢复和最新历史分页建立已有轮次的基线，不把历史完成状态补发为新完成事件；保留后续真实完成提醒。
- 已从本机后端或其他受管窗口收到的完成事件，不再由 SQLite 同步重复发送。
- 更新的是管理器中的同步桥接，无需重建原生应用副本。已打开窗口在下次正常重开时使用新桥接；不会批量清空已经持久化的未读提示。

通过合成 SQLite/事件日志的定向回归，覆盖元数据读取、完整历史、分页/恢复、首次轮询前完成和完成事件去重。未启动客户端或调用模型，界面效果待实际使用确认。

---

# Codex Duo 0.7.0 / build 19

本次调整运行时架构：不再打包固定 Codex 引擎，从用户本机安装的原生 Codex 按需生成受管副本。

- 删除 build 白名单与固定脚本路径/摘要门禁，按当前代码的启动、同步和写入权接口自动定位补丁。
- 自动解析应用入口、实际可执行文件与 Electron framework 版本；独立登录、窗口数据、IPC 和钥匙串边界继续保留。
- 原生更新后，只在对应实例停止时重建，复用原有登录、配置和共享会话；运行中副本不热替换。
- 接口缺失或歧义提示具体原因；失败的准备不会覆盖有效副本。自动检测不等于保证所有未来架构兼容。

构建命令：`./build.sh --python-runtime /path/to/portable/python`。构建不再需要 `--source-app`，运行时优先使用 `/Applications/Codex.app`，其次 `/Applications/ChatGPT.app`。

本次按用户要求未启动 Codex 实例、未执行模型请求或实际同步验收，只进行静态检查。默认仍为 ad-hoc 本地签名；旧版验证记录仅作历史证据。

---

# Codex Duo 0.6.0 源码发布说明

版本为 `0.6.0 / build 18`，本次公开发布源码、构建脚本和使用文档，不上传内含官方客户端引擎的本机测试 ZIP。默认使用 ad-hoc 签名；尚未完成正式 Developer ID 签名、Hardened Runtime 和 Apple 公证，不能称为正式公开发行就绪，也不保证安装和运行无系统提示。

## 支持范围与构建

当前目标为 Apple Silicon、macOS 14+，兼容用户自己的官方 Codex build `20398`（`26.1007.71326`）。API 登录需要用户配置自己的 CC Switch 供应商。其他官方 build、Intel Mac 或更早 macOS 不在当前验证范围。

全新克隆先按 [素材说明](assets/README.md) 准备本地图标。构建机准备 Swift 工具链、兼容的官方 Codex 应用和 portable Python 3.11+，执行：

```sh
./build.sh --python-runtime /path/to/portable/python --source-app /Applications/ChatGPT.app
```

构建入口会打包 Python 标准库和固定受管引擎，输出 `Codex Duo.app`。运行时克隆随包引擎并仅签名本地外层启动入口；用户无需 Homebrew、额外 Python、Signing 目录或签名私钥。发布包不包含账号、实例或运行数据。

发布者以后可在构建机选择签名身份：

```sh
./build.sh --python-runtime /path/to/portable/python --sign-identity "SIGNING_IDENTITY"
```

该参数只选择构建签名，不自动完成 Hardened Runtime、公证或公证票据附加。签名私钥只由发布者在构建阶段使用，不应随包提供给用户。公开分发包使用默认数据目录，勿带测试用途的 `runtime-location.json`。

## 当前验收边界

已留存的 0.6.0 开发验证及未验证项见 [验证记录](docs/validation.md)。完整二进制发行仍需按实际环境确认：

- 没有 Homebrew、外部 Python、签名材料和旧账号数据的初始化及实例启动。
- 固定引擎严格签名验证，跨实例与改名时保持内层引擎签名；新服务 `Codex Duo Vault` 保留正常存储加密。
- Dock、菜单和标题按实例命名；关闭、重开及改名复用实例资源；删除实例清理应用和缓存并保留共享历史。
- API、订阅及共享会话的实际行为；引擎升级可能再次触发的系统授权。

正式公开发行前还需要完成发布者签名、Hardened Runtime、公证及带下载隔离标记的安装验证，并核对随包组件的来源、版本与许可说明。当前未做真实网络流量审计；隐私边界见 [PRIVACY.md](PRIVACY.md)。重签实例的部分官方内置 MCP / Computer Use 辅助服务在测试中出现发送者认证或 pipe closed 错误，不能算作验证通过。API 实例未登录订阅时，云端会话和远程插件目录也不在可用性证明范围。

## 安装与更新

用户将完整应用放到 `/Applications/Codex Duo.app` 后打开。默认私有数据目录为 `~/Library/Application Support/Codex Duo/runtime`。应用更新前结束相关工具实例的工作并正常关闭；运行中的实例不会被热替换。固定引擎不自动跟随官方应用升级，兼容新 build 需要另行核验和重新构建。

实例关闭保留应用，重新打开复用原包；实例删除释放自己的应用、认证、配置和缓存，不删除共享原生历史。备份会话时应包含官方共享历史，不能只备份 runtime 中的软链接。

## 源码发布范围

原创代码采用 [MIT](LICENSE)。官方客户端、Python 解释器、第三方图标、账号与 runtime 均不进入 Git 历史，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。`outputs/` 仅作本机保留，不作为公开下载入口。
