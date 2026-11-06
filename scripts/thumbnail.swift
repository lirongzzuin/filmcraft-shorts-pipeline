// 썸네일 렌더러 (macOS Core Graphics + Vision, 무료·오프라인). babyreel/thumbs.py 가 JSON 설정을 넘겨 실행한다.
//
//   thumbnail <spec.json>
//
// 1) 고른 순간 앞뒤(window)에서 프레임 여러 장을 뽑아 '주인공 얼굴'을 찾고, 얼굴 품질(Vision faceCaptureQuality)·
//    눈 뜸·선명도로 점수를 매겨 가장 좋은 프레임을 고른다 (흔들리거나 눈 감은 프레임 방지)
// 2) 사진을 꽉 채우고(카드 없음) 얼굴을 삼분할 지점에 크게 놓는다
// 3) 글자는 Core Text 로 실제 폭을 재서 상자에 들어가는 가장 큰 크기로 쓴다 (가로로 눌러 맞추지 않음 → 찌그러짐 없음)
// 4) 결과 사각형(얼굴·글자·배지·이모지)을 report JSON 으로 남겨 QA 가 겹침·잘림을 검사한다
import AVFoundation
import AppKit
import CoreImage
import CoreText
import ImageIO
import Vision

struct Spec: Decodable {
    let src: String
    let isPhoto: Bool
    let t: Double
    let window: Double
    let samples: Int
    let crop: [Double]          // [왼, 위, 오른, 아래] %
    let blur: [[Double]]        // 흐리게 할 얼굴 위치 [x, y] (크롭 전 화면 기준 0~1)
    let blurAreas: [[Double]]?  // 위치가 드러나는 곳(창밖 등) [x0, y0, x1, y1] (크롭 전 화면 기준 0~1), 강하게 흐림
    let faceHints: [[Double]]   // 주인공 얼굴 위치들 [[x, y], ...] (크롭 전 화면 기준 0~1). 여러 개면 함께 담는다
    let W: Int
    let H: Int
    let layout: String          // long | cover
    let faceFrac: Double        // 얼굴 높이 / 캔버스 높이 목표
    let lines: [String]
    let font: String            // 폰트 파일 경로 (Jua)
    let colors: [String]        // 줄별 글자색
    let stroke: String
    let badge: String
    let accent: String
    let emoji: String
    let blurOthers: Bool        // 주인공이 아닌 얼굴(행인 등)은 흐리게
    let subjectRefs: [String]   // 주인공(아기) 기준 사진 (assets/cast/첫째) — 얼굴이 여럿이면 누가 아기인지 고를 때
    let otherRefs: [String]     // 다른 가족 기준 사진 (아빠·엄마)
    let mirror: Bool            // true = 얼굴 왼쪽·글자 오른쪽 (A/B 테스트용 다른 구도)
    let pick: Int               // 0 = 가장 좋은 프레임, 1 = 두 번째 (A/B 테스트용, 0.4초 이상 떨어진)
    let minFont: Double
    let maxFont: Double
    let out: String
    let report: String
}

func color(_ hex: String, _ a: CGFloat = 1) -> CGColor {
    var h = hex.trimmingCharacters(in: .whitespaces)
    if h.hasPrefix("#") { h.removeFirst() }
    let v = UInt64(h, radix: 16) ?? 0xFFFFFF
    return CGColor(red: CGFloat((v >> 16) & 0xFF) / 255, green: CGFloat((v >> 8) & 0xFF) / 255,
                   blue: CGFloat(v & 0xFF) / 255, alpha: a)
}

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

let specURL = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "")
guard let specData = try? Data(contentsOf: specURL),
      let spec = try? JSONDecoder().decode(Spec.self, from: specData) else { fail("spec 읽기 실패") }

// --- 프레임 뽑기 ------------------------------------------------------------------------
var candidates: [(t: Double, img: CGImage)] = []
let url = URL(fileURLWithPath: spec.src)
if spec.isPhoto {
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let img = CGImageSourceCreateThumbnailAtIndex(src, 0, [
              kCGImageSourceCreateThumbnailFromImageAlways: true,
              kCGImageSourceCreateThumbnailWithTransform: true,
              kCGImageSourceThumbnailMaxPixelSize: 3840,
          ] as CFDictionary) else { fail("사진 열기 실패: \(spec.src)") }
    candidates.append((0, img))
} else {
    let asset = AVURLAsset(url: url)
    let gen = AVAssetImageGenerator(asset: asset)
    gen.appliesPreferredTrackTransform = true
    gen.requestedTimeToleranceBefore = .zero
    gen.requestedTimeToleranceAfter = .zero
    let dur = CMTimeGetSeconds(asset.duration)
    let n = max(1, spec.samples)
    for i in 0..<n {
        var t = n == 1 ? spec.t : spec.t - spec.window + 2 * spec.window * Double(i) / Double(n - 1)
        t = min(max(0, t), max(0, dur - 0.05))
        if let img = try? gen.copyCGImage(at: CMTime(seconds: t, preferredTimescale: 600), actualTime: nil) {
            candidates.append((t, img))
        }
    }
}
if candidates.isEmpty { fail("프레임을 뽑지 못했습니다") }

