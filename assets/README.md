# 本地图标素材

仓库保留历史图标的生成脚本，原始官方图标及生成图片只在本机使用，由 `.gitignore` 排除。已有本机图标无需重新生成；开源整理没有更换已安装应用的图标。

全新克隆后，构建需要 `assets/Orbit.png` 和 `assets/Orbit.icns`。若你有权在本地使用相应素材，可从自己安装的兼容 Codex 应用提取图标，再运行已有脚本：

```sh
# 按实际安装路径修改；当前兼容 build 20398。
DUO_SOURCE_APP='/Applications/ChatGPT.app'
DUO_ICON_NAME=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIconFile' "$DUO_SOURCE_APP/Contents/Info.plist")
# 当前兼容版本的 CFBundleIconFile 包含 .icns 扩展名。
/usr/bin/sips -s format png -z 1024 1024 "$DUO_SOURCE_APP/Contents/Resources/$DUO_ICON_NAME" --out assets/ChatGPT.png
/usr/bin/swift assets/make_icon.swift assets
python3 assets/make_icns.py
```

生成完成后，`Orbit.iconset/` 是可再生成的中间文件，可删除；构建仅使用 `Orbit.png` 和 `Orbit.icns`。这些素材及第三方商标不适用项目 MIT 许可，详见 [第三方说明](../THIRD_PARTY_NOTICES.md)。
