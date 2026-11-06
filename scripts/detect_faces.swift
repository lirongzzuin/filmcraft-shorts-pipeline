// 영상·사진에서 얼굴(아기 포함) 위치를 찾는다. macOS 내장 Vision 사용 (무료·오프라인).
// 크롭·확대할 때 얼굴이 화면 밖으로 잘리지 않게 하는 '얼굴 보호'(babyreel/faces.py)가 쓴다.
//
//   swift scripts/detect_faces.swift <파일> <시작초> <끝초> [샘플 수=6]
//
// 출력(JSON): {"width": W, "height": H, "faces": [[x0, y0, x1, y1], ...], "texts": [[...], ...]}
//   좌표는 화면에 보이는 방향(회전 메타데이터 적용) 기준, 왼쪽 위 원점, 0~1 정규화.
//   얼굴 검출이 실패한 프레임은 사람 상반신(머리 포함) 검출로 대신한다.
//   texts = 글자 영역(이름표·병원명 등). 보정이 가려 둔 글자를 드러내지 않게 하는 데 쓴다.
//   frames = 샘플마다 {"t": 초, "faces": [...], "luma": 평균 밝기 0~1} (얼굴 블러 추적·노출 맞춤용)
import AVFoundation
import AppKit
import ImageIO
import Vision

let args = CommandLine.arguments
guard args.count >= 4, let t0 = Double(args[2]), let t1 = Double(args[3]) else {
    FileHandle.standardError.write("usage: detect_faces.swift <file> <start> <end> [samples]\n".data(using: .utf8)!)
    exit(2)
}
let url = URL(fileURLWithPath: args[1])
let samples = args.count > 4 ? max(1, Int(args[4]) ?? 6) : 6

var texts: [[Double]] = []
var frames: [[String: Any]] = []

func luma(_ image: CGImage) -> Double {
    // 32x32 로 줄여 평균 밝기 (Rec.709)
    let w = 32, h = 32
    var px = [UInt8](repeating: 0, count: w * h * 4)
    guard let ctx = CGContext(data: &px, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                              space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return 0.5 }
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
    var sum = 0.0
    for i in stride(from: 0, to: px.count, by: 4) {
        sum += 0.2126 * Double(px[i]) + 0.7152 * Double(px[i + 1]) + 0.0722 * Double(px[i + 2])
    }
    return sum / Double(w * h) / 255.0
}

func detect(_ image: CGImage) -> [[Double]] {
    let faces = VNDetectFaceRectanglesRequest()
    let bodies = VNDetectHumanRectanglesRequest()
    bodies.upperBodyOnly = true
    let words = VNDetectTextRectanglesRequest()
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try? handler.perform([faces, bodies, words])
    // Vision 좌표는 왼쪽 아래 원점 → 왼쪽 위 원점으로 뒤집는다
    func box(_ r: CGRect) -> [Double] {
        [Double(r.minX), Double(1 - r.maxY), Double(r.maxX), Double(1 - r.minY)]
    }
    texts += (words.results ?? []).map { box($0.boundingBox) }
    let f = (faces.results ?? []).filter { $0.confidence > 0.5 }.map { box($0.boundingBox) }
    if !f.isEmpty { return f }
    // 상반신 박스의 위쪽 45% ≈ 머리
    return (bodies.results ?? []).filter { $0.confidence > 0.5 }.map {
        let r = $0.boundingBox
        return box(CGRect(x: r.minX, y: r.maxY - r.height * 0.45, width: r.width, height: r.height * 0.45))
    }
}

var out: [[Double]] = []
var size = CGSize.zero
let ext = url.pathExtension.lowercased()
if ["jpg", "jpeg", "png", "heic", "heif"].contains(ext) {
    // 사진: EXIF 방향을 적용한 썸네일로 검출
    if let src = CGImageSourceCreateWithURL(url as CFURL, nil),
       let img = CGImageSourceCreateThumbnailAtIndex(src, 0, [
           kCGImageSourceCreateThumbnailFromImageAlways: true,
           kCGImageSourceCreateThumbnailWithTransform: true,
           kCGImageSourceThumbnailMaxPixelSize: 1280,
       ] as CFDictionary) {
        size = CGSize(width: img.width, height: img.height)
        out = detect(img)
        frames.append(["t": t0, "faces": out, "luma": luma(img)])
    }
} else {
    let asset = AVURLAsset(url: url)
    let gen = AVAssetImageGenerator(asset: asset)
    gen.appliesPreferredTrackTransform = true
    gen.maximumSize = CGSize(width: 1280, height: 1280)
    gen.requestedTimeToleranceBefore = CMTime(seconds: 0.1, preferredTimescale: 600)
    gen.requestedTimeToleranceAfter = CMTime(seconds: 0.1, preferredTimescale: 600)
    for i in 0..<samples {
        let t = samples == 1 ? t0 : t0 + (t1 - t0) * (Double(i) + 0.5) / Double(samples)
        if let img = try? gen.copyCGImage(at: CMTime(seconds: t, preferredTimescale: 600), actualTime: nil) {
            size = CGSize(width: img.width, height: img.height)
            let f = detect(img)
            out += f
            frames.append(["t": t, "faces": f, "luma": luma(img)])
        }
    }
}
let json: [String: Any] = ["width": Int(size.width), "height": Int(size.height), "faces": out, "texts": texts,
                           "frames": frames]
let data = try! JSONSerialization.data(withJSONObject: json)
print(String(data: data, encoding: .utf8)!)