// 크롭 (이름표·병원명 가림은 썸네일에도 그대로)
func cropped(_ img: CGImage) -> CGImage {
    let w = Double(img.width), h = Double(img.height)
    let c = spec.crop.count == 4 ? spec.crop : [0, 0, 0, 0]
    let r = CGRect(x: w * c[0] / 100, y: h * c[1] / 100, width: w * (1 - (c[0] + c[2]) / 100),
                   height: h * (1 - (c[1] + c[3]) / 100)).integral
    return img.cropping(to: r) ?? img
}

// 크롭 전 좌표(0~1) → 크롭 후 좌표(0~1)
func toCropped(_ p: [Double]) -> (Double, Double) {
    let c = spec.crop.count == 4 ? spec.crop : [0, 0, 0, 0]
    let x = (p[0] - c[0] / 100) / max(0.01, 1 - (c[0] + c[2]) / 100)
    let y = (p[1] - c[1] / 100) / max(0.01, 1 - (c[1] + c[3]) / 100)
    return (x, y)
}

struct Face {
    var box: CGRect          // 0~1, 왼쪽 위 원점
    var quality: Double
    var eyes: Double         // 눈 뜬 정도 (0 = 감음)
    var smile: Double        // 입 너비/얼굴 너비 + 벌어짐 (웃을수록 큼)
}

func faces(_ img: CGImage) -> [Face] {
    let rects = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(cgImage: img, options: [:])
    try? handler.perform([rects])
    let found = (rects.results ?? []).filter { $0.confidence > 0.5 }
    if found.isEmpty { return [] }
    let quality = VNDetectFaceCaptureQualityRequest()
    quality.inputFaceObservations = found
    let marks = VNDetectFaceLandmarksRequest()
    marks.inputFaceObservations = found
    try? handler.perform([quality, marks])
    var out: [Face] = []
    for (i, f) in found.enumerated() {
        let b = f.boundingBox
        let box = CGRect(x: b.minX, y: 1 - b.maxY, width: b.width, height: b.height)
        var q = 0.5
        if let qs = quality.results, i < qs.count, let v = qs[i].faceCaptureQuality { q = Double(v) }
        var eyes = 0.25, smile = 0.0
        if let ms = marks.results, i < ms.count, let lm = ms[i].landmarks {
            if let lips = lm.outerLips?.normalizedPoints, lips.count >= 4 {
                let xs = lips.map { Double($0.x) }, ys = lips.map { Double($0.y) }
                smile = (xs.max()! - xs.min()!) + 0.6 * (ys.max()! - ys.min()!)
            }
            func open(_ r: VNFaceLandmarkRegion2D?) -> Double? {
                guard let pts = r?.normalizedPoints, pts.count >= 4 else { return nil }
                let xs = pts.map { Double($0.x) }, ys = pts.map { Double($0.y) }
                let w = (xs.max()! - xs.min()!), h = (ys.max()! - ys.min()!)
                return w > 0 ? h / w : nil
            }
            let vals = [open(lm.leftEye), open(lm.rightEye)].compactMap { $0 }
            if !vals.isEmpty { eyes = vals.reduce(0, +) / Double(vals.count) }
        }
        out.append(Face(box: box, quality: q, eyes: eyes, smile: smile))
    }
    return out
}

func sharpness(_ img: CGImage, _ box: CGRect) -> Double {
    // 얼굴 영역을 96x96 회색으로 줄여 라플라시안 분산 (흔들림 판별)
    let w = Double(img.width), h = Double(img.height)
    let r = CGRect(x: box.minX * w, y: box.minY * h, width: box.width * w, height: box.height * h).integral
    guard let part = img.cropping(to: r) else { return 0 }
    let n = 96
    var px = [UInt8](repeating: 0, count: n * n)
    guard let ctx = CGContext(data: &px, width: n, height: n, bitsPerComponent: 8, bytesPerRow: n,
                              space: CGColorSpaceCreateDeviceGray(), bitmapInfo: 0) else { return 0 }
    ctx.interpolationQuality = .medium
    ctx.draw(part, in: CGRect(x: 0, y: 0, width: n, height: n))
    var sum = 0.0, sum2 = 0.0, cnt = 0.0
    for y in 1..<(n - 1) {
        for x in 1..<(n - 1) {
            let c = Double(px[y * n + x]) * 4
            let l = c - Double(px[y * n + x - 1]) - Double(px[y * n + x + 1]) - Double(px[(y - 1) * n + x])
                - Double(px[(y + 1) * n + x])
            sum += l; sum2 += l * l; cnt += 1
        }
    }
    let mean = sum / cnt
    return sum2 / cnt - mean * mean
}

