import AppKit

// Run with: swift assets/make_icon.swift assets && python3 assets/make_icns.py
// The knot is extracted from the bundled official ChatGPT icon. The source
// application's white tile is intentionally omitted so Orbit has one surface.
let root = CommandLine.arguments[1]
let sourceURL = URL(fileURLWithPath: root + "/ChatGPT.png")
guard let source = NSBitmapImageRep(data: try Data(contentsOf: sourceURL)) else {
    fatalError("Missing the bundled ChatGPT icon")
}

let cropOrigin = 168
let cropSize = 688
var knotPixels = [UInt8](repeating: 0, count: cropSize * cropSize * 4)
for y in 0..<cropSize {
    for x in 0..<cropSize {
        guard let color = source.colorAt(x: x + cropOrigin, y: y + cropOrigin)?
            .usingColorSpace(.deviceRGB) else { continue }
        let brightness = (0.2126 * color.redComponent
            + 0.7152 * color.greenComponent
            + 0.0722 * color.blueComponent) * 255
        let alpha = UInt8(max(0, min(255, ((236 - brightness) * 255 / 175).rounded())))
        let offset = (y * cropSize + x) * 4
        knotPixels[offset] = 248
        knotPixels[offset + 1] = 250
        knotPixels[offset + 2] = 251
        knotPixels[offset + 3] = alpha
    }
}
let provider = CGDataProvider(data: Data(knotPixels) as CFData)!
let knotCG = CGImage(
    width: cropSize, height: cropSize,
    bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: cropSize * 4,
    space: CGColorSpaceCreateDeviceRGB(),
    bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.last.rawValue),
    provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent
)!
let knot = NSImage(cgImage: knotCG, size: NSSize(width: cropSize, height: cropSize))

func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat) -> NSColor {
    NSColor(calibratedRed: red / 255, green: green / 255, blue: blue / 255, alpha: 1)
}

func draw(_ size: Int) -> Data {
    let image = NSImage(size: NSSize(width: size, height: size))
    image.lockFocus()
    NSGraphicsContext.current?.imageInterpolation = .high
    let scale = CGFloat(size) / 1024
    let transform = NSAffineTransform()
    transform.scale(by: scale)
    transform.concat()

    let tile = NSBezierPath(
        roundedRect: NSRect(x: 20, y: 20, width: 984, height: 984),
        xRadius: 226, yRadius: 226
    )
    NSGradient(starting: color(32, 51, 72), ending: color(14, 25, 39))!
        .draw(in: tile, angle: -65)

    // A large single mark reads at 16–64 px. Two small accents suggest
    // independent account contexts without repeating the whole app icon.
    knot.draw(in: NSRect(x: 206, y: 240, width: 594, height: 594),
              from: .zero, operation: .sourceOver, fraction: 1)

    color(12, 25, 39).setFill()
    NSBezierPath(roundedRect: NSRect(x: 626, y: 201, width: 210, height: 69),
                 xRadius: 35, yRadius: 35).fill()
    color(81, 216, 192).setFill()
    NSBezierPath(ovalIn: NSRect(x: 652, y: 207, width: 54, height: 54)).fill()
    color(134, 183, 232).setFill()
    NSBezierPath(ovalIn: NSRect(x: 718, y: 207, width: 54, height: 54)).fill()

    image.unlockFocus()
    let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
    return bitmap.representation(using: .png, properties: [:])!
}

let fileManager = FileManager.default
try fileManager.createDirectory(atPath: root + "/Orbit.iconset", withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    try draw(size).write(to: URL(fileURLWithPath: root + "/Orbit.iconset/icon_\(size)x\(size).png"))
    try draw(size * 2).write(to: URL(fileURLWithPath: root + "/Orbit.iconset/icon_\(size)x\(size)@2x.png"))
}
try draw(1024).write(to: URL(fileURLWithPath: root + "/Orbit.png"))
