"""업로드 전 결과물 자동 검사: 규격(해상도·fps·코덱·길이·용량) + 라우드니스(BS.1770).

라우드니스는 결과 mp4 의 오디오를 FilmCraft 로 WAV 로 뽑아 ITU-R BS.1770-4 방식
(K-weighting + 400ms 블록 + 절대/상대 게이트)으로 직접 잰다. ffmpeg 불필요.
"""

from __future__ import annotations

import array
import math
import tempfile
import wave
from pathlib import Path

from . import config
from .fc import FilmCraft, TICKS_PER_SECOND, cli_path
from .media import probe

import json
import subprocess


def _biquad(x: array.array, b: tuple, a: tuple) -> array.array:
    b0, b1, b2 = b
    _, a1, a2 = a
    y = array.array("d", bytes(8 * len(x)))
    x1 = x2 = y1 = y2 = 0.0
    for i, xi in enumerate(x):
        yi = b0 * xi + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, xi, y1, yi
        y[i] = yi
    return y


# BS.1770 K-weighting 계수 (48 kHz)
_SHELF = ((1.53512485958697, -2.69169618940638, 1.19839281085285), (1.0, -1.69065929318241, 0.73248077421585))
_HPF = ((1.0, -2.0, 1.0), (1.0, -1.99004745483398, 0.99007225036621))


