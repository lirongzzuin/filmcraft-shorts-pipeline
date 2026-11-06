"""흔들림 보정 (FilmCraft Warp Stabilizer, 2026-10-29 화질 개선).

손에 들고 찍은 클립만 골라 살짝 보정한다:
- 판단: 그 구간을 540px 로 빠르게 내보내 프레임 사이 이동량(위상 상관)을 잰다. 평균 이동이 SHAKE_PX 이상이면 보정
  (실측: 걸으면서 안고 찍음 5.7 · 바닥 아기 따라 찍음 3.2~3.9 → 보정 / 가만히 1.0~1.5 · 삼각대 0 → 그대로)
- 설정: 최대 확대 110%(기본 150% 는 머리가 잘림), 부드러움 30%, 위치·크기·회전(왜곡 없는 방식)
  → 실측 흔들림 -60%, 아기 머리 안 잘림
- 하지 않는 클립: 사진·정지 화면, 가림(covers)·잘라내기(crop)·얼굴 흐림(blur_faces)·위치 흐림(blur_areas)이 있는 클립
  (보정이 화면을 옮기면 가려 둔 영역·흐림 위치가 어긋날 수 있다 → 안전 우선)
결과는 data/shake.json 에 캐시 (같은 구간을 다시 재지 않음).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from . import config
from .config import DATA

CACHE = DATA / "shake.json"
SHAKE_PX = 2.0  # 540px 폭 기준 프레임당 평균 이동(px)
PARAMS = {"max_scale": 110, "smoothness": 30}


def _load() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _bmp(path: Path):
    """PNG → 회색조 numpy 배열 (sips 로 BMP 변환, 표준 라이브러리 + numpy 만)."""
    import numpy as np

    with tempfile.NamedTemporaryFile(suffix=".bmp") as t:
        subprocess.run(["sips", "-s", "format", "bmp", str(path), "--out", t.name], capture_output=True, check=True)
        b = Path(t.name).read_bytes()
    off = int.from_bytes(b[10:14], "little")
    w = int.from_bytes(b[18:22], "little", signed=True)
    h = int.from_bytes(b[22:26], "little", signed=True)
    bpp = int.from_bytes(b[28:30], "little") // 8
    row = (w * bpp + 3) & ~3
    a = np.frombuffer(b[off:off + row * abs(h)], np.uint8).reshape(abs(h), row)[:, : w * bpp].reshape(abs(h), w, bpp)
    return a[..., :3].mean(2)


def _step(a, b) -> float:
    import numpy as np

    A, B = np.fft.fft2(a), np.fft.fft2(b)
    R = A * B.conj()
    R /= np.abs(R) + 1e-9
    r = np.fft.ifft2(R).real
    y, x = np.unravel_index(r.argmax(), r.shape)
    h, w = a.shape
    y, x = (y if y < h / 2 else y - h), (x if x < w / 2 else x - w)
    return float((y * y + x * x) ** 0.5)


def measure(src: Path, start: float, end: float) -> float:
    """프레임당 평균 이동(px, 540 폭 기준). 실패하면 0 (보정 안 함)."""
    key = f"{src}|{start:.2f}|{end:.2f}"
    cache = _load()
    if key in cache:
        return cache[key]
    from .fc import FilmCraft, seconds_to_ticks as T

    value = 0.0
    try:
        import numpy  # noqa: F401  (.venv 에만 있음)

        with tempfile.TemporaryDirectory() as tmp, FilmCraft() as fc:
            item = fc.run("file.import", paths=[str(src)])["items"][0]
            fc.run("file.newSequence", name="shake", width=270, height=480, fps=15, video=1, audio=1)
            clip = fc.run("timeline.place", item=item, track="V1", seconds=0, sourceIn=T(start),
                          duration=T(min(end - start, 6.0)), audio=False)["clips"][0]
            fc.run("clip.fillFrame", clips=[clip])
            fc.run("file.exportMedia", path=str(Path(tmp) / "f.png"), settings={"format": "png"}, wait=True)
            frames = sorted(Path(tmp).glob("*.png"))
            g = [_bmp(f) for f in frames]
            steps = [_step(g[i + 1], g[i]) for i in range(len(g) - 1)]
            # 15fps·270px 로 쟀으므로 30fps·540px 기준으로 환산 (이동량 ×2, 프레임 간격 ÷2)
            value = round(sum(steps) / len(steps), 2) if steps else 0.0
    except Exception as e:  # 측정 실패는 보정 안 함 (빌드는 계속)
        print(f"  ! 흔들림 측정 실패 {src.name}: {e}")
        return 0.0
    cache[key] = value
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    return value


def wanted(clip, info: dict) -> bool:
    """이 클립을 보정할지. clip.stabilize: true/false 로 직접 정할 수 있다 (기본 None = 자동)."""
    mode = config.load().get("style", {}).get("stabilize", "off")  # 키가 없는 프로필(render.py)은 기존 동작
    if mode == "off" or info.get("still") or clip.hold or clip.covers or clip.crop or clip.blur_faces \
            or clip.blur_areas:
        return False
    if clip.stabilize is not None:
        return bool(clip.stabilize)
    if mode == "on":
        return True
    return measure(config.resolve(clip.src), clip.start, clip.end) >= SHAKE_PX


def apply(fc, clip_id: int) -> None:
    fc.run("effects.apply", clips=[clip_id], effect="warp_stabilizer")
    for k, v in PARAMS.items():
        fc.run("effects.setParam", clip=clip_id, effect="warp_stabilizer", param=k, value=v)