// 작은 얼굴(배경 행인)은 전체 화면에서 잘 안 잡힌다 → 고른 프레임만 3x3 조각으로 나눠 다시 찾는다
func smallFaces(_ img: CGImage) -> [CGRect] {
    var found: [CGRect] = []
    let w = Double(img.width), h = Double(img.height)
    for gy in 0..<3 {
        for gx in 0..<3 {
            let tw = w / 2.5, th = h / 2.5
            let tx = min(w - tw, Double(gx) * (w - tw) / 2), ty = min(h - th, Double(gy) * (h - th) / 2)
            guard let tile = img.cropping(to: CGRect(x: tx, y: ty, width: tw, height: th).integral) else { continue }
            let r = VNDetectFaceRectanglesRequest()
            try? VNImageRequestHandler(cgImage: tile, options: [:]).perform([r])
            for f in (r.results ?? []) where f.confidence > 0.6 {
                let b = f.boundingBox
                let box = CGRect(x: (tx + b.minX * tw) / w, y: (ty + (1 - b.maxY) * th) / h, width: b.width * tw / w,
                                 height: b.height * th / h)
                if !found.contains(where: { $0.intersects(box) }) { found.append(box) }
            }
        }
    }
    return found
}
// --- 누가 아기인지: 얼굴 조각의 이미지 특징(Vision FeaturePrint)을 기준 사진과 비교 ---------------------
func facePrint(_ img: CGImage, _ box: CGRect) -> VNFeaturePrintObservation? {
    let w = Double(img.width), h = Double(img.height)
    let r = CGRect(x: (box.minX - box.width * 0.2) * w, y: (box.minY - box.height * 0.25) * h,
                   width: box.width * 1.4 * w, height: box.height * 1.45 * h).intersection(CGRect(x: 0, y: 0, width: w, height: h)).integral
    guard let part = img.cropping(to: r) else { return nil }
    let req = VNGenerateImageFeaturePrintRequest()
    try? VNImageRequestHandler(cgImage: part, options: [:]).perform([req])
    return req.results?.first
}
func refPrints(_ paths: [String]) -> [VNFeaturePrintObservation] {
    paths.compactMap { p -> VNFeaturePrintObservation? in
        guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: p) as CFURL, nil),
              let img = CGImageSourceCreateThumbnailAtIndex(src, 0, [kCGImageSourceCreateThumbnailFromImageAlways: true,
                  kCGImageSourceCreateThumbnailWithTransform: true, kCGImageSourceThumbnailMaxPixelSize: 1600] as CFDictionary)
        else { return nil }
        guard let f = faces(img).max(by: { $0.box.height < $1.box.height }) else { return nil }
        return facePrint(img, f.box)
    }
}
func distance(_ a: VNFeaturePrintObservation, _ refs: [VNFeaturePrintObservation]) -> Double {
    refs.compactMap { r -> Double? in var d: Float = 0; return (try? a.computeDistance(&d, to: r)) != nil ? Double(d) : nil }.min() ?? 99
}
let subjectPrints = spec.faceHints.isEmpty ? refPrints(spec.subjectRefs) : []
let otherPrints = spec.faceHints.isEmpty ? refPrints(spec.otherRefs) : []
var identified = 0
/// 얼굴이 여럿일 때 기준 사진과 가장 닮은 얼굴 (아기 − 어른 거리 차가 가장 작은 것)
func likelySubject(_ img: CGImage, _ pool: [Face]) -> Face? {
    guard pool.count > 1, !subjectPrints.isEmpty else { return nil }
    let scored = pool.compactMap { f -> (Face, Double)? in
        guard let p = facePrint(img, f.box) else { return nil }
        let ds = distance(p, subjectPrints), dot = otherPrints.isEmpty ? 0 : distance(p, otherPrints)
        return (f, otherPrints.isEmpty ? ds : ds - dot)
    }
    return scored.min { $0.1 < $1.1 }?.0
}

// 흐리게 할 얼굴(타인)은 주인공 후보에서 뺀다
let blurPts = spec.blur.map { toCropped($0) }
func dist(_ f: Face, _ p: (Double, Double)) -> Double { hypot(Double(f.box.midX) - p.0, Double(f.box.midY) - p.1) }
// 블러 위치마다 '가장 가까운 얼굴 하나'만 가린다 (반경 안의 얼굴을 다 빼면 옆의 아기까지 빠진다)
func blurredSet(_ fs: [Face]) -> [CGRect] {
    blurPts.compactMap { p in fs.min { dist($0, p) < dist($1, p) }.flatMap { dist($0, p) < 0.2 ? $0.box : nil } }
}

