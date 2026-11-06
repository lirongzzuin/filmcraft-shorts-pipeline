"""원본 미디어: 프로브, inbox → library 정리, 컨택트시트."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unicodedata
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from . import storage
from .fc import ROOT, TICKS_PER_SECOND, FilmCraft, cli_path, rel, seconds_to_ticks

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
MEMO_EXT = {".txt", ".md"}
LIBRARY = ROOT / "library"
INDEX = LIBRARY / "index.json"
SHEETS = LIBRARY / "_sheets"


@lru_cache(maxsize=512)
def _probe(path: str, mtime: float) -> dict:
    out = subprocess.run(
        [str(cli_path()), "probe", path, "--compact"], capture_output=True, text=True, check=True
    ).stdout
    raw = json.loads(out)
    v = raw.get("video") or {}
    w, h = v.get("width", 0), v.get("height", 0)
    rotation = int(v.get("rotation") or 0)
    if rotation % 180:
        w, h = h, w
    color = v.get("color") or {}
    fr = v.get("frame_rate") or {}
    return {
        "still": raw.get("kind") == "Still",
        "duration_s": raw["duration"] / TICKS_PER_SECOND,
        "width": w,
        "height": h,
        "orientation": "portrait" if h > w else "landscape" if w > h else "square",
        "fps": round(fr["num"] / fr["den"], 3) if fr.get("den") else None,
        "codec": v.get("codec"),
        "hdr": color.get("transfer") in ("Hlg", "Pq", "AribStdB67", "SmpteSt2084"),
        "has_audio": bool(raw.get("audio_streams")),
        "size_mb": round(raw.get("file_size", 0) / 1e6, 1),
    }


def probe(path: Path) -> dict:
    return _probe(str(path), path.stat().st_mtime)


# --- inbox → library ----------------------------------------------------------------


def _shot_time(path: Path) -> datetime:
    """실제 촬영 시각. 영상/사진 메타데이터(macOS Spotlight)를 먼저 보고, 없으면 파일 시각.

    AirDrop·복사본은 파일 시각이 '복사한 날'이 되므로 메타데이터가 중요하다 (이야기 순서 정렬).
    """
    try:
        raw = subprocess.run(["mdls", "-raw", "-name", "kMDItemContentCreationDate", str(path)],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if raw and raw != "(null)":
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S %z").astimezone().replace(tzinfo=None)
    except (subprocess.SubprocessError, ValueError):
        pass
    st = path.stat()
    ts = min(getattr(st, "st_birthtime", st.st_mtime), st.st_mtime)
    return datetime.fromtimestamp(ts)


def load_index() -> dict[str, dict]:
    return json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}


def save_index(index: dict[str, dict]) -> None:
    LIBRARY.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _fingerprint(path: Path) -> tuple[int, str]:
    """(크기, 앞·뒤 1MB 해시). 같은 영상이 두 번 복사된 것('IMG_1 2.MOV')을 가려낸다."""
    import hashlib

    size = path.stat().st_size
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), 2)
            h.update(f.read(1 << 20))
    return size, h.hexdigest()


def _drop_duplicates(files: list[Path]) -> list[Path]:
    """이번에 들어온 파일 중 내용이 같은 것은 하나만 남기고 library/_duplicates/ 로 옮긴다 (지우지 않음).
    이름이 'IMG_1 2.MOV' 처럼 복사본 표시가 붙은 쪽을 옮긴다."""
    seen: dict[tuple[int, str], Path] = {}
    keep: list[Path] = []
    dup_dir = LIBRARY / "_duplicates"  # 외장 SSD 로의 링크일 수 있음
    # 복사본 표시(' 2', ' 3' …)가 없는 이름을 먼저 보게 정렬
    for f in sorted(files, key=lambda x: (bool(re.search(r" \d+$", x.stem)), x.name)):
        try:
            fp = _fingerprint(f)
        except OSError:
            keep.append(f)
            continue
        if fp in seen:
            dup_dir = storage.library_dir("_duplicates", LIBRARY)
            storage.safe_move(f, dup_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{f.name}")
            print(f"  · 중복(같은 영상) 건너뜀: {f.name} = {seen[fp].name} → library/_duplicates/")
            continue
        seen[fp] = f
        keep.append(f)
    return sorted(keep)


def ingest(inbox: Path, sheets: bool = True) -> list[str]:
    """inbox 의 영상을 library/YYYY-MM/ 로 옮기고 인덱스와 컨택트시트를 만든다."""
    index = load_index()
    added: list[str] = []
    # 메모: 영상과 같은 이름이면 그 영상 전용, 아니면 이번에 들어온 영상 전체에 적용
    memos = sorted(p for p in inbox.rglob("*") if p.is_file() and p.suffix.lower() in MEMO_EXT)
    by_stem = {m.stem: m.read_text(encoding="utf-8").strip() for m in memos}
    files = sorted(p for p in inbox.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXT | IMAGE_EXT)
    media_stems = {f.stem for f in files}
    batch_memo = "\n".join(t for stem, t in by_stem.items() if stem not in media_stems and t)
    files = _drop_duplicates(files)
    for src in files:
        if src.suffix.lower() in (".heic", ".heif"):
            # FilmCraft 는 HEIC 를 못 읽는다 → macOS sips 로 JPEG 변환
            jpg = src.with_suffix(".jpg")
            subprocess.run(["sips", "-s", "format", "jpeg", str(src), "--out", str(jpg)], check=True, capture_output=True)
            src.unlink()
            src = jpg
        dated = re.match(r"(\d{4})-(\d{2})-(\d{2})_", src.name)  # 이미 날짜가 붙은 이름은 그 날짜 (다시 넣은 파일·테스트)
        shot = _shot_time(src)
        if dated and shot.strftime("%Y-%m-%d") != dated.group(0)[:10]:
            shot = datetime(*map(int, dated.groups()), 12)
        name = src.name if dated else f"{shot:%Y-%m-%d}_{src.name}"
        # 외장 SSD(exFAT)는 한글 자모 분리형(NFD) 이름으로 만든 파일을 같은 이름으로 다시 찾지 못한다 → 완성형(NFC)으로 통일
        name = unicodedata.normalize("NFC", name)
        dest = storage.library_dir(f"{shot:%Y-%m}", LIBRARY) / name  # 외장 SSD (babyreel/storage.py)
        if dest.exists():
            print(f"  · 이미 있음, 건너뜀: {rel(dest)}")
            continue
        stem = src.stem
        storage.safe_move(src, dest)  # 검증 복사 뒤에만 원본 삭제 (SSD 가 끊겨도 촬영 원본은 그대로)
        key = rel(dest)
        info = probe(dest)
        index[key] = {**info, "shot_at": shot.isoformat(timespec="seconds"), "used_in": []}
        memo = "\n".join(x for x in (by_stem.get(stem), batch_memo) if x)
        if memo:
            index[key]["memo"] = memo
        added.append(key)
        kind = "사진" if info["still"] else f"{info['duration_s']:.1f}s" + (" HDR" if info["hdr"] else "")
        print(f"  + {key}  {info['width']}x{info['height']} {kind}")
    if memos:
        memo_dir = LIBRARY / "_memos"
        memo_dir.mkdir(parents=True, exist_ok=True)
        for m in memos:
            shutil.move(m, memo_dir / f"{datetime.now():%Y-%m-%d_%H%M%S}_{m.name}")
        print(f"  ✎ 메모 {len(memos)}개 → library/_memos/")
    save_index(index)
    if sheets and added:
        videos = [ROOT / r for r in added if not index[r]["still"]]
        photos = [ROOT / r for r in added if index[r]["still"]]
        if videos:
            contact_sheets(videos)
        if photos:
            for sheet_path, members in photo_sheets(photos).items():
                for label, path in members:
                    index[rel(path)]["sheet"] = rel(sheet_path)
                    index[rel(path)]["label"] = label
            save_index(index)
        added = [r for r in added if not index[r]["still"]] + [r for r in added if index[r]["still"]]
        from . import transcribe

        if transcribe.available():
            for r in (r for r in added if not index[r]["still"]):
                t = transcribe.transcribe(ROOT / r)
                n = len(t["segments"]) if t else 0
                print(f"  ✎ 받아쓰기 {Path(r).name}: {n}문장")
        else:
            print("  · 받아쓰기 생략 (./br 로 실행하면 로컬 Whisper 로 대사를 받아씁니다)")
    return added


# --- 컨택트시트 -------------------------------------------------------------------------


def _fmt(sec: float) -> str:
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def contact_sheets(paths: list[Path], out_dir: Path = SHEETS) -> list[Path]:
    """각 영상에서 장면 12개를 같은 간격으로 뽑아 한 장(1920x1080)에 모은다.

    Claude 가 원본을 '보고' 구간·초점·자막을 기획할 때 쓴다. 칸마다 원본 기준 시각이 찍힌다.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    with FilmCraft() as fc:
        for path in paths:
            info = probe(path)
            fc.run("file.newProject")
            item = fc.run("file.import", paths=[str(path)])["items"][0]
            out = out_dir / f"{path.stem}.png"
            times = sheet(fc, item, info["duration_s"], info["width"], info["height"], out)
            outputs.append(out)
            print(f"  ▦ {rel(out)}  ({', '.join(_fmt(t) for t in times[:3])} … )")
    return outputs