def loudness(wav_path: Path) -> dict:
    with wave.open(str(wav_path), "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sr != 48000 or width != 2:
        raise ValueError("48kHz 16bit WAV 만 지원")
    pcm = array.array("h", raw)
    chans = [array.array("d", (s / 32768 for s in pcm[c::ch])) for c in range(ch)]
    peak = max(abs(s) for c in chans for s in c)
    weighted = [_biquad(_biquad(c, *_SHELF), *_HPF) for c in chans]
    block, hop = int(0.4 * sr), int(0.1 * sr)
    n = len(weighted[0])
    powers = []
    for start in range(0, max(1, n - block + 1), hop):
        z = sum(sum(v * v for v in c[start:start + block]) / block for c in weighted)
        powers.append(z)

    def lk(z: float) -> float:
        return -0.691 + 10 * math.log10(z) if z > 0 else -120.0

    gated = [z for z in powers if lk(z) > -70]
    if not gated:
        return {"integrated_lufs": -120.0, "sample_peak_dbfs": -120.0}
    rel = lk(sum(gated) / len(gated)) - 10
    gated = [z for z in gated if lk(z) > rel]
    return {
        "integrated_lufs": round(lk(sum(gated) / len(gated)), 1),
        "sample_peak_dbfs": round(20 * math.log10(peak), 1) if peak else -120.0,
    }


def measure(mp4: Path) -> dict:
    with tempfile.TemporaryDirectory() as tmp, FilmCraft() as fc:
        item = fc.run("file.import", paths=[str(mp4)])["items"][0]
        fc.run("file.newSequence", name="qa", fromItem=item)
        fc.run("timeline.place", item=item, track="V1", seconds=0)
        wav = Path(tmp) / "a.wav"
        fc.run("file.exportMedia", path=str(wav), format="wav", wait=True)
        return loudness(wav)


def dropouts(mp4: Path, floor_db: float = -40.0, min_len: float = 0.2, tail: float = 2.0) -> list[tuple[float, float]]:
    """영상 중간에 소리가 완전히 꺼지는 구간 (BGM 까지 함께 끊겨 '뚝' 끊긴 것처럼 들림).
    처음 0.3초와 끝 tail 초(페이드아웃)는 제외. macOS afconvert 로 오디오만 풀어 50ms 창으로 본다."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        try:
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@22050", "-c", "1", str(mp4), str(wav)],
                           check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            return []
        with wave.open(str(wav)) as w:
            sr = w.getframerate()
            a = array.array("h")
            a.frombytes(w.readframes(w.getnframes()))
    win = int(sr * 0.05)
    n = len(a) // win
    total = len(a) / sr
    runs, start = [], None
    for i in range(n + 1):
        quiet = False
        if i < n:
            seg = a[i * win:(i + 1) * win]
            rms = math.sqrt(sum(x * x for x in seg) / win) / 32768
            quiet = (20 * math.log10(rms) if rms > 0 else -120) < floor_db
        if quiet and start is None:
            start = i * win / sr
        elif not quiet and start is not None:
            end = i * win / sr
            if end - start >= min_len and start > 0.3 and end < total - tail:
                runs.append((start, end))
            start = None
    return runs


def check(mp4: Path, target: str, music: bool = True) -> list[str]:
    """문제 목록을 돌려준다 (빈 목록 = 통과)."""
    cfg = config.load()
    spec = cfg["long" if target == "long" else "short"]
    raw = json.loads(subprocess.run([str(cli_path()), "probe", str(mp4), "--compact"],
                                    capture_output=True, text=True, check=True).stdout)
    info = probe(mp4)
    problems = []
    want = (spec.get("upload_width", spec["width"]), spec.get("upload_height", spec["height"]))
    # 1440p 업로드(2026-10-29) 전에 만든 회차는 1080p 그대로 정상 (다시 렌더하지 않는다)
    if (info["width"], info["height"]) not in (want, (spec["width"], spec["height"])):
        problems.append(f"해상도 {info['width']}x{info['height']} ≠ {want[0]}x{want[1]}")
    if not str(info["codec"]).startswith("H.264"):
        problems.append(f"코덱 {info['codec']} (H.264 필요)")
    if info["fps"] and abs(info["fps"] - spec["fps"]) > 0.01:
        problems.append(f"fps {info['fps']} ≠ {spec['fps']}")
    audio = raw.get("audio_streams") or []
    if not audio or audio[0]["codec"] != "AAC" or audio[0]["sample_rate"] != 48000:
        problems.append(f"오디오 {audio} (AAC 48kHz 필요)")
    if target != "long":
        dur = raw["duration"] / TICKS_PER_SECOND
        if dur > spec["hard_max_seconds"]:
            problems.append(f"길이 {dur:.1f}s > {spec['hard_max_seconds']}s")
        if dur < 3:
            problems.append("길이 3초 미만 (Reels 최소 3초)")
        if info["size_mb"] > 300:
            problems.append(f"용량 {info['size_mb']}MB > 300MB (Instagram API 제한 대비)")
    for a, b in freezes(mp4):
        problems.append(f"{a:.1f}~{b:.1f}s 화면이 멈춰 있습니다 ({b - a:.1f}s, 끊겨 보임)")
    for a, b in (dropouts(mp4) if music else []):  # BGM 없는 버전은 조용한 구간이 정상 (앱에서 음악 추가)
        problems.append(f"{a:.2f}~{b:.2f}s 소리가 완전히 끊깁니다 ({b - a:.2f}s, BGM 쉬는 구간 + 원본 음소거 등)")
    lv = measure(mp4)
    target_lufs = cfg["audio"]["target_lufs"]
    if abs(lv["integrated_lufs"] - target_lufs) > 1.5:
        problems.append(f"라우드니스 {lv['integrated_lufs']} LUFS (목표 {target_lufs}±1.5)")
    if lv["sample_peak_dbfs"] > -0.5:
        problems.append(f"피크 {lv['sample_peak_dbfs']} dBFS (클리핑 위험)")
    print(f"  {mp4.name}: {info['width']}x{info['height']} {info['fps']}fps {info['duration_s']:.1f}s "
          f"{info['size_mb']}MB · {lv['integrated_lufs']} LUFS · peak {lv['sample_peak_dbfs']} dBFS")
    return problems


def freezes(mp4: Path, min_seconds: float = 2.0) -> list[tuple[float, float]]:
    """화면이 min_seconds 이상 완전히 멈춘 구간 (끊겨 보이는 곳). 작은 BMP 프레임으로 풀어 비교한다."""
    import struct

    with tempfile.TemporaryDirectory() as tmp, FilmCraft() as fc:
        item = fc.run("file.import", paths=[str(mp4)])["items"][0]
        fc.run("file.newSequence", name="qa_v", fromItem=item)
        fc.run("timeline.place", item=item, track="V1", seconds=0)
        info = probe(mp4)
        # 해상도와 상관없이 같은 크기(짧은 변 108px)로 비교 → 1080·1440 업로드 모두 같은 기준 (2026-10-29)
        scale = 108 / max(1, min(info["width"], info["height"]))
        res = fc.run("file.exportMedia", path=str(Path(tmp) / "f.bmp"), format="bmp", scale=scale, audio=False,
                     wait=True)
        fps = info["fps"] or 30

        def load(p: Path) -> bytes:
            b = p.read_bytes()
            return b[struct.unpack_from("<I", b, 10)[0]:]

        frames = sorted(Path(tmp).glob("f*.bmp"))
        data = [load(f)[::97] for f in frames]
        # 0.5초 간격으로 비교: 천천히 움직이는 장면은 정상, 진짜 정지만 잡는다
        gap = max(1, int(round(fps / 2)))
        still = [False] * len(data)
        for i in range(gap, len(data)):
            a, b = data[i], data[i - gap]
            still[i] = sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a)) < 0.6
        runs, start = [], None
        for i, st in enumerate(still + [False]):
            if st and start is None:
                start = i - gap
            elif not st and start is not None:
                if (i - start) / fps >= min_seconds:
                    runs.append((start / fps, i / fps))
                start = None
        del res
        return runs


def layout_problems(project: Path, seq_name: str) -> tuple[list[str], list[str]]:
    """완성 편집 프로젝트에서 자막 배치를 검사한다 → (오류, 경고).

    오류: 같은 시간에 보이는 글자끼리 영역이 겹침 / 글자가 화면 밖으로 잘림
    경고: 스티커가 글자를 가림
    """
    import re

    from . import stickers as st
    from .fc import TICKS_PER_SECOND as TPS

    speaker_pngs = {st.png_for(s["emoji"]).name for s in config.load().get("speakers", {}).values()}
    errors: list[str] = []
    warnings: list[str] = []
    with FilmCraft(project=project) as fc:
        seqs = {c["name"]: c["item"] for c in fc.inspect()["project"]["root"]["children"] if c["type"] == "Sequence"}
        if seq_name not in seqs:
            return [], []
        fc.run("sequence.open", item=seqs[seq_name])
        seq = fc.sequence()
        W, H = seq["settings"]["width"], seq["settings"]["height"]
        texts, images = [], []
        for track in seq["video"]:
            for it in track["items"]:
                effects = [e["effect"] for e in it["effects"]]
                t0, t1 = it["start"] / TPS, it["end"] / TPS
                if "graphic_text" in effects:
                    try:
                        layers = fc.run("graphics.list", clip=it["clip"])["layers"]
                    except Exception:
                        continue
                    for L in layers:
                        if L.get("kind") != "text" or not L.get("quad"):
                            continue
                        xs, ys = [p[0] for p in L["quad"]], [p[1] for p in L["quad"]]
                        texts.append((t0, t1, (min(xs), min(ys), max(xs), max(ys)), it["name"].replace("\n", " ")))
                elif it["name"].endswith(".png") and it["name"] not in speaker_pngs:
                    motion = next(e for e in it["effects"] if e["effect"] == "motion")["params"]
                    scale = float(re.search(r"Float\(([-\d.e]+)\)", motion["scale"]["value"]).group(1))
                    px, py = map(float, re.search(r"x: ([-\d.e]+), y: ([-\d.e]+)", motion["position"]["value"]).groups())
                    half = 256 * scale / 100 / 2 * 0.8  # 이모지 PNG 여백 감안
                    images.append((t0, t1, (px - half, py - half, px + half, py + half), it["name"]))

    def hit(a, b, m=4):
        return a[0] < b[2] - m and b[0] < a[2] - m and a[1] < b[3] - m and b[1] < a[3] - m

    def fmt(t):
        return f"{int(t // 60)}:{t % 60:04.1f}"

    # 쇼츠 세이프존: YouTube Shorts·Instagram Reels·TikTok UI 에 가려지는 영역
    safe = None
    if H > W:
        sc = config.load()["short"]
        safe = (sc["safe_side"], sc["safe_top"], W - sc["safe_side"], H - sc["safe_bottom"])
    deco = {config.load().get("frame", {}).get("watermark", ""), config.load()["channel"].get("handle", "")}
    nicks = tuple(c["nick"] for c in config.children())

    def decorative(name: str) -> bool:  # 나이·핸들 줄은 가려져도 되는 장식
        return name in deco or name.startswith(nicks)

    for i, (a0, a1, ra, na) in enumerate(texts):
        if ra[0] < -2 or ra[1] < -2 or ra[2] > W + 2 or ra[3] > H + 2:
            errors.append(f"[{seq_name}] {fmt(a0)} 자막이 화면 밖으로 잘림: '{na[:16]}'")
        elif safe and not decorative(na) and (ra[0] < safe[0] - 6 or ra[1] < safe[1] - 6 or ra[2] > safe[2] + 6
                                              or ra[3] > safe[3] + 6):
            errors.append(f"[{seq_name}] {fmt(a0)} 자막이 세이프존 밖 → 인스타·틱톡·쇼츠 UI 에 가려짐: '{na[:16]}'")
        for b0, b1, rb, nb in texts[i + 1:]:
            if a0 < b1 - 0.05 and b0 < a1 - 0.05 and hit(ra, rb):
                errors.append(f"[{seq_name}] {fmt(max(a0, b0))} 자막 겹침: '{na[:14]}' ↔ '{nb[:14]}'")
    if safe:
        for s0, s1, rs, ns in images:
            if rs[2] > safe[2] + 30 or rs[3] > safe[3] + 30:
                warnings.append(f"[{seq_name}] {fmt(s0)} 스티커가 세이프존 밖 (오른쪽 버튼·하단 캡션에 가려질 수 있음)")
    for s0, s1, rs, ns in images:
        for t0, t1, rt, nt in texts:
            if s0 < t1 - 0.05 and t0 < s1 - 0.05 and hit(rs, rt, 10):
                warnings.append(f"[{seq_name}] {fmt(max(s0, t0))} 스티커가 자막을 가림: '{nt[:14]}'")
    return errors, warnings


def cut_faces(mp4: Path, box: tuple[float, float, float, float], samples: int = 16) -> list[tuple[float, str]]:
    """완성 영상에서 얼굴이 영상 칸 가장자리에 걸려 잘린 순간 (Vision). box = 영상 칸 (x0, y0, x1, y1, 0~1).
    화면을 꽉 채운 클로즈업은 원래 가장자리에 닿으므로 제외하고, 머리가 작게 나오는데 위·옆이 잘린 경우만 본다."""
    from . import faces

    info = probe(mp4)
    dur = info["duration_s"]
    try:
        out = subprocess.run(faces._binary() + [str(mp4), "0", f"{dur}", str(samples)], check=True,
                             capture_output=True, text=True, timeout=300).stdout
        frames = json.loads(out.strip().splitlines()[-1]).get("frames", [])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return []
    bx0, by0, bx1, by1 = box
    bh, bw = by1 - by0, bx1 - bx0
    hits = []
    for f in frames:
        for b in f.get("faces", []):
            if not (bx0 <= (b[0] + b[2]) / 2 <= bx1 and by0 <= (b[1] + b[3]) / 2 <= by1):
                continue  # 영상 칸 밖(썸네일·자막 영역)
            small = (b[3] - b[1]) < 0.35 * bh and (b[2] - b[0]) < 0.45 * bw
            if not small:
                continue
            if b[1] <= by0 + 0.004:
                hits.append((f["t"], "위쪽"))
            elif b[0] <= bx0 + 0.004 or b[2] >= bx1 - 0.004:
                hits.append((f["t"], "옆"))
    return hits


def full_check(episode_dir: Path, targets: list[str] | None = None) -> tuple[list[str], list[str]]:
    """쇼츠·롱폼 전체 검수 → (오류, 경고). publish 는 오류가 하나라도 있으면 멈춘다.

    영상 파일: 해상도·fps·코덱·길이·용량·라운드니스·피크·멈춘 화면(2초 이상)·소리 끊김(0.2초 이상 무음)
    쇼츠 1부(short)·2부(short2)·롱폼(long) 모두 같은 기준
    편집 프로젝트: 자막 겹침·화면 밖 잘림(오류), 스티커가 자막을 가림(경고)
    """
    errors: list[str] = []
    warnings: list[str] = []
    for target in targets or ["short", "short2", "long"]:
        mp4 = episode_dir / "out" / f"{target}.mp4"
        if not mp4.exists():
            continue
        errors += [f"[{target}] {p}" for p in check(mp4, target)]
        nb = episode_dir / "out" / f"{target}_nobgm.mp4"
        if nb.exists():
            errors += [f"[{target}_nobgm] {p}" for p in check(nb, target, music=False)]
        # 얼굴 잘림 (완성본에서 Vision 으로 확인, 경고)
        cfg = config.load()
        if target == "long":
            box = (0.0, 0.0, 1.0, 1.0)
        else:
            fr, sc = cfg["frame"], cfg["short"]
            layout = "frame"
            try:
                layout = json.loads((episode_dir / "plan.json").read_text(encoding="utf-8")).get("layout", "frame")
            except (OSError, ValueError):
                pass
            box = (0.0, fr["media_top"] / sc["height"], 1.0, (fr["media_top"] + fr["media_height"]) / sc["height"]) \
                if layout == "frame" else (0.0, 0.0, 1.0, 1.0)
        for t, where in cut_faces(mp4, box):
            warnings.append(f"[{target}] {t:.1f}s 얼굴 {where}이 영상 칸 경계에서 잘림 → 해당 클립 focus_y/zoom 확인")
        project = episode_dir / "project.fcproj"
        if project.exists():
            e, w = layout_problems(project, target)
            errors += e
            warnings += w
    # 썸네일·커버: 글자가 얼굴을 가림·화면 밖 잘림·자리 모자람·얼굴 못 찾음(오류), 얼굴 작음·흐림(경고)
    from . import thumbs

    out = episode_dir / "out"
    wanted = {"long": list(thumbs.FILES.values()), "short": ["cover.jpg"], "short2": ["cover2.jpg"]}
    for target in targets or ["short", "short2", "long"]:
        if not (out / f"{target}.mp4").exists():
            continue
        for name in wanted.get(target, []):
            img = out / name
            if not thumbs.report_path(img).exists() and img.exists():
                warnings.append(f"[{name}] 예전 방식 썸네일 → ./br thumbnail 로 다시 만들면 검사됩니다")
                continue
            e, w = thumbs.problems(img)
            errors += [f"[썸네일] {x}" for x in e]
            warnings += [f"[썸네일] {x}" for x in w]
    return errors, warnings
