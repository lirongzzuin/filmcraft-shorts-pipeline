"""다른 채널 프로필로 세로 영상 한 편을 만드는 진입점 (외부 렌더 작업자가 부른다).

    ./br render <작업 폴더> --profile <profile.json> [--no-export]

작업 폴더:
  plan.json      이 패키지의 plan 스키마 (babyreel/plan.py). 클립 src 는 절대 경로
  profile.json   channel.toml 과 같은 구조의 채널 프로필 (style·short·audio·series …)
출력 (작업 폴더/out):
  video.mp4      profile [short] 의 width×height
  cover.jpg      커버(대표 화면) — plan.thumbnail 의 클립·시각
  preview.png    확인용 컨택트시트
  render.json    결과 요약 (길이, 구간, QA)

./br build 와 달리 에피소드 부수 효과가 없다: library 사용 표시·연재 기록·업로드 일정·썸네일 A/B 를 만들지 않는다.
기존 채널(config/channel.toml)의 빌드는 이 파일과 무관하게 그대로다.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from . import config
from .fc import FilmCraft, rel
from .media import probe, sheet


class RenderError(RuntimeError):
    pass


def render(job_dir: Path, profile: Path, export: bool = True, verbose: bool = False) -> dict:
    from .build import Builder, _preview_times
    from .plan import Plan, validate

    job_dir = job_dir.resolve()
    config.use_profile(profile.resolve())
    try:
        plan = Plan.load(job_dir)
        report = validate(plan)
        if not report.ok:
            raise RenderError("plan 오류: " + " / ".join(report.errors))
        plan.fill_ends()
        cfg = config.load()
        s = cfg["short"]
        out = job_dir / "out"
        out.mkdir(parents=True, exist_ok=True)
        started = time.time()
        with FilmCraft(verbose=verbose) as fc:
            b = Builder(fc, plan, job_dir, name="short")
            b.hdr = any(probe(config.resolve(c.src))["hdr"] for c in plan.clips)
            b.frame_size = (s["width"], s["height"])
            res = b.build_short()
            seq, total = res["sequence"], res["duration"]
            fc.run("sequence.open", item=seq)
            # 커버: thumbnail.clip 의 시작 + thumbnail.at (타임라인 초)
            seg = next((x for x in res["segments"] if x.clip_index == plan.thumbnail.clip), res["segments"][0])
            cover_t = min(total - 0.05, max(0.0, seg.start + plan.thumbnail.at))
            cover_png = out / "cover.png"
            fc.render_frame(cover_t, cover_png)
            preview = out / "preview.png"
            sheet(fc, seq, total, s["width"], s["height"], preview, times=_preview_times(plan, res))
            video = out / "video.mp4"
            if export:
                b.export(seq, video, "Social Vertical 1080×1920", s["video_kbps"], s["audio_kbps"], s["fps"])
            fc.run("file.saveAs", path=str(job_dir / "project.fcproj"))

        cover = _to_jpeg(cover_png, out / "cover.jpg")
        problems: list[str] = []
        if export:
            from .qa import check

            has_sound = bool(plan.music) or any(probe(config.resolve(c.src))["has_audio"] and not c.mute for c in plan.clips)
            problems = [p for p in check(video, "short", music=bool(plan.music))
                        if has_sound or not p.startswith("라우드니스")]  # 소리 없는 영상(앱에서 음악 추가)은 정상
        manifest = {
            "video": rel(video) if export else None,
            "cover": rel(cover),
            "preview": rel(preview),
            "width": s["width"],
            "height": s["height"],
            "duration": round(total, 2),
            "segments": [asdict(x) for x in res["segments"]],
            "qa": problems,
            "warnings": report.warnings,
            "render_seconds": round(time.time() - started, 1),
        }
        (job_dir / "render.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest
    finally:
        config.use_profile(None)


def _to_jpeg(png: Path, jpg: Path) -> Path:
    """sips(macOS) 로 JPEG 변환. 없으면 PNG 그대로."""
    import shutil
    import subprocess

    if shutil.which("sips"):
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "90", str(png), "--out", str(jpg)],
                       capture_output=True, check=False)
        if jpg.exists():
            return jpg
    return png
