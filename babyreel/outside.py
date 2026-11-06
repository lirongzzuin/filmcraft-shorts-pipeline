"""창밖 풍경 찾기: 집에서 찍은 영상의 창밖(건물·하늘·도로·간판)으로 집 위치가 드러나지 않게 (2026-11-03 사용자 요청).

macOS Vision 장면 분류(scripts/detect_outside.swift)로 화면 3x3 칸마다 창밖처럼 보이는 칸을 찾는다. 무료·오프라인.
위치를 정확히 찾지는 못한다 → 검증기가 '창밖 의심 칸'을 경고하고, plan 을 쓰는 쪽(Claude)이 컨택트시트에서
창문 영역을 보고 clips[].blur_areas 로 흐리게 한다 (빌더가 FilmCraft 가우시안 블러 + 사각 마스크로, 썸네일도 같이).
창밖이 보이지 않거나 위치를 알 수 없는 장면이면 clips[].window_ok: true.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "detect_outside.swift"
BIN = ROOT / "data" / "bin" / "detect_outside"
CACHE = ROOT / "data" / "outside.json"

# 칸 하나에서 이 점수 이상이면 '창밖 의심'. window 자체는 창틀만 보여도 나오므로 바깥 풍경 label 과 함께 본다
STRONG = {"building": 0.3, "skyscraper": 0.25, "cityscape": 0.25, "apartment": 0.3, "house_single": 0.3,
          "street": 0.3, "road": 0.35, "street_sign": 0.3, "tower": 0.3, "bridge": 0.35}
WEAK = {"window": 0.3, "sky": 0.4, "blue_sky": 0.4, "night_sky": 0.4, "balcony": 0.3, "tree": 0.5, "outdoor": 0.5,
        "car": 0.5}
NAMES = {"building": "건물", "skyscraper": "고층 건물", "cityscape": "도시 풍경", "apartment": "아파트",
         "house_single": "주택", "street": "거리", "road": "도로", "street_sign": "표지판", "tower": "탑",
         "bridge": "다리", "window": "창문", "sky": "하늘", "blue_sky": "하늘", "night_sky": "밤하늘",
         "balcony": "발코니", "tree": "나무", "outdoor": "바깥", "car": "차"}
ROWS, COLS = ("위", "가운데", "아래"), ("왼쪽", "가운데", "오른쪽")


def _binary() -> list[str]:
    try:
        if not BIN.exists() or BIN.stat().st_mtime < SCRIPT.stat().st_mtime:
            BIN.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["swiftc", "-O", "-suppress-warnings", str(SCRIPT), "-o", str(BIN)], check=True,
                           capture_output=True)
        return [str(BIN)]
    except (OSError, subprocess.CalledProcessError):
        return ["swift", "-suppress-warnings", str(SCRIPT)]


def _load() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def scan(src: Path, start: float, end: float, samples: int = 3) -> list[dict]:
    """[{"t": 초, "cells": [[열, 행, label, 점수], ...]}] (캐시). 실패하면 []."""
    src = Path(src).resolve()
    try:
        key = f"v1|{src}|{src.stat().st_mtime:.0f}|{start:.2f}|{end:.2f}|{samples}"
    except OSError:
        return []
    cache = _load()
    if key in cache:
        return cache[key]
    try:
        out = subprocess.run(_binary() + [str(src), f"{start}", f"{end}", str(samples)], check=True,
                             capture_output=True, text=True, timeout=180).stdout
        frames = json.loads(out.strip().splitlines()[-1])["frames"]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError):
        return []
    cache[key] = frames
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")))
    return frames


def suspects(frames: list[dict]) -> dict[tuple[int, int], tuple[str, float]]:
    """창밖 의심 칸 {(열, 행): (label, 최고 점수)}. 바깥 풍경 label 하나가 강하거나, 약한 label 이 둘 이상 겹친 칸."""
    best: dict[tuple[int, int], dict[str, float]] = {}
    for f in frames:
        for col, row, label, score in f.get("cells", []):
            cell = best.setdefault((int(col), int(row)), {})
            cell[label] = max(cell.get(label, 0.0), float(score))
    out = {}
    for cell, labels in best.items():
        strong = [(s, l) for l, s in labels.items() if s >= STRONG.get(l, 2)]
        weak = [(s, l) for l, s in labels.items() if s >= WEAK.get(l, 2)]
        if strong or len({NAMES[l] for _, l in weak}) >= 2:  # sky·blue_sky 는 같은 '하늘' 하나로 센다
            s, l = max(strong or weak)
            out[cell] = (l, s)
    return out


def cell_box(col: int, row: int) -> list[float]:
    return [round(col / 3, 3), round(row / 3, 3), round((col + 1) / 3, 3), round((row + 1) / 3, 3)]


def describe(found: dict[tuple[int, int], tuple[str, float]]) -> str:
    """'오른쪽 위(건물 0.42), 가운데 위(하늘 0.51)'"""
    return ", ".join(f"{COLS[c]} {ROWS[r]}({NAMES.get(l, l)} {s:.2f})" for (c, r), (l, s) in sorted(found.items(),
                                                                                                    key=lambda x: -x[1][1]))


def covered(found: dict[tuple[int, int], tuple[str, float]], boxes: list[list[float]]) -> dict:
    """blur_areas 로 이미 가린 칸(칸 면적의 절반 이상)은 뺀다."""
    def overlap(a, b):
        w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        return w * h

    return {cell: v for cell, v in found.items()
            if not any(overlap(cell_box(*cell), b) >= 0.5 / 9 for b in boxes)}