def photo_sheets(paths: list[Path], per_sheet: int = 12) -> dict[Path, list[tuple[str, Path]]]:
    """사진 여러 장을 12장씩 한 장에 모은다. 칸마다 번호(#01…)와 해상도를 찍는다."""
    W, H = 1920, 1080
    c, r = 6, 2
    cw, ch = W / c, H / r
    out: dict[Path, list[tuple[str, Path]]] = {}
    SHEETS.mkdir(parents=True, exist_ok=True)
    with FilmCraft() as fc:
        for start in range(0, len(paths), per_sheet):
            group = paths[start:start + per_sheet]
            fc.run("file.newProject")
            fc.run("file.newSequence", name="photos", width=W, height=H, fps=30, video=len(group) * 2 + 1, audio=1)
            bg = fc.run("file.newColorMatte", color="#111111", seconds=1)["item"]
            fc.run("timeline.place", item=bg, track="V1", seconds=0, duration=seconds_to_ticks(1))
            members = []
            for i, path in enumerate(group):
                info = probe(path)
                item = fc.run("file.import", paths=[str(path)])["items"][0]
                clip = fc.run("timeline.place", item=item, track=f"V{i + 2}", seconds=0,
                              duration=seconds_to_ticks(1), audio=False)["clips"][0]
                fc.run("clip.fitToFrame", clips=[clip])
                scale = clip_motion(fc, clip)["scale"]
                fit = min(cw / (info["width"] * scale / 100), ch / (info["height"] * scale / 100))
                cx, cy = (i % c + 0.5) * cw, (i // c + 0.5) * ch
                fc.run("effects.setParam", clip=clip, effect="motion", param="scale", value=scale * fit * 0.94)
                fc.run("effects.setParam", clip=clip, effect="motion", param="position", value=[cx, cy])
                label = f"#{start + i + 1:02d}"
                low = " 저해상도" if max(info["width"], info["height"]) < 1280 else ""
                t = fc.run("graphics.newText", text=f"{label} {info['width']}x{info['height']}{low}",
                           position=[cx - cw / 2 + 10, cy - ch / 2 + 30], size=22, seconds=1,
                           track=len(group) + 1 + i, time=0)["clip"]
                fc.run("graphics.set", clip=t, layer=0, props={"fill_color": "#ffffff", "background": True,
                                                                 "font": "Apple SD Gothic Neo", "font_style": "Bold"})
                members.append((label, path))
            sheet_path = SHEETS / f"photos_{group[0].stem[:20]}_{start // per_sheet + 1}.png"
            fc.run("file.exportFrame", path=str(sheet_path), time=0)
            out[sheet_path] = members
            print(f"  ▦ {rel(sheet_path)}  (사진 {len(group)}장)")
    return out


def sheet(fc: FilmCraft, item: int, dur: float, w: int, h: int, out: Path,
          times: list[float] | None = None) -> list[float]:
    """프로젝트 아이템(원본 클립이나 완성 시퀀스)의 여러 시점을 한 장에 모은다."""
    W, H = 1920, 1080
    c, r = (6, 2) if h > w else (4, 3)
    cw, ch, n = W / c, H / r, c * r
    times = times or [dur * (i + 0.5) / n for i in range(n)]
    times = times[:n]
    active = fc.inspect()["project"]["activeSequence"]
    fc.run("file.newSequence", name="_sheet", width=W, height=H, fps=30, video=n * 2 + 1, audio=1)
    bg = fc.run("file.newColorMatte", color="#111111", seconds=1)["item"]
    fc.run("timeline.place", item=bg, track="V1", seconds=0, duration=seconds_to_ticks(1))
    for i, t in enumerate(times):
        clip = fc.run(
            "timeline.place", item=item, track=f"V{i + 2}", seconds=0,
            sourceIn=seconds_to_ticks(t), duration=seconds_to_ticks(1), audio=False,
        )["clips"][0]
        fc.run("clip.fitToFrame", clips=[clip])
        scale = clip_motion(fc, clip)["scale"]
        fw, fh = w * scale / 100, h * scale / 100
        fit = min(cw / fw, ch / fh)
        cx, cy = (i % c + 0.5) * cw, (i // c + 0.5) * ch
        fc.run("effects.setParam", clip=clip, effect="motion", param="scale", value=scale * fit * 0.96)
        fc.run("effects.setParam", clip=clip, effect="motion", param="position", value=[cx, cy])
        label = fc.run(
            "graphics.newText", text=_fmt(t), position=[cx - cw / 2 + 12, cy - ch / 2 + 34],
            size=26, seconds=1, track=n + 1 + i, time=0,
        )["clip"]
        fc.run("graphics.set", clip=label, layer=0,
               props={"fill_color": "#ffffff", "background": True, "font_style": "Bold"})
    out.parent.mkdir(parents=True, exist_ok=True)
    fc.run("file.exportFrame", path=str(out), time=0)
    # 미리보기용 시퀀스는 지우고 원래 시퀀스로 돌아간다
    sheet_seq = fc.inspect()["project"]["activeSequence"]
    if active is not None:
        fc.run("sequence.open", item=active)
    fc.run("project.delete", items=[sheet_seq, bg])
    return times


_FLOAT = re.compile(r"Float\(([-\d.e]+)\)")
_VEC2 = re.compile(r"x: ([-\d.e]+), y: ([-\d.e]+)")


def clip_motion(fc: FilmCraft, clip_id: int) -> dict:
    """타임라인 클립의 현재 motion scale/position."""
    seq = fc.sequence()
    for track in seq["video"]:
        for item in track["items"]:
            if item.get("clip") == clip_id:
                motion = next(e for e in item["effects"] if e["effect"] == "motion")["params"]
                scale = float(_FLOAT.search(motion["scale"]["value"]).group(1))
                px, py = map(float, _VEC2.search(motion["position"]["value"]).groups())
                return {"scale": scale, "position": [px, py]}
    raise KeyError(f"clip {clip_id} not found")


_LEVEL_CACHE: dict[str, tuple] = {}  # 경로 → (크기, 샘플 배열)


def audio_level(path: Path, start: float, end: float) -> float:
    """원본 소리의 평균 크기(dBFS, start~end). 소리가 거의 없는 클립은 BGM 을 '음악만' 크기로 올린다
    (조용한 원본 + BGM 의 쉬는 박자 = 소리 끊김, 2026-10-18 육아일지 #2 QA). 실패하면 0.0(=소리 있음)."""
    import array
    import math
    import subprocess
    import tempfile
    import wave

    key = str(Path(path).resolve())
    if key not in _LEVEL_CACHE:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "a.wav"
            try:
                subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@8000", "-c", "1", key, str(wav)],
                               check=True, capture_output=True)
                with wave.open(str(wav)) as w:
                    a = array.array("h")
                    a.frombytes(w.readframes(w.getnframes()))
                    _LEVEL_CACHE[key] = (w.getframerate(), a)
            except (OSError, subprocess.CalledProcessError, wave.Error):
                return 0.0
    sr, a = _LEVEL_CACHE[key]
    seg = a[int(start * sr):int(end * sr)]
    if not seg:
        return -120.0
    rms = math.sqrt(sum(x * x for x in seg) / len(seg)) / 32768
    return 20 * math.log10(rms) if rms > 0 else -120.0


def loud_parts(path: Path, start: float, end: float, floor_db: float = -42.0, win: float = 0.25,
               bridge: float = 0.5) -> list[tuple[float, float]]:
    """원본 소리가 실제로 들리는 구간들 (원본 기준 초). 0.5초 미만 틈은 이어 붙인다 (BGM 이 오르내리며 출렁이지 않게)."""
    parts: list[list[float]] = []
    t = start
    while t < end - 0.01:
        t1 = min(end, t + win)
        if audio_level(path, t, t1) >= floor_db:
            if parts and t - parts[-1][1] < bridge:
                parts[-1][1] = t1
            else:
                parts.append([t, t1])
        t = t1
    return [(a, b) for a, b in parts]
