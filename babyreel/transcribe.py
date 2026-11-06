"""원본 영상 속 말소리 받아쓰기 (로컬 mlx-whisper, 무료).

Claude 는 소리를 직접 들을 수 없다. 받아쓴 대사·타임스탬프를 보고
'아빠: 공 굴려볼까?' 같은 대사 자막과 컷 지점을 정한다.
결과: library/_transcripts/<영상이름>.json

아기 옹알이는 잘 안 잡히고, 조용한 구간에서 Whisper 가 '시청해주셔서 감사합니다' 같은
문장을 지어내는(환각) 경우가 있어 걸러낸다.
"""

from __future__ import annotations

import json
import tempfile
import wave
from pathlib import Path

from .fc import FilmCraft
from .media import LIBRARY

OUT = LIBRARY / "_transcripts"
MODEL = "mlx-community/whisper-large-v3-turbo"
HALLUCINATIONS = ("시청해주셔서", "구독과 좋아요", "MBC 뉴스", "자막 제공", "감사합니다.\n", "다음 영상에서")


def available() -> bool:
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _load16k(path: Path):
    import numpy as np

    with wave.open(str(path)) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    a = a.reshape(-1, ch).mean(1)
    n = int(len(a) * 16000 / sr)
    return np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)


def segments_of(wav: Path) -> list[dict]:
    """wav 파일(16bit) 바로 받아쓰기 → [{start, end, text}]. 캐시 없음 (녹음 등록·TTS 확인용)."""
    if not available():
        return []
    import mlx_whisper

    r = mlx_whisper.transcribe(_load16k(wav), path_or_hf_repo=MODEL, language="ko",
                               condition_on_previous_text=False)
    return [{"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"].strip()}
            for s in r["segments"] if s["text"].strip() and s.get("no_speech_prob", 0) <= 0.6]


def words_of(wav: Path) -> list[dict]:
    """wav 받아쓰기 단어별 시각 [{word, start, end}] (목소리 녹음에서 문장 끝 어미를 잘라 낼 때)."""
    if not available():
        return []
    import mlx_whisper

    r = mlx_whisper.transcribe(_load16k(wav), path_or_hf_repo=MODEL, language="ko", word_timestamps=True,
                               condition_on_previous_text=False)
    return [{"word": w["word"].strip(), "start": float(w["start"]), "end": float(w["end"])}
            for s in r["segments"] for w in s.get("words", []) if w["word"].strip()]


def text_of(wav: Path) -> str | None:
    """wav 전체를 받아쓴 글. 받아쓰기를 못 쓰면 None."""
    if not available():
        return None
    return " ".join(s["text"] for s in segments_of(wav))


def transcribe(video: Path, force: bool = False) -> dict | None:
    out = OUT / f"{video.stem}.json"
    if out.exists() and not force:
        return json.loads(out.read_text(encoding="utf-8"))
    if not available():
        return None
    import mlx_whisper

    with tempfile.TemporaryDirectory() as tmp, FilmCraft() as fc:
        item = fc.run("file.import", paths=[str(video)])["items"][0]
        fc.run("file.newSequence", name="stt", fromItem=item)
        fc.run("timeline.place", item=item, track="V1", seconds=0)
        wav = Path(tmp) / "a.wav"
        try:
            fc.run("file.exportMedia", path=str(wav), format="wav", wait=True)
        except Exception:
            return None  # 소리 없는 영상
        audio = _load16k(wav)
    r = mlx_whisper.transcribe(audio, path_or_hf_repo=MODEL, language="ko", word_timestamps=False,
                               condition_on_previous_text=False)
    segments = []
    for s in r["segments"]:
        text = s["text"].strip()
        if not text or s.get("no_speech_prob", 0) > 0.6 or any(h.strip() in text for h in HALLUCINATIONS):
            continue
        segments.append({"start": round(s["start"], 2), "end": round(s["end"], 2), "text": text})
    result = {"file": video.name, "model": MODEL, "segments": segments}
    OUT.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def speech_lines(video: Path, start: float = 0.0, end: float | None = None) -> list[dict]:
    """받아쓰기 각 줄에 '진짜 대사일 가능성'을 붙인다 (Claude 는 소리를 못 들으므로 판단 근거).

    real = 실제 소리가 들리고(-40dBFS 이상) 환각 패턴이 아님. 환각 패턴:
    같은 문장 3번 이상 반복, 영상 길이 밖, 한글이 거의 없는 외국어·기호 조각.
    """
    import re

    from .media import audio_level, probe

    data = transcribe(video) or {"segments": []}
    dur = probe(video)["duration_s"]
    segs = data.get("segments", [])
    counts: dict[str, int] = {}
    for sg in segs:
        counts[sg["text"]] = counts.get(sg["text"], 0) + 1
    out = []
    for sg in segs:
        a, b, text = sg["start"], sg["end"], sg["text"]
        if b < start or (end is not None and a > end):
            continue
        hangul = len(re.findall(r"[가-힣]", text))
        why = []
        if counts[text] >= 3:
            why.append("반복")
        if a > dur + 0.1 or b < a:
            why.append("영상 밖")
        if hangul < max(1, len(text.replace(" ", "")) * 0.5):
            why.append("한글 아님")
        lv = audio_level(video, a, max(b, a + 0.2)) if a <= dur else -120.0
        if lv < -40:
            why.append(f"소리 작음 {lv:.0f}dB")
        out.append({"start": a, "end": b, "text": text, "level_db": round(lv, 1), "real": not why, "why": why})
    return out
