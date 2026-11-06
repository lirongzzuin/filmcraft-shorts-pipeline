"""썸네일 (롱폼 1280x720 + A/B 변형 2장, 쇼츠 커버 1080x1920). scripts/thumbnail.swift 로 그린다.

설계 원칙(구도·글자 수·색·배치)은 비공개 스타일 가이드에 있다. [공개 사본]
YouTube '테스트 및 비교'(최대 3장)용으로 롱폼은 A·B·C 3장.
글자 크기는 Core Text 로 실제 폭을 재서 정한다 (가로로 눌러 맞추지 않음). 프레임은 고른 순간 ±1초에서
얼굴 품질·눈 뜸·웃음·선명도로 가장 좋은 것을 자동으로 고른다. 결과 사각형은 *_report.json → QA 가 검사.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import config
from .config import ROOT
from .media import probe
from .plan import Plan

SCRIPT = ROOT / "scripts" / "thumbnail.swift"
BIN = ROOT / "data" / "bin" / "thumbnail"
FONT = ROOT / "assets" / "fonts" / "Jua-Regular.ttf"

LONG = {"W": 1280, "H": 720, "layout": "long", "faceFrac": 0.46, "minFont": 80, "maxFont": 150}
COVER = {"W": 1080, "H": 1920, "layout": "cover", "faceFrac": 0.30, "minFont": 96, "maxFont": 190}
# A/B/C: A = 가장 좋은 순간, B = 좌우 바꾼 구도(이모지 없이 표정 집중), C = 다른 순간 (0.4초 이상 떨어진 두 번째 후보)
VARIANTS = {"a": {"pick": 0, "zoom": 1.0, "emoji": True, "mirror": False},
            "b": {"pick": 0, "zoom": 1.15, "emoji": False, "mirror": True},  # 좌우 바꾼 구도, 표정에 집중
            "c": {"pick": 1, "zoom": 1.0, "emoji": True, "mirror": False}}  # 다른 순간
FILES = {"a": "thumbnail.jpg", "b": "thumbnail_b.jpg", "c": "thumbnail_c.jpg"}
TEXT_COLORS = ["#FFFFFF", "#FFE14D"]

# QA 기준
MIN_FACE = {"long": 0.22, "cover": 0.14}  # 얼굴 높이 / 화면 높이 (이보다 작으면 썸네일에서 안 보임)
MIN_QUALITY = 0.25  # Vision 얼굴 품질 (흔들림·가림·어두움)


def _binary() -> list[str]:
    try:
        if not BIN.exists() or BIN.stat().st_mtime < SCRIPT.stat().st_mtime:
            BIN.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["swiftc", "-O", "-suppress-warnings", str(SCRIPT), "-o", str(BIN)], check=True,
                           capture_output=True)
        return [str(BIN)]
    except (OSError, subprocess.CalledProcessError):
        return ["swift", "-suppress-warnings", str(SCRIPT)]


IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic"}


def cast_photos(nick: str) -> list[str]:
    """assets/cast/<이름>/ 의 사진 (얼굴 기준). 아이는 channel.toml [[children]] ref, 부모는 assets/cast/엄마·아빠."""
    ref = next((c.get("ref") for c in config.children() if c["nick"] == nick), None) or f"assets/cast/{nick}"
    d = config.resolve(ref)
    return sorted(str(p) for p in d.glob("*") if p.suffix.lower() in IMAGE_EXT)[:6] if d.is_dir() else []


def spec_for(plan: Plan, kind: str, out: Path, variant: str = "a") -> dict:
    """kind: long | cover. plan 은 쇼츠 2부면 plan.part('short2') 보기를 넘긴다."""
    th = plan.thumbnail
    clip = plan.clips[th.clip]
    src = config.resolve(clip.src)
    info = probe(src)
    base = LONG if kind == "long" else COVER
    v = VARIANTS[variant] if kind == "long" else VARIANTS["a"]
    lines = [x for x in (th.text or plan.title_lines) if x.strip()][:2]
    badge = f"육아일지 #{plan.episode_no}" if plan.episode_no else config.series_label(plan.series)  # 번외편은 '번외편'
    return {
        "src": str(src), "isPhoto": bool(info["still"]), "t": float(th.at), "window": 1.0, "samples": 9,
        "crop": list(clip.crop), "blur": [list(b) for b in clip.blur_faces],
        "blurAreas": [a.at(float(th.at)) for a in clip.blur_areas], "faceHints": [list(f) for f in th.faces],
        **base, "faceFrac": base["faceFrac"] * (th.zoom or 1.0) * v["zoom"],
        "lines": lines, "font": str(FONT), "colors": TEXT_COLORS[-len(lines):] if len(lines) == 1 else TEXT_COLORS,
        "stroke": "#111111", "badge": badge, "accent": config.load()["style"].get("accent_color", "#ff7a3d"),
        "emoji": (th.stickers[0] if th.stickers else "") if v["emoji"] else "", "blurOthers": True,
        "pick": v["pick"], "mirror": v["mirror"], "out": str(out), "report": str(report_path(out)),
        # 얼굴이 여럿이면 기준 사진과 닮은 얼굴을 주인공으로 (thumbnail.faces 가 있으면 그게 우선)
        "subjectRefs": [p for nick in (plan.cast or ["첫째"])[:1] for p in cast_photos(nick)],
        "otherRefs": [p for nick in config.load().get("family", {}).get("parents", ["엄마", "아빠"]) for p in cast_photos(nick)],
    }


def report_path(out: Path) -> Path:
    return out.with_name(out.stem + "_report.json")


def render(plan: Plan, kind: str, out: Path, variant: str = "a") -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    spec = spec_for(plan, kind, out, variant)
    spec_file = out.with_name(out.stem + "_spec.json")
    spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    rep = _run(spec_file, out)
    if not rep["face_found"] and not spec["isPhoto"] and not plan.thumbnail.no_face:
        # 고른 순간 ±1초에 얼굴이 없으면 ±3초까지 넓혀서 다시 (기획한 순간과 가까운 프레임이 여전히 우선)
        spec.update(window=3.0, samples=19)
        spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
        rep = _run(spec_file, out)
    rep["no_face_ok"] = bool(plan.thumbnail.no_face)
    report_path(out).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return rep


def _run(spec_file: Path, out: Path) -> dict:
    r = subprocess.run(_binary() + [str(spec_file)], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"썸네일 생성 실패 ({out.name}): {r.stderr.strip()[-300:]}")
    return json.loads(report_path(out).read_text(encoding="utf-8"))


def render_long(plan: Plan, out_dir: Path) -> list[Path]:
    """롱폼 썸네일 A(기본 thumbnail.jpg) + B·C (YouTube 테스트 및 비교용)."""
    made = []
    for v, name in FILES.items():
        render(plan, "long", out_dir / name, v)
        made.append(out_dir / name)
    return made


def _overlap(a: list[float], b: list[float]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


# Vision 얼굴 상자는 넉넉해서(볼·이마까지) 가까이 찍은 얼굴은 화면 끝을 조금 넘는다 → 얼굴 크기의 12% 넘게 나가야 잘림
CUT_TOLERANCE = 0.12


def overflow(box: list[float], W: float, H: float) -> float:
    """얼굴 상자가 화면 밖으로 나간 정도 (얼굴 폭·높이 대비, 가로+세로)."""
    w, h = max(1e-6, box[2] - box[0]), max(1e-6, box[3] - box[1])
    return max(0.0, -box[0], box[2] - W) / w + max(0.0, -box[1], box[3] - H) / h


def problems(image: Path) -> tuple[list[str], list[str]]:
    """(오류, 경고): 리포트로 겹침·잘림·얼굴 크기·글자 맞춤·규격을 검사한다."""
    errors, warnings = [], []
    rep_path = report_path(image)
    if not image.exists() or not rep_path.exists():
        return [f"{image.name}: 썸네일이 없습니다 (./br thumbnail)"], []
    rep = json.loads(rep_path.read_text(encoding="utf-8"))
    W, H = rep["width"], rep["height"]
    kind = "long" if W > H else "cover"
    tag = image.name
    from .media import probe as _probe

    info = _probe(image)
    if (info["width"], info["height"]) != (W, H):
        errors.append(f"{tag}: 크기 {info['width']}x{info['height']} ≠ {W}x{H}")
    if image.stat().st_size > 2_000_000:
        errors.append(f"{tag}: 파일 {image.stat().st_size / 1e6:.1f}MB > 2MB (YouTube 한도)")
    if not rep["fit"]:
        errors.append(f"{tag}: 글자가 들어갈 자리가 모자랍니다 (글꼴 {rep['font_size']:.0f}px 최소) → thumbnail.text 를 줄당 6자 안팎으로")
    if not rep["face_found"]:
        msg = f"{tag}: 얼굴을 못 찾은 프레임입니다 → thumbnail.at 을 얼굴이 잘 보이는 순간으로 (또는 thumbnail.faces)"
        (warnings if rep.get("no_face_ok") else errors).append(msg + ("" if not rep.get("no_face_ok") else " (no_face 허용)"))
    else:
        face = rep["face"]
        fh = (face[3] - face[1]) / H
        if fh < MIN_FACE[kind]:
            warnings.append(f"{tag}: 얼굴이 작습니다 (화면 높이의 {fh:.0%}) → thumbnail.zoom 을 올리거나 더 가까운 장면")
        if overflow(face, W, H) > CUT_TOLERANCE:
            errors.append(f"{tag}: 얼굴이 화면 밖으로 잘립니다")
        elif rep.get("face_source") and overflow(rep["face_source"], 1, 1) > CUT_TOLERANCE:
            errors.append(f"{tag}: 고른 순간의 원본에서 얼굴이 화면 끝에 걸려 잘립니다 → thumbnail.at 을 얼굴이 가운데 있는 순간으로")
        if rep["face_quality"] < MIN_QUALITY:
            warnings.append(f"{tag}: 얼굴 품질이 낮습니다 ({rep['face_quality']:.2f}: 흔들림·가림·어두움) → 다른 순간 추천")
        if rep["eyes"] < 0.12:
            warnings.append(f"{tag}: 눈을 감은 프레임 같습니다 → 잠든 장면이 의도가 아니면 다른 순간")
        for i, t in enumerate(rep["text"]):
            if _overlap(t, face) > 0.02 * (face[2] - face[0]) * (face[3] - face[1]):
                errors.append(f"{tag}: 글자 {i + 1}줄이 얼굴을 가립니다")
        e = rep["emoji"]
        if e[2] > e[0] and _overlap(e, face) > 0:
            errors.append(f"{tag}: 이모지가 얼굴을 가립니다")
    for i, t in enumerate(rep["text"]):
        if t[0] < 0 or t[1] < 0 or t[2] > W or t[3] > H:
            errors.append(f"{tag}: 글자 {i + 1}줄이 화면 밖으로 잘립니다")
        if kind == "long" and t[2] > W * 0.82 and t[3] > H * 0.84:
            warnings.append(f"{tag}: 글자가 오른쪽 아래(재생 시간 표시 자리)에 걸립니다")
    return errors, warnings


def outputs(out_dir: Path) -> list[Path]:
    names = list(FILES.values()) + ["cover.jpg", "cover2.jpg"]
    return [out_dir / n for n in names if (out_dir / n).exists()]