struct Scored { let t: Double; let img: CGImage; let face: Face?; let others: [Face]; let score: Double; let sharp: Double }
var scored: [Scored] = []
let hintPts = spec.faceHints.map { toCropped($0) }
var prev: (Double, Double)? = hintPts.first
for c in candidates {
    let img = cropped(c.img)
    var all = faces(img)
    if all.isEmpty {  // 작은 얼굴(거울 속·멀리 있는 아기)은 조각으로 나눠 다시 찾는다
        all = smallFaces(img).map { Face(box: $0, quality: 0.4, eyes: 0.22, smile: 0) }
    }
    let hidden = blurredSet(all)
    let pool = all.filter { f in !hidden.contains(f.box) }
    var subject: Face? = nil
    if hintPts.count > 1 {
        // 여러 얼굴을 함께: 힌트마다 가장 가까운 얼굴을 모아 그 전체를 '주인공'으로
        let picked = hintPts.compactMap { p in pool.min { hypot($0.box.midX - p.0, $0.box.midY - p.1) < hypot($1.box.midX - p.0, $1.box.midY - p.1) } }
            .filter { f in hintPts.contains { hypot(f.box.midX - $0.0, f.box.midY - $0.1) < 0.3 } }
        if picked.count == hintPts.count {
            let u = picked.dropFirst().reduce(picked[0].box) { $0.union($1.box) }
            subject = Face(box: u, quality: picked.map { $0.quality }.min() ?? 0, eyes: picked.map { $0.eyes }.min() ?? 0,
                           smile: picked.map { $0.smile }.max() ?? 0)
        }
    } else if let p = prev {
        subject = pool.min { hypot($0.box.midX - p.0, $0.box.midY - p.1) < hypot($1.box.midX - p.0, $1.box.midY - p.1) }
        if let s = subject, hypot(s.box.midX - p.0, s.box.midY - p.1) > 0.3 { subject = nil }
    } else if let s = likelySubject(img, pool) {
        subject = s
        identified += 1
    } else {
        subject = pool.max { $0.box.height < $1.box.height }
    }
    if hintPts.isEmpty, prev == nil, subjectPrints.isEmpty, let s = subject { prev = (Double(s.box.midX), Double(s.box.midY)) }
    let others = all.filter { o in subject.map { !$0.box.contains(CGPoint(x: o.box.midX, y: o.box.midY)) } ?? true }
    var score = -10.0, sharp = 0.0
    if let f = subject {
        sharp = sharpness(img, f.box)
        // 얼굴 품질 + 눈 뜸 + 웃음 + 기획한 순간과 가까움 (Claude 가 컨택트시트에서 고른 순간을 존중)
        // 눈이 또렷이 보이는 게 가장 중요 (눈 맞춤). 눈을 찡그려 감으면 웃어도 작게 보면 우는 얼굴처럼 보인다
        // Vision 얼굴 상자는 넉넉해서(볼까지) 가까이 찍으면 화면 끝을 조금 넘는다 → 얼굴 폭의 12% 넘게 나가야 '잘림'
        let over = max(0, -f.box.minX, f.box.maxX - 1) / max(0.01, f.box.width) + max(0, -f.box.minY, f.box.maxY - 1) / max(0.01, f.box.height)
        score = 2 * f.quality + 1.6 * min(1, f.eyes / 0.22) + (f.eyes < 0.12 ? -0.8 : 0) + (over <= 0.12 ? 0.5 : -3)
            + 0.6 * max(0, min(1, (f.smile - 0.45) / 0.15)) - 0.5 * abs(c.t - spec.t) / max(0.1, spec.window)
    }
    scored.append(Scored(t: c.t, img: img, face: subject, others: others, score: score, sharp: sharp))
}
let maxSharp = max(1e-6, scored.map { $0.sharp }.max() ?? 1)
let ranked = scored.map { s -> (Scored, Double) in (s, s.score + 0.8 * s.sharp / maxSharp) }
    .sorted { $0.1 > $1.1 }
var chosen = ranked[0].0
if spec.pick > 0 {
    // 두 번째 후보: 첫 후보와 0.4초 이상 떨어진 가장 좋은 프레임 (A/B 테스트용 다른 순간)
    if let alt = ranked.dropFirst().first(where: { abs($0.0.t - chosen.t) >= 0.4 && $0.0.face != nil }) { chosen = alt.0 }
}
let img = chosen.img
let iw = Double(img.width), ih = Double(img.height)

if spec.blurOthers, let subj = chosen.face {
    let known = chosen.others.map { $0.box } + [subj.box]
    let extra = smallFaces(img).filter { b in !known.contains { $0.intersects(b) } && !subj.box.intersects(b) }
    chosen = Scored(t: chosen.t, img: chosen.img, face: chosen.face,
                    others: chosen.others + extra.map { Face(box: $0, quality: 0, eyes: 0, smile: 0) },
                    score: chosen.score, sharp: chosen.sharp)
}
let W = Double(spec.W), H = Double(spec.H)

// --- 배치 ------------------------------------------------------------------------------------
// 가로 썸네일 + 세로/정사각 원본 → '분할': 오른쪽 60% 사진 패널, 왼쪽은 같은 사진을 흐리고 어둡게 깔고 글자
// 가로 원본 → 꽉 채우고 얼굴을 삼분할 지점에. 세로 썸네일(쇼츠 커버) → 꽉 채우고 얼굴은 아래쪽, 글자는 위
var faceRect = CGRect(x: 0.4, y: 0.3, width: 0.2, height: 0.3) // 얼굴을 못 찾으면 가운데 위쪽 가정
if let f = chosen.face { faceRect = f.box }
let fx = faceRect.midX * iw, fy = faceRect.midY * ih, fh = faceRect.height * ih
let split = spec.layout == "long" && iw / ih < 1.25
let panel: CGRect = split ? CGRect(x: spec.mirror ? 0 : W * 0.40, y: 0, width: W * 0.60, height: H) : CGRect(x: 0, y: 0, width: W, height: H)
let coverScale = max(panel.width / iw, panel.height / ih)
let maxFace = spec.layout == "long" ? 0.62 : 0.40  // 얼굴이 이보다 크면 잘려 보인다
var scale = max(coverScale, spec.faceFrac * H / max(1, fh))
scale = min(scale, max(coverScale, maxFace * H / max(1, fh)))
scale = min(scale, max(coverScale, 2.4 * coverScale)) // 지나친 확대(흐릿함) 방지
var faceRight = split ? true : (spec.layout == "long" ? faceRect.midX >= 0.42 : true)
if spec.mirror && spec.layout == "long" { faceRight.toggle() }
let targetX = split ? panel.midX : (spec.layout == "long" ? (faceRight ? 0.66 : 0.34) * W : 0.5 * W)
var targetY = spec.layout == "long" ? 0.52 * H : 0.62 * H
if spec.layout != "long" { targetY = max(targetY, 0.36 * H + fh * scale * 0.95) } // 머리 위로 글자 자리를 비운다
var ox = targetX - fx * scale, oy = targetY - fy * scale
ox = min(panel.minX, max(panel.maxX - iw * scale, ox))
oy = min(panel.minY, max(panel.maxY - ih * scale, oy))
let faceCanvas = CGRect(x: ox + faceRect.minX * iw * scale, y: oy + faceRect.minY * ih * scale,
                        width: faceRect.width * iw * scale, height: faceRect.height * ih * scale)
