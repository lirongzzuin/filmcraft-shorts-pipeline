// 이모지를 투명 PNG 로 굽는다. FilmCraft 텍스트 엔진은 컬러 이모지를 못 그리므로
// 자막의 화자 아이콘·스티커는 이 PNG 를 이미지 레이어로 얹는다.
//
//   swift scripts/render_emoji.swift <emoji> <out.png> [size=256]
import AppKit

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write("usage: render_emoji.swift <emoji> <out.png> [size]\n".data(using: .utf8)!)
    exit(2)
}
let emoji = args[1]
let out = URL(fileURLWithPath: args[2])
let size = args.count > 3 ? CGFloat(Double(args[3]) ?? 256) : 256

let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size), bitsPerSample: 8,
    samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
    bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let font = NSFont(name: "Apple Color Emoji", size: size * 0.8)!
let text = NSAttributedString(string: emoji, attributes: [.font: font])
let bounds = text.boundingRect(with: NSSize(width: size * 2, height: size * 2), options: [.usesLineFragmentOrigin])
text.draw(at: NSPoint(x: (size - bounds.width) / 2, y: (size - bounds.height) / 2 - bounds.origin.y))
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: out)
