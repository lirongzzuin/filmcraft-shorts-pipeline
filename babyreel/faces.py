"""얼굴 보호: 확대·크롭·펀치줌 때문에 아기(사람) 얼굴이 화면 밖으로 잘리지 않게 구도를 보정한다.

docs/FEEDBACK.md 2026-10-17 "크롭될 때 아기 얼굴이 잘리려는 느낌".
얼굴 위치는 macOS 내장 Vision(scripts/detect_faces.swift)으로 찾는다. 무료·오프라인.

원칙
- plan 이 정한 구도(zoom/focus/focus_y)에서 **이미 일부라도 보이는 얼굴만** 지킨다.
  일부러 화면 밖으로 뺀 얼굴(타인)·crop 으로 가린 영역의 얼굴은 다시 끌어들이지 않는다 (docs/POLICY.md)
- 얼굴 + 머리 위 여유(headroom)가 화면에 들어오도록 먼저 위치를 옮기고, 그래도 안 되면 확대를 줄인다
- 펀치줌·켄번즈는 얼굴 중심을 기준으로 확대한다 (화면 가운데 기준으로 커지면 가장자리 얼굴이 밀려 나간다)
- 끄려면 clip 의 "face_guard": false
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import config

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "detect_faces.swift"
BIN = ROOT / "data" / "bin" / "detect_faces"
CACHE = ROOT / "data" / "faces.json"

# 얼굴 박스(눈썹~턱) 기준 여유: 위는 이마·정수리, 옆은 볼·귀
HEAD_TOP, CHIN, SIDE = 0.65, 0.15, 0.2
MIN_VISIBLE = 0.25  # 계획한 구도에서 이 비율 이상 보이는 얼굴만 지킨다
# plan 의 zoom 이 이 이상이면 '일부러 확대한 장면'(가리기·강조)으로 보고 확대를 줄이지 않는다 (위치만 보정).
# 실제 사례: 1.75배로 담요의 병원 이름을 화면 밖으로 뺀 장면 → 확대를 줄이면 병원 이름이 드러남 (2026-10-17)
KEEP_ZOOM_FROM = 1.5

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1 (0~1, 왼쪽 위 원점, 화면 방향 기준)


def _binary() -> list[str]:
    """swift 스크립트를 한 번 컴파일해 둔다 (매번 해석 실행하면 클립당 ~8초)."""
    try:
        if not BIN.exists() or BIN.stat().st_mtime < SCRIPT.stat().st_mtime:
            BIN.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["swiftc", "-O", "-suppress-warnings", str(SCRIPT), "-o", str(BIN)], check=True,
                           capture_output=True)
        return [str(BIN)]
    except (OSError, subprocess.CalledProcessError):
        return ["swift", "-suppress-warnings", str(SCRIPT)]


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _round(x):
    if isinstance(x, float):
        return round(x, 4)  # 0.0001 = 1440px 화면에서 0.15px
    if isinstance(x, (list, tuple)):
        return [_round(i) for i in x]
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    return x


def _save_cache(cache: dict) -> None:
    """용량 최소화: 지금 버전(v3) 키만 남기고, 좌표는 소수 4자리, 공백 없이."""
    keep = {k: _round(v) for k, v in cache.items() if k.startswith("v3|")}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(keep, separators=(",", ":")))


def scan(src: Path, start: float, end: float, samples: int = 6) -> dict[str, list[Rect]]:
    """src 의 start~end 구간에서 찾은 {"faces": 얼굴 박스, "texts": 글자 박스} (캐시).
    실패하면 빈 목록 → 보정하지 않는다."""
    empty: dict = {"faces": [], "texts": [], "frames": []}
    src = Path(src).resolve()
    try:
        key = f"v3|{src}|{src.stat().st_mtime:.0f}|{start:.2f}|{end:.2f}|{samples}"
    except OSError:
        return empty
    cache = _load_cache()
    if key in cache:
        c = cache[key]
        return {"faces": [tuple(b) for b in c["faces"]], "texts": [tuple(b) for b in c["texts"]],
                "frames": c.get("frames", [])}
    try:
        out = subprocess.run(_binary() + [str(src), f"{start}", f"{end}", str(samples)], check=True,
                             capture_output=True, text=True, timeout=180).stdout
        found = json.loads(out.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return empty
    res = {
        "faces": [tuple(b) for b in found.get("faces", []) if (b[2] - b[0]) > 0.03 and (b[3] - b[1]) > 0.02],
        "texts": [tuple(b) for b in found.get("texts", []) if (b[2] - b[0]) > 0.02],
        "frames": [{"t": f["t"], "luma": f.get("luma", 0.5),
                    "faces": [b for b in f.get("faces", []) if (b[2] - b[0]) > 0.03]} for f in found.get("frames", [])],
    }
    cache[key] = res
    _save_cache(cache)
    return res


def detect(src: Path, start: float, end: float, samples: int = 6) -> list[Rect]:
    return scan(src, start, end, samples)["faces"]


def shown(b: Rect, iw: float, ih: float, W: float, H: float, scale: float, px: float, py: float) -> float:
    """박스 b 가 화면에 보이는 비율 (0~1)."""
    s = _screen(b, iw * scale / 100, ih * scale / 100, px, py)
    area = (s[2] - s[0]) * (s[3] - s[1])
    ix = max(0.0, min(s[2], W) - max(s[0], 0.0))
    iy = max(0.0, min(s[3], H) - max(s[1], 0.0))
    return ix * iy / area if area > 0 else 0.0


def reveals(hidden: list[Rect], iw: float, ih: float, W: float, H: float,
            before: tuple[float, float, float], after: tuple[float, float, float], crop: list[float] | None = None,
            threshold: float = 0.15) -> bool:
    """보정 후 구도가, 계획한 구도에서 가려져 있던 글자(이름표·병원명)·타인 얼굴을 새로 드러내는가."""
    l, t, r, btm = [(v or 0) / 100 for v in (crop or [0, 0, 0, 0])]
    for b in hidden:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if not (l <= cx <= 1 - r and t <= cy <= 1 - btm):
            continue  # crop 으로 잘려 나가는 영역은 어차피 안 보인다
        if shown(b, iw, ih, W, H, *after) >= threshold and shown(b, iw, ih, W, H, *before) < threshold:
            return True
    return False


def head(b: Rect) -> Rect:
    """얼굴 박스 → 머리 전체(이마·정수리 여유 포함)."""
    w, h = b[2] - b[0], b[3] - b[1]
    return (max(0.0, b[0] - SIDE * w), max(0.0, b[1] - HEAD_TOP * h),
            min(1.0, b[2] + SIDE * w), min(1.0, b[3] + CHIN * h))


def _screen(b: Rect, fw: float, fh: float, px: float, py: float) -> Rect:
    return (px - fw / 2 + b[0] * fw, py - fh / 2 + b[1] * fh, px - fw / 2 + b[2] * fw, py - fh / 2 + b[3] * fh)


def visible(boxes: list[Rect], iw: float, ih: float, W: float, H: float, scale: float, px: float, py: float,
            crop: list[float] | None = None) -> list[Rect]:
    """계획한 구도에서 일부라도(MIN_VISIBLE) 보이는 얼굴. crop(%) 으로 가린 영역의 얼굴은 뺀다."""
    l, t, r, btm = [(v or 0) / 100 for v in (crop or [0, 0, 0, 0])]
    keep = []
    for b in boxes:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if not (l <= cx <= 1 - r and t <= cy <= 1 - btm):
            continue
        if shown(b, iw, ih, W, H, scale, px, py) >= MIN_VISIBLE:
            keep.append(b)
    return keep


def covers(iw: float, ih: float, W: float, H: float, frame: tuple[float, float, float], crop: list[float] | None) -> bool:
    """crop 을 뺀 남은 그림이 화면을 꽉 채우는가 (쇼츠 꽉 채움에서 빈 띠가 안 생기게)."""
    scale, px, py = frame
    fw, fh = iw * scale / 100, ih * scale / 100
    l, t, r, b = [(v or 0) / 100 for v in (crop or [0, 0, 0, 0])]
    return (px - fw / 2 + l * fw <= 0.5 and px + fw / 2 - r * fw >= W - 0.5
            and py - fh / 2 + t * fh <= 0.5 and py + fh / 2 - b * fh >= H - 0.5)


def guarded(found: dict[str, list[Rect]], iw: float, ih: float, W: float, H: float, scale: float, min_scale: float,
            px: float, py: float, cover: bool, crop: list[float] | None = None, zoom: float = 1.0
            ) -> tuple[Rect | None, list[Rect], tuple[float, float, float], str]:
    """얼굴 보호 결정: (보호 영역, 숨겨야 할 것들, 새 (scale, px, py), 메모).

    1) 보이는 얼굴이 다 들어오게 위치·확대 보정 (plan zoom 이 KEEP_ZOOM_FROM 이상이면 확대는 줄이지 않고 위치만)
    2) 그 결과가 가려 둔 글자·타인 얼굴을 드러내면 → 확대는 그대로 두고 위치만 보정
    3) 그래도 드러나면 → 보정하지 않음 (기획한 구도 유지, 경고)
    가리는 것은 crop 이 확실하다 (원본 기준이라 어떤 보정에도 드러나지 않음).
    """
    if zoom >= KEEP_ZOOM_FROM:
        min_scale = scale
    before = (scale, px, py)
    vis = visible(found["faces"], iw, ih, W, H, scale, px, py, crop)
    u = union(vis)
    hidden = [b for b in found["faces"] if b not in vis] + \
        [b for b in found["texts"] if shown(b, iw, ih, W, H, *before) < 0.15]
    if not u:
        return None, hidden, before, ""
    need_cover = cover and covers(iw, ih, W, H, before, crop)  # 기획 구도가 꽉 찼으면 보정 후에도 꽉 차게

    def ok(frame: tuple[float, float, float]) -> bool:
        return not reveals(hidden, iw, ih, W, H, before, frame, crop) and \
            (not need_cover or covers(iw, ih, W, H, frame, crop))

    after = fit(u, iw, ih, W, H, scale, min_scale, px, py, cover)
    if ok(after):
        return u, hidden, after, ""
    after = fit(u, iw, ih, W, H, scale, scale, px, py, cover)
    if ok(after):
        return u, hidden, after, "확대는 유지(가려 둔 글자·얼굴·crop 보호), 위치만 보정"
    return None, hidden, before, "보정 안 함: 보정하면 가려 둔 글자·얼굴이 드러남 → zoom/focus 를 직접 조정하세요"


def union(boxes: list[Rect]) -> Rect | None:
    if not boxes:
        return None
    hs = [head(b) for b in boxes]
    return (min(b[0] for b in hs), min(b[1] for b in hs), max(b[2] for b in hs), max(b[3] for b in hs))


def _axis(lo: float, hi: float, p: float, size: float, frame: float, cover: bool) -> float:
    """한 축: 화면상의 보호 영역 [lo, hi] 가 [0, frame] 에 들어가도록 중심 p 를 옮긴다."""
    if hi - lo > frame:
        p += frame / 2 - (lo + hi) / 2  # 다 못 넣으면 가운데 정렬
    elif lo < 0:
        p -= lo
    elif hi > frame:
        p -= hi - frame
    if size <= frame:
        return frame / 2 if not cover else p
    return min(size / 2, max(frame - size / 2, p)) if cover else p


def fit(u: Rect, iw: float, ih: float, W: float, H: float, scale: float, min_scale: float, px: float,
        py: float, cover: bool) -> tuple[float, float, float]:
    """보호 영역 u 가 화면에 들어오는 (scale, px, py). 너무 크면 min_scale 까지 확대를 줄인다."""
    uw, uh = (u[2] - u[0]) * iw * scale / 100, (u[3] - u[1]) * ih * scale / 100
    k = min(1.0, W / uw if uw else 1.0, H / uh if uh else 1.0)
    if k < 1:
        new = max(min_scale, scale * k)
        # 확대를 줄일 때 보호 영역 중심이 화면에서 같은 자리에 머물도록
        cx = px - iw * scale / 200 + (u[0] + u[2]) / 2 * iw * scale / 100
        cy = py - ih * scale / 200 + (u[1] + u[3]) / 2 * ih * scale / 100
        px, py = cx - (cx - px) * new / scale, cy - (cy - py) * new / scale
        scale = new
    fw, fh = iw * scale / 100, ih * scale / 100
    s = _screen(u, fw, fh, px, py)
    px = _axis(s[0], s[2], px, fw, W, cover)
    py = _axis(s[1], s[3], py, fh, H, cover)
    return scale, px, py


def zoom_about(u: Rect, iw: float, ih: float, W: float, H: float, scale: float, px: float, py: float,
               k: float, cover: bool) -> tuple[float, float, float]:
    """scale·(px,py) 에서 k 배 확대(펀치줌·켄번즈)할 때의 (scale, px, py): 얼굴 중심 기준, 얼굴이 안 잘리게."""
    fw, fh = iw * scale / 100, ih * scale / 100
    s = _screen(u, fw, fh, px, py)
    k = max(1.0, min(k, W / max(1.0, s[2] - s[0]), H / max(1.0, s[3] - s[1]))) if k > 1 else k
    fx, fy = (s[0] + s[2]) / 2, (s[1] + s[3]) / 2
    nx, ny = fx - (fx - px) * k, fy - (fy - py) * k
    return fit(u, iw, ih, W, H, scale * k, scale * k, nx, ny, cover)


def enabled() -> bool:
    return bool(config.load().get("style", {}).get("face_guard", True))


def mean_luma(found: dict) -> float | None:
    """샘플 프레임 평균 밝기 (0~1). 없으면 None."""
    vals = [f["luma"] for f in found.get("frames", []) if "luma" in f]
    return sum(vals) / len(vals) if vals else None


def track_near(found: dict, hint: tuple[float, float], max_jump: float = 0.25) -> list[tuple[float, Rect]]:
    """hint(x, y: 0~1) 가까이에 있는 얼굴을 샘플마다 따라간다 → [(원본 시각, 얼굴 박스)].
    못 찾은 프레임은 직전 위치를 쓴다 (블러가 한 프레임이라도 빠지면 얼굴이 드러나므로)."""
    out: list[tuple[float, Rect]] = []
    cx, cy = hint
    last: Rect | None = None
    for f in sorted(found.get("frames", []), key=lambda f: f["t"]):
        best, dist = None, 9.0
        for b in f.get("faces", []):
            d = (((b[0] + b[2]) / 2 - cx) ** 2 + ((b[1] + b[3]) / 2 - cy) ** 2) ** 0.5
            if d < dist:
                best, dist = tuple(b), d
        if best and dist <= max_jump:
            last = best
            cx, cy = (best[0] + best[2]) / 2, (best[1] + best[3]) / 2
        if last:
            out.append((f["t"], last))
    return out


def without(found: dict, hints: list[tuple[float, float]], radius: float = 0.15) -> dict:
    """hints(흐리게 할 얼굴 위치) 근처 얼굴을 뺀 결과. blur_faces 의 얼굴을 얼굴 보호 대상에서 제외할 때."""
    tracks = [track_near(found, h) for h in hints]
    centers = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for tr in tracks for _, b in tr] + list(hints)

    def near(b) -> bool:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return any(((cx - x) ** 2 + (cy - y) ** 2) ** 0.5 < radius for x, y in centers)

    return {**found, "faces": [b for b in found["faces"] if not near(b)],
            "frames": [{**f, "faces": [b for b in f.get("faces", []) if not near(b)]} for f in found.get("frames", [])]}