// 머리(머리카락 포함) = 얼굴 상자를 위로 45%, 옆으로 15% 넓힌 것. 글자·이모지는 여기를 피한다
let headCanvas = CGRect(x: faceCanvas.minX - faceCanvas.width * 0.15, y: faceCanvas.minY - faceCanvas.height * 0.45,
                        width: faceCanvas.width * 1.3, height: faceCanvas.height * 1.55)

// --- 그리기 --------------------------------------------------------------------------------
let ciCtx = CIContext()
var blurredOthers = 0
var ci = CIImage(cgImage: img)
// 창밖 풍경 등 위치가 드러나는 곳 (영상의 clips[].blur_areas 와 같은 영역)
for a in spec.blurAreas ?? [] where a.count == 4 {
    let p0 = toCropped([a[0], a[1]]), p1 = toCropped([a[2], a[3]])
    let r = CGRect(x: max(0, p0.0), y: max(0, p0.1), width: min(1, p1.0) - max(0, p0.0), height: min(1, p1.1) - max(0, p0.1))
    guard r.width > 0, r.height > 0 else { continue }
    let rr = CGRect(x: r.minX * iw, y: (1 - r.maxY) * ih, width: r.width * iw, height: r.height * ih)
    let blurred = ci.clampedToExtent().applyingGaussianBlur(sigma: max(30, max(rr.width, rr.height) / 8)).cropped(to: ci.extent)
    let mask = CIImage(color: .white).cropped(to: rr.insetBy(dx: -6, dy: -6)).applyingGaussianBlur(sigma: 8).cropped(to: ci.extent)
    ci = blurred.applyingFilter("CIBlendWithMask", parameters: [kCIInputBackgroundImageKey: ci, kCIInputMaskImageKey: mask])
}
// 타인 얼굴 블러 (블러 위치 근처 얼굴, 못 찾으면 그 자리)
for p in blurPts {
    let face = chosen.others.min { hypot($0.box.midX - p.0, $0.box.midY - p.1) < hypot($1.box.midX - p.0, $1.box.midY - p.1) }
    var r = CGRect(x: p.0 - 0.08, y: p.1 - 0.08, width: 0.16, height: 0.16)
    if let f = face, hypot(f.box.midX - p.0, f.box.midY - p.1) < 0.2 { r = f.box.insetBy(dx: -f.box.width * 0.25, dy: -f.box.height * 0.25) }
    let rr = CGRect(x: r.minX * iw, y: (1 - r.maxY) * ih, width: r.width * iw, height: r.height * ih)
    let blurred = ci.clampedToExtent().applyingGaussianBlur(sigma: max(18, rr.width / 6)).cropped(to: ci.extent)
    let mask = CIImage(color: .white).cropped(to: rr).applyingGaussianBlur(sigma: rr.width / 10).cropped(to: ci.extent)
    ci = blurred.applyingFilter("CIBlendWithMask", parameters: [kCIInputBackgroundImageKey: ci, kCIInputMaskImageKey: mask])
}
// 주인공이 아닌 얼굴 (행인·타인): 주인공보다 작은 얼굴은 흐리게 (부모를 함께 담으려면 faces 에 같이 지정)
if spec.blurOthers, let subj = chosen.face {
    let hidden = blurredSet(chosen.others)
    for o in chosen.others where o.box.height < subj.box.height * 1.2 && !hidden.contains(o.box) {
        let r = o.box.insetBy(dx: -o.box.width * 0.3, dy: -o.box.height * 0.3)
        let rr = CGRect(x: r.minX * iw, y: (1 - r.maxY) * ih, width: r.width * iw, height: r.height * ih)
        let blurred = ci.clampedToExtent().applyingGaussianBlur(sigma: max(14, rr.width / 5)).cropped(to: ci.extent)
        let mask = CIImage(color: .white).cropped(to: rr).applyingGaussianBlur(sigma: rr.width / 8).cropped(to: ci.extent)
        ci = blurred.applyingFilter("CIBlendWithMask", parameters: [kCIInputBackgroundImageKey: ci, kCIInputMaskImageKey: mask])
        blurredOthers += 1
    }
}
// 색감: 채도·대비 조금 올리고, 얼굴이 어두우면 살짝 밝게
ci = ci.applyingFilter("CIVibrance", parameters: ["inputAmount": 0.25])
ci = ci.applyingFilter("CIColorControls", parameters: ["inputSaturation": 1.08, "inputContrast": 1.06, "inputBrightness": 0.0])
var faceLuma = 0.5
if let f = chosen.face {
    let fr = CGRect(x: f.box.minX * iw, y: (1 - f.box.maxY) * ih, width: f.box.width * iw, height: f.box.height * ih)
    let avg = ci.applyingFilter("CIAreaAverage", parameters: [kCIInputExtentKey: CIVector(cgRect: fr)])
    var px = [UInt8](repeating: 0, count: 4)
    ciCtx.render(avg, toBitmap: &px, rowBytes: 4, bounds: CGRect(x: 0, y: 0, width: 1, height: 1), format: .RGBA8,
                 colorSpace: CGColorSpaceCreateDeviceRGB())
    faceLuma = (0.2126 * Double(px[0]) + 0.7152 * Double(px[1]) + 0.0722 * Double(px[2])) / 255
    if faceLuma < 0.42 {
        ci = ci.applyingFilter("CIExposureAdjust", parameters: ["inputEV": min(0.8, (0.5 - faceLuma) * 2.2)])
    }
}
guard let graded = ciCtx.createCGImage(ci, from: ci.extent) else { fail("색 보정 실패") }

