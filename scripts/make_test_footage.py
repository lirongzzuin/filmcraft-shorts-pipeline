"""파이프라인 검증용 가짜 '폰 촬영본'을 FilmCraft 로 만든다.

실제 아기 영상 없이도 ingest → build → package 전체를 돌려볼 수 있게
세로(1080x1920)·가로(1920x1080) 테스트 클립을 tests/footage/ 에 만든다.

    python3 scripts/make_test_footage.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from babyreel.fc import ROOT, FilmCraft, seconds_to_ticks  # noqa: E402

OUT = ROOT / "tests" / "footage"

CLIPS = [
    # name, w, h, 배경색, 공 색, 길이(초)
    ("test_portrait_01.mp4", 1080, 1920, "#f6d6c8", "#e07a5f", 8),
    ("test_portrait_02.mp4", 1080, 1920, "#d8e2dc", "#3d5a80", 6),
    ("test_landscape_01.mp4", 1920, 1080, "#fde2b3", "#81b29a", 7),
]


def make(fc: FilmCraft, name: str, w: int, h: int, bg: str, ball: str, secs: float) -> None:
    fc.run("file.newProject")
    fc.run("file.newSequence", name=name, width=w, height=h, fps=30)
    matte = fc.run("file.newColorMatte", color=bg, seconds=secs)["item"]
    fc.run("timeline.place", item=matte, track="V1", seconds=0, duration=seconds_to_ticks(secs))
    tone = fc.run("file.newBarsAndTone", seconds=secs)["item"]
    placed = fc.run("timeline.place", item=tone, track="A1", seconds=0, duration=seconds_to_ticks(secs), video=False)
    fc.run("clip.audioGain", clips=placed["clips"], mode="set", db=-30)

    shape = fc.run("graphics.newShape", shape="ellipse", size=[w * 0.3, w * 0.3], seconds=secs, track=1, time=0)
    sclip = shape["clip"]
    fc.run("graphics.set", clip=sclip, layer=0, props={"fill_color": ball})
    # 공이 화면을 가로지르게 움직여서 크롭/프레이밍 결과를 눈으로 확인할 수 있게 한다.
    for t, x in [(0, w * 0.2), (secs, w * 0.8)]:
        fc.run("effects.setParam", clip=sclip, effect="motion", param="position",
               value=[x, h * 0.5], time=seconds_to_ticks(t), keyframe=True)

    label = f"{name.removesuffix('.mp4')}  {w}x{h}"
    fc.run("graphics.newText", text=label, position=[w * 0.05, h * 0.1], size=w * 0.045, seconds=secs, track=2, time=0)

    OUT.mkdir(parents=True, exist_ok=True)
    fc.run("file.exportMedia", path=str(OUT / name), format="h264", wait=True)
    print(f"✓ {OUT / name}")


def main() -> None:
    with FilmCraft() as fc:
        for spec in CLIPS:
            make(fc, *spec)


if __name__ == "__main__":
    main()
