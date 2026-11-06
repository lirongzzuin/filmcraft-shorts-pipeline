// 창밖 풍경(건물·하늘·도로·간판 등 집 위치를 짐작할 수 있는 것) 찾기. macOS 내장 Vision 장면 분류 (무료·오프라인).
// 화면을 3x3 칸으로 나눠 칸마다 분류한다 → 어느 칸에 창밖이 보이는지 대략 알 수 있다 (babyreel/outside.py).
//
//   swift scripts/detect_outside.swift <파일> <시작초> <끝초> [샘플 수=3]
//
// 출력(JSON): {"frames": [{"t": 초, "cells": [[열, 행, "label", 점수], ...]}]}
//   열·행은 0~2 (왼쪽 위 원점, 회전 메타데이터 적용), 점수는 Vision 신뢰도 0~1. 관련 label 만 0.1 이상.
import AVFoundation
import AppKit
import ImageIO
import Vision

// 'sign'·'structure' 는 화면 글자·가구에도 잘 붙어서 뺀다 (썸네일 샘플에서 0.48 까지 나옴)
let LABELS: Set<String> = ["window", "building", "skyscraper", "cityscape", "apartment", "balcony", "house_single",
                           "street", "road", "street_sign", "sky", "blue_sky", "night_sky", "tree", "outdoor",
                           "tower", "bridge", "car"]

let args = CommandLine.arguments
guard args.count >= 4, let t0 = Double(args[2]), let t1 = Double(args[3]) else {
    FileHandle.standardError.write("usage: detect_outside.swift <file> <start> <end> [samples]\n".data(using: .utf8)!)
    exit(2)
}
let url = URL(fileURLWithPath: args[1])
let samples = args.count > 4 ? max(1, Int(args[4]) ?? 3) : 3

func classify(_ image: CGImage) -> [[Any]] {
    var cells: [[Any]] = []
    let w = image.width / 3, h = image.height / 3
    for row in 0..<3 {
        for col in 0..<3 {
            guard let tile = image.cropping(to: CGRect(x: col * w, y: row * h, width: w, height: h)) else { continue }
            let req = VNClassifyImageRequest()
            try? VNImageRequestHandler(cgImage: tile, options: [:]).perform([req])
            for o in req.results ?? [] where LABELS.contains(o.identifier) && o.confidence >= 0.1 {
                cells.append([col, row, o.identifier, (Double(o.confidence) * 1000).rounded() / 1000])
            }
        }
    }
    return cells
}

var frames: [[String: Any]] = []
let ext = url.pathExtension.lowercased()
if ["jpg", "jpeg", "png", "heic", "heif"].contains(ext) {
    if let src = CGImageSourceCreateWithURL(url as CFURL, nil),
       let img = CGImageSourceCreateThumbnailAtIndex(src, 0, [
           kCGImageSourceCreateThumbnailFromImageAlways: true,
           kCGImageSourceCreateThumbnailWithTransform: true,
           kCGImageSourceThumbnailMaxPixelSize: 1280,
       ] as CFDictionary) {
        frames.append(["t": t0, "cells": classify(img)])
    }
} else {
    let gen = AVAssetImageGenerator(asset: AVURLAsset(url: url))
    gen.appliesPreferredTrackTransform = true
    gen.maximumSize = CGSize(width: 1280, height: 1280)
    for i in 0..<samples {
        let t = samples == 1 ? t0 : t0 + (t1 - t0) * (Double(i) + 0.5) / Double(samples)
        if let img = try? gen.copyCGImage(at: CMTime(seconds: t, preferredTimescale: 600), actualTime: nil) {
            frames.append(["t": (t * 100).rounded() / 100, "cells": classify(img)])
        }
    }
}
let data = try! JSONSerialization.data(withJSONObject: ["frames": frames])
print(String(data: data, encoding: .utf8)!)