let cs = CGColorSpaceCreateDeviceRGB()
guard let ctx = CGContext(data: nil, width: spec.W, height: spec.H, bitsPerComponent: 8, bytesPerRow: 0, space: cs,
                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { fail("캔버스 실패") }
// 위쪽 원점 좌표로 그리기 쉽게: y 를 뒤집는 대신 직접 변환
func flip(_ r: CGRect) -> CGRect { CGRect(x: r.minX, y: H - r.maxY, width: r.width, height: r.height) }
ctx.interpolationQuality = .high
if split {
    // 바탕: 같은 사진을 크게 흐리고 어둡게 (카드·테두리 없이 이어지게)
    let bgScale = max(W / iw, H / ih)
    let bg = CIImage(cgImage: graded).clampedToExtent().applyingGaussianBlur(sigma: 40).cropped(to: CGRect(x: 0, y: 0, width: iw, height: ih))
        .applyingFilter("CIColorControls", parameters: ["inputBrightness": -0.40, "inputSaturation": 0.5, "inputContrast": 0.9])
    if let bgImg = ciCtx.createCGImage(bg, from: CGRect(x: 0, y: 0, width: iw, height: ih)) {
        ctx.draw(bgImg, in: flip(CGRect(x: (W - iw * bgScale) / 2, y: (H - ih * bgScale) / 2, width: iw * bgScale, height: ih * bgScale)))
    }
    ctx.saveGState()
    ctx.clip(to: flip(panel))
    ctx.draw(graded, in: flip(CGRect(x: ox, y: oy, width: iw * scale, height: ih * scale)))
    ctx.restoreGState()
    // 이음매를 부드럽게
    if let g = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: [color("#000000", 0.55), color("#000000", 0.0)] as CFArray, locations: [0, 1]) {
        let seam = spec.mirror ? panel.maxX : panel.minX
        ctx.drawLinearGradient(g, start: CGPoint(x: seam, y: 0), end: CGPoint(x: seam + (spec.mirror ? -W * 0.10 : W * 0.10), y: 0), options: [])
    }
} else {
    ctx.draw(graded, in: flip(CGRect(x: ox, y: oy, width: iw * scale, height: ih * scale)))
}

// 글자 쪽 어둡게 (가독성)
let textLeft = spec.layout == "long" ? faceRight : false  // 얼굴 반대쪽에 글자
let gradColors = [color("#000000", 0.62), color("#000000", 0.0)] as CFArray
if let g = CGGradient(colorsSpace: cs, colors: gradColors, locations: [0, 1]) {
    if spec.layout == "long" && split {
        if spec.mirror { ctx.drawLinearGradient(g, start: CGPoint(x: W, y: 0), end: CGPoint(x: W * 0.58, y: 0), options: [.drawsAfterEndLocation]) }
        else { ctx.drawLinearGradient(g, start: CGPoint(x: 0, y: 0), end: CGPoint(x: W * 0.42, y: 0), options: [.drawsAfterEndLocation]) }
    } else if spec.layout == "long" {
        let x0 = faceRight ? 0.0 : W, x1 = faceRight ? W * 0.62 : W * 0.38
        ctx.drawLinearGradient(g, start: CGPoint(x: x0, y: 0), end: CGPoint(x: x1, y: 0), options: [])
    } else {
        ctx.drawLinearGradient(g, start: CGPoint(x: 0, y: H), end: CGPoint(x: 0, y: H * 0.55), options: [])
    }
}

// 폰트
CTFontManagerRegisterFontsForURL(URL(fileURLWithPath: spec.font) as CFURL, .process, nil)
let fontName: CFString = {
    if let descs = CTFontManagerCreateFontDescriptorsFromURL(URL(fileURLWithPath: spec.font) as CFURL) as? [CTFontDescriptor],
       let d = descs.first, let n = CTFontDescriptorCopyAttribute(d, kCTFontNameAttribute) as? String { return n as CFString }
    return "AppleSDGothicNeo-Heavy" as CFString
}()

func line(_ s: String, _ size: Double, _ fill: CGColor, stroke: CGColor? = nil, strokePct: Double = 0) -> CTLine {
    let font = CTFontCreateWithName(fontName, size, nil)
    var attrs: [NSAttributedString.Key: Any] = [
        NSAttributedString.Key(kCTFontAttributeName as String): font,
        NSAttributedString.Key(kCTForegroundColorAttributeName as String): fill,
    ]
    if let st = stroke {  // 양수 = 외곽선만 (글꼴 크기 대비 %). 선은 글자 윤곽 양쪽으로 반씩
        attrs[NSAttributedString.Key(kCTStrokeColorAttributeName as String)] = st
        attrs[NSAttributedString.Key(kCTStrokeWidthAttributeName as String)] = strokePct
    }
    return CTLineCreateWithAttributedString(NSAttributedString(string: s, attributes: attrs))
}

func width(_ l: CTLine) -> Double { Double(CTLineGetTypographicBounds(l, nil, nil, nil)) }

// 배지 (작게, 채널 일관성)
var badgeRect = CGRect.zero
if !spec.badge.isEmpty {
    let bs = spec.layout == "long" ? 30.0 : 44.0
    let bl = line(spec.badge, bs, color("#FFFFFF"))
    let bw = width(bl) + bs * 1.1, bh = bs * 1.6
    let bx = spec.layout == "long" ? (faceRight ? W * 0.035 : W - W * 0.035 - bw) : (W - bw) / 2
    let by = spec.layout == "long" ? H * 0.06 : H * 0.075
    badgeRect = CGRect(x: bx, y: by, width: bw, height: bh)
    ctx.setFillColor(color(spec.accent, 0.95))
    ctx.addPath(CGPath(roundedRect: flip(badgeRect), cornerWidth: bh / 2, cornerHeight: bh / 2, transform: nil))
    ctx.fillPath()
    ctx.textPosition = CGPoint(x: bx + bs * 0.55, y: H - by - bh / 2 - bs * 0.35)
    CTLineDraw(bl, ctx)
}

// 글자 상자: 먼저 머리(머리카락)까지 피하고, 자리가 모자라면 얼굴(눈·코·입)만 피한다
func textBox(_ avoid: CGRect) -> CGRect {
    if spec.layout == "long" {
        let x0 = textLeft ? W * 0.05 : max(W * 0.05, avoid.maxX + W * 0.02)
        let x1 = textLeft ? min(W * 0.95, avoid.minX - W * 0.02) : W * 0.95
        let w = max(W * 0.30, min(W * 0.58, x1 - x0))
        let left = textLeft ? x0 : W * 0.95 - w
        let top = badgeRect.maxY + H * 0.04
        let bottom = H * (textLeft ? 0.92 : 0.80) // 오른쪽 아래는 재생 시간 표시가 덮는다
        return CGRect(x: left, y: top, width: w, height: bottom - top)
    }
    if coverBelow {  // 얼굴 아래 (인스타 그리드 가운데 3:4 안쪽: 아래 끝 0.86H)
        let top = max(avoid.maxY + H * 0.015, H * 0.5)
        return CGRect(x: W * 0.07, y: top, width: W * 0.86, height: max(H * 0.06, H * 0.86 - top))
    }
    let top = badgeRect.maxY + H * 0.03
    let bottom = min(H * 0.42, avoid.minY - H * 0.01)
    return CGRect(x: W * 0.07, y: top, width: W * 0.86, height: max(H * 0.06, bottom - top))
}
var coverBelow = false
var box = textBox(headCanvas)
// 들어가는 가장 큰 크기 (줄 높이 1.08, 외곽선 두께 포함)
let strokeRatio = 0.16
var size = spec.maxFont
let n = Double(max(1, spec.lines.count))
func fits(_ s: Double) -> Bool {
    let pad = s * strokeRatio
    let widest = spec.lines.map { width(line($0, s, color("#FFFFFF"))) }.max() ?? 0
    return widest + pad * 2 <= box.width && n * s * 1.08 + pad * 2 <= box.height
}
while size > spec.minFont && !fits(size) { size -= 2 }
func bestSize() -> Double {
    var s = spec.maxFont
    while s > spec.minFont && !fits(s) { s -= 2 }
    return s
}
if !fits(size) {
    box = textBox(faceCanvas)
    size = bestSize()
}
if spec.layout != "long" && (!fits(size) || size < spec.minFont * 1.15) {
    // 위쪽 자리가 좁으면 얼굴 아래에 (머리 위에 억지로 작게 쓰지 않는다)
    let above = (box, size)
    coverBelow = true
    box = textBox(headCanvas)
    size = bestSize()
    if !fits(size) || size <= above.1 { coverBelow = false; box = above.0; size = above.1 }
}
let fit = fits(size)

var textRects: [[Double]] = []
let totalH = n * size * 1.08
var y = spec.layout == "long" ? box.midY - totalH / 2 : box.minY + (box.height - totalH) / 2
for (i, s) in spec.lines.enumerated() {
    let fill = color(spec.colors.count > i ? spec.colors[i] : spec.colors.last ?? "#FFFFFF")
    let l = line(s, size, fill)
    let w = width(l)
    let x: Double
    if spec.layout == "long" { x = textLeft ? box.minX + size * strokeRatio : box.maxX - w - size * strokeRatio }
    else { x = (W - w) / 2 }
    var ascent: CGFloat = 0, descent: CGFloat = 0
    CTLineGetTypographicBounds(l, &ascent, &descent, nil)
    let baseline = y + (size * 1.08 + Double(ascent) - Double(descent)) / 2
    let pos = CGPoint(x: x, y: H - baseline)
    // 그림자 깔린 두꺼운 외곽선 → 그 위에 채우기
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -size * 0.06), blur: size * 0.12, color: color("#000000", 0.55))
    ctx.textPosition = pos
    CTLineDraw(line(s, size, color(spec.stroke), stroke: color(spec.stroke), strokePct: strokeRatio * 200), ctx)
    ctx.restoreGState()
    ctx.textPosition = pos
    CTLineDraw(l, ctx)
    let pad = size * strokeRatio
    textRects.append([x - pad, baseline - Double(ascent) - pad, x + w + pad, baseline + Double(descent) + pad])
    y += size * 1.08
}

// 이모지 하나: 얼굴 바깥쪽 위 모서리 (글자·얼굴과 안 겹치는 자리만)
var emojiRect = CGRect.zero
if !spec.emoji.isEmpty {
    let es = spec.layout == "long" ? H * 0.17 : H * 0.075
    let ef = CTFontCreateWithName("AppleColorEmoji" as CFString, es, nil)
    let attr = NSAttributedString(string: spec.emoji, attributes: [NSAttributedString.Key(kCTFontAttributeName as String): ef])
    let eline = CTLineCreateWithAttributedString(attr)
    let ew = width(eline)
    let spots: [CGPoint] = [
        CGPoint(x: (textLeft ? headCanvas.maxX : headCanvas.minX - ew), y: faceCanvas.minY - es * 0.2),
        CGPoint(x: (textLeft ? headCanvas.maxX : headCanvas.minX - ew), y: faceCanvas.maxY - es),
        CGPoint(x: (textLeft ? headCanvas.minX - ew : headCanvas.maxX), y: faceCanvas.maxY - es * 0.6),
    ]
    let tr = textRects.map { CGRect(x: $0[0], y: $0[1], width: $0[2] - $0[0], height: $0[3] - $0[1]) }
    for p in spots {
        let r = CGRect(x: p.x, y: p.y, width: ew, height: es)
        let inside = r.minX >= W * 0.02 && r.maxX <= W * 0.98 && r.minY >= H * 0.02 && r.maxY <= H * 0.98
        let badgeHit = badgeRect.intersects(r)
        let bottomRight = spec.layout == "long" && r.maxX > W * 0.8 && r.maxY > H * 0.82
        if inside && !badgeHit && !bottomRight && !r.intersects(faceCanvas)
            && !tr.contains(where: { $0.intersects(r) }) {
            emojiRect = r
            ctx.textPosition = CGPoint(x: r.minX, y: H - r.maxY + es * 0.2)
            CTLineDraw(eline, ctx)
            break
        }
    }
}

guard let outImg = ctx.makeImage() else { fail("이미지 생성 실패") }
let outURL = URL(fileURLWithPath: spec.out)
var quality = 0.9
while true {
    guard let dest = CGImageDestinationCreateWithURL(outURL as CFURL, "public.jpeg" as CFString, 1, nil) else { fail("저장 실패") }
    CGImageDestinationAddImage(dest, outImg, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
    CGImageDestinationFinalize(dest)
    let bytes = (try? FileManager.default.attributesOfItem(atPath: spec.out)[.size] as? Int) ?? 0
    if bytes <= 1_900_000 || quality < 0.5 { break }
    quality -= 0.08
}

func r4(_ r: CGRect) -> [Double] { [Double(r.minX), Double(r.minY), Double(r.maxX), Double(r.maxY)] }
let rep: [String: Any] = [
    "face_source": chosen.face.map { r4($0.box) } ?? [],
    "t": chosen.t, "face_found": chosen.face != nil, "face": r4(faceCanvas), "head": r4(headCanvas),
    "face_quality": chosen.face?.quality ?? 0, "eyes": chosen.face?.eyes ?? 0, "face_luma": faceLuma,
    "sharpness": chosen.sharp / maxSharp, "upscale": scale * Double(spec.W) / W, "source_px_per_canvas_px": 1 / scale,
    "font_size": size, "fit": fit, "text": textRects, "badge": r4(badgeRect), "emoji": r4(emojiRect),
    "box": r4(box), "blurred_others": blurredOthers, "identified_by_reference": identified, "others": chosen.others.map { r4($0.box) }, "candidates": ranked.map { ["t": $0.0.t, "score": $0.1, "face": $0.0.face != nil] },
    "width": spec.W, "height": spec.H, "jpeg_quality": quality,
]
let repData = try! JSONSerialization.data(withJSONObject: rep, options: [.prettyPrinted])
try! repData.write(to: URL(fileURLWithPath: spec.report))
print(spec.out)
