"""AI 내레이션 / 아기 속마음 더빙 (TTS).

엔진 (config/channel.toml [voices] engine):
  supertonic  기본값. 로컬 ONNX, 무료, 한국어 지원. 코드 MIT / 모델 OpenRAIL-M
              → 상업 이용 가능. 단 'AI 생성임을 숨기지 말 것' 조건이 있어 내레이션을 쓴 영상은
                설명란에 AI 음성 안내 문구가 자동으로 붙는다 (metadata.py).
  say         macOS 내장 음성. 미리듣기 전용. Apple 음성은 비상업 용도로만 허용되므로
              업로드용으로 쓰면 검증기가 막는다.

아빠 목소리 (음색에 clone = "아빠"):
  Qwen3-TTS Base 0.6B (Apache-2.0, 상업 이용 가능)로 assets/voice/아빠/ 의 녹음(ref.wav + ref.txt)을 따라 읽는다.
  mlx-audio 가 무거워 별도 가상환경(.venv-voice)의 voice_worker.py 를 띄워서 쓴다.
  녹음이 없거나 .venv-voice 가 없으면 supertonic 음색으로 대신 읽는다 (빌드가 멈추지 않게).
  본인 목소리 복제로 만든 내레이션은 YouTube '변경·합성 콘텐츠' 공개 대상이 아니다
  (YouTube 고객센터: "Cloning one's own voice to create voice overs or dubs" 는 예외).
  그래서 복제는 채널 주인(아빠) 목소리만 허용한다 (CLONE_ALLOWED).

모든 엔진에 넣기 전에 kotext.spoken() 으로 숫자·단위를 읽는 말로 바꾼다 ('3번' → '세 번', '7번째'·'육아일지 7번' 구분).
음색은 [voices.<이름>] 에서 voice(F1~F5, M1~M5)·speed·pitch(반음) 로 정한다.
'아기' 처럼 pitch 를 올리면 FilmCraft Pitch Shifter 로 귀여운 속마음 목소리가 된다.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import re
import subprocess
import wave
from pathlib import Path

from . import config, kotext
from .config import ASSETS, ROOT

CACHE = ASSETS / "_tts"
VOICES = ASSETS / "voice"  # assets/voice/<이름>/ref.wav + ref.txt (복제용 녹음)
VENV = ROOT / ".venv-voice"
CLONE_MODEL = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit"
CLONE_ALLOWED = {"아빠"}  # 본인(채널 주인) 목소리만. 다른 사람 목소리 복제는 동의·정책 문제
COMMERCIAL_OK = {"supertonic", "clone"}
# 실험(2026-10-20, 아빠 녹음 5문장×2): 온도 0.9(기본)·0.6/top_p 0.85·0.3 비교 → 0.6/0.85 가 발음 오류 6%로 안정적이고
# 끝음 늘어짐도 가장 적었다. 끝음을 끄는 주원인은 겹받침('잠듦'→'잠들~') → kotext.pronounce_finals 로 대표음 처리
CLONE_SAMPLING = {"temperature": 0.6, "top_p": 0.85}
TAIL_KEEP, TAIL_FADE, PAUSE_MAX = 0.07, 0.05, 0.25  # 끝음 정리: 마지막 음절 뒤 남길 길이·페이드, 문장 사이 쉼 최대
MAX_CER = 0.2  # 받아쓰기 글자 오류율이 이보다 크면 다시 만든다 (말 건너뛰기·반복 방지)
TRIES = 3
_tts = None
_worker: subprocess.Popen | None = None


def voice_config(name: str) -> dict:
    v = config.load().get("voices", {})
    if name not in v or not isinstance(v[name], dict):
        raise KeyError(f"[voices.{name}] 이 config/channel.toml 에 없습니다")
    vc = {"speed": 1.05, "pitch": 0, "gain_db": 0.0, **v[name]}
    if vc.get("clone") and vc["clone"] not in CLONE_ALLOWED:
        raise ValueError(f"[voices.{name}] clone='{vc['clone']}': 목소리 복제는 {', '.join(CLONE_ALLOWED)} 본인만 허용")
    return vc


def engine() -> str:
    return config.load().get("voices", {}).get("engine", "supertonic")


def clone_model() -> str:
    return config.load().get("voices", {}).get("clone_model", CLONE_MODEL)


def reference(name: str) -> tuple[Path, str] | None:
    """복제용 녹음 (wav, 녹음 내용 글). 없으면 None.

    ref_style.wav(문장 끝 '~습니다/~입니다'를 잘라 낸 녹음)가 있으면 그것을 쓴다: 모델이 녹음의 말투를 따라
    육아일지 문체 '~함/~음'을 '~합니다'로 바꿔 읽기 때문 (2026-10-21 실험: 원래 녹음 2/8 → 잘라 낸 녹음 9/12)."""
    d = VOICES / name
    for stem in ("ref_style", "ref"):
        wav, txt = d / f"{stem}.wav", d / f"{stem}.txt"
        if wav.exists() and txt.exists() and txt.read_text(encoding="utf-8").strip():
            return wav, txt.read_text(encoding="utf-8").strip()
    return None


POLITE_END = re.compile(r"(습니다|입니다|니다|어요|아요|에요|예요)[.!?]*$")
STYLE_MIN = 4.0  # 잘라 낸 녹음이 이보다 짧으면 목소리가 덜 닮아서 원래 녹음을 쓴다


def build_style_reference(name: str = "아빠") -> dict | None:
    """ref.wav 에서 존댓말 어미로 끝나는 마지막 단어를 잘라 낸 ref_style.wav/.txt 를 만든다 (인사말 '안녕하세요' 는 둔다)."""
    import numpy as np

    from . import transcribe

    d = VOICES / name
    src = d / "ref.wav"
    if not src.exists():
        return None
    words = transcribe.words_of(src)
    if not words:
        return None
    with wave.open(str(src)) as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
    # 문장 나누기: 마침표·물음표로 끝나는 단어까지
    sentences, cur = [], []
    for wd in words:
        cur.append(wd)
        if re.search(r"[.!?]$", wd["word"]):
            sentences.append(cur)
            cur = []
    if cur:
        sentences.append(cur)
    pieces, texts = [], []
    for sent in sentences:
        last = sent[-1]["word"]
        keep = sent
        if POLITE_END.search(last) and not last.startswith("안녕"):
            keep = sent[:-1]
        if not keep:
            continue
        # 잘라 낸 어미 쪽은 다음 단어 시작 전에서 멈춘다 (붙어 있으면 '아…' 같은 조각이 남는다)
        t0, t1 = max(0.0, keep[0]["start"] - 0.05), keep[-1]["end"] + (0.06 if keep is sent else -0.03)
        x = a[int(t0 * sr):int(t1 * sr)].copy()
        k = min(int(sr * 0.012), len(x) // 2)
        if k:
            ramp = np.linspace(0, 1, k, dtype=np.float32)
            x[:k] *= ramp
            x[-k:] *= ramp[::-1]
        pieces += [x, np.zeros(int(sr * 0.3), np.float32)]
        texts.append(" ".join(fix_names(w["word"]) for w in keep).rstrip(".!?") + ("." if keep is sent else ","))
    if not pieces:
        return None
    out = np.concatenate(pieces)
    secs = len(out) / sr
    style_wav, style_txt = d / "ref_style.wav", d / "ref_style.txt"
    if secs < STYLE_MIN:
        style_wav.unlink(missing_ok=True)
        style_txt.unlink(missing_ok=True)
        return {"seconds": round(secs, 1), "used": False}
    with wave.open(str(style_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(out, -32768, 32767).astype(np.int16).tobytes())
    text = kotext.spoken(" ".join(texts))
    style_txt.write_text(text + "\n", encoding="utf-8")
    return {"seconds": round(secs, 1), "used": True, "text": text}


def clone_ready(name: str = "아빠") -> tuple[bool, str]:
    if not (VENV / "bin" / "python").exists():
        return False, ".venv-voice 없음 (./scripts/setup.sh)"
    if reference(name) is None:
        return False, f"녹음 없음 → ./br voice enroll <녹음 파일> (assets/voice/{name}/)"
    return True, f"{name} 목소리 ({clone_model().split('/')[-1]})"


def resolve_engine(vc: dict) -> str:
    """이 음색을 실제로 읽을 엔진. clone 이 준비 안 됐으면 기본 엔진으로."""
    if vc.get("clone") and clone_ready(vc["clone"])[0]:
        return "clone"
    return engine()


def speak(text: str, voice_name: str) -> tuple[Path, dict]:
    """텍스트 → wav (캐시). (경로, 음색 설정) 을 돌려준다. vc['engine'] 에 실제로 쓴 엔진."""
    vc = voice_config(voice_name)
    eng = resolve_engine(vc)
    clean = kotext.spoken(text)
    ident = vc["voice"]
    if eng == "clone":
        ref_wav, ref_text = reference(vc["clone"])
        ident = f"{vc['clone']}:{_digest(ref_wav)}:{clone_model()}:{CLONE_SAMPLING}:{TAIL_KEEP}:{PAUSE_MAX}:v8"
    key = hashlib.sha1(f"{eng}|{ident}|{vc['speed']}|{clean}".encode()).hexdigest()[:16]
    out = CACHE / f"{key}.wav"
    vc = {**vc, "engine": eng}
    if eng == "clone":
        vc["pitch"] = 0  # 아빠 목소리는 그대로
    if not out.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        if eng == "clone":
            _clone(clean, vc, out)
        elif eng == "supertonic":
            _supertonic(clean, vc, out)
        elif eng == "say":
            _say(clean, vc, out)
        else:
            raise KeyError(f"알 수 없는 TTS 엔진 '{eng}'")
    return out, vc


def _digest(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:10]


def duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def speech_bounds(path: Path, floor_db: float = -45.0, pad: float = 0.05) -> tuple[float, float]:
    """실제로 말하는 구간 (앞뒤 무음 제외). TTS 출력은 앞 ~0.5초·뒤 ~0.6초가 무음이라
    그대로 깔면 자막보다 말이 늦고, 더킹이 무음 동안에도 걸려 소리가 텅 빈다 (2026-10-17 QA)."""
    import array
    import math

    with wave.open(str(path)) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        if w.getsampwidth() != 2:
            return 0.0, w.getnframes() / sr
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
    total = len(a) / ch / sr
    win = max(1, int(sr * 0.02)) * ch
    loud = []
    for i in range(0, len(a), win):
        seg = a[i:i + win]
        r = math.sqrt(sum(x * x for x in seg) / max(1, len(seg))) / 32768
        loud.append(r > 0 and 20 * math.log10(r) > floor_db)
    if not any(loud):
        return 0.0, total
    first = loud.index(True) * win / ch / sr
    last = (len(loud) - loud[::-1].index(True)) * win / ch / sr
    return max(0.0, first - pad), min(total, last + pad)


def _supertonic(text: str, vc: dict, out: Path) -> None:
    global _tts
    try:
        import supertonic
    except ImportError as e:
        raise RuntimeError("TTS 미설치: ./scripts/setup.sh 실행 (.venv 에 supertonic 설치)") from e
    if _tts is None:
        _tts = supertonic.TTS(model="supertonic-3")
    style = _tts.get_voice_style(vc["voice"])
    wav, _ = _tts.synthesize(text, style, lang="ko", total_steps=12, speed=vc["speed"], silence_duration=0.15)
    _tts.save_audio(wav, str(out))


def _say(text: str, vc: dict, out: Path) -> None:
    aiff = out.with_suffix(".aiff")
    subprocess.run(["say", "-v", vc.get("say_voice", "Yuna"), "-o", str(aiff), text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@48000", str(aiff), str(out)], check=True)
    aiff.unlink()


# --- 아빠 목소리 (Qwen3-TTS, 별도 가상환경) ---------------------------------------------

def _start_worker() -> subprocess.Popen:
    global _worker
    if _worker is None or _worker.poll() is not None:
        print(f"    아빠 목소리 모델 올리는 중 ({clone_model().split('/')[-1]}, 처음 한 번 30초쯤)…", flush=True)
        _worker = subprocess.Popen(
            [str(VENV / "bin" / "python"), "-m", "babyreel.voice_worker", clone_model()],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        while True:
            line = _worker.stdout.readline()
            if not line:
                raise RuntimeError("아빠 목소리 작업자가 시작하지 못했습니다 (.venv-voice 확인: ./scripts/setup.sh)")
            if line.startswith("{") and json.loads(line).get("ready"):
                break
        atexit.register(stop_worker)
    return _worker


def stop_worker() -> None:
    global _worker
    if _worker is not None and _worker.poll() is None:
        _worker.stdin.close()
        _worker.wait(timeout=10)
    _worker = None


def _clone_once(text: str, vc: dict, out: Path, seed: int) -> None:
    ref_wav, ref_text = reference(vc["clone"])
    w = _start_worker()
    req = {"text": text if re.search(r"[.!?…,]$", text.strip()) else sentence_end(text), "out": str(out),
           "ref_audio": str(ref_wav), "ref_text": ref_text,
           "seed": seed, **CLONE_SAMPLING, **{k: vc[k] for k in ("temperature", "top_p") if k in vc}}
    w.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
    w.stdin.flush()
    res = json.loads(w.stdout.readline() or '{"ok": false, "error": "작업자 종료"}')
    if not res.get("ok"):
        raise RuntimeError(f"아빠 목소리 생성 실패: {res.get('error')}")


# 육아일지 문체('~함/~음/~중') 끝을 녹음 말투('~습니다')로 바꿔 읽는 경우가 있다 (2026-10-21 '발견함'→'발견합니다').
# 문장 끝 부호를 바꿔 가며 다시 만들고, 받아쓰기로 끝이 맞는 것을 고른다
NOMINAL_END = re.compile(r"(함|음|임|중|듦|듬|름|됨|짐|옴|감|봄|남|줌|씀|있음|없음)[.!?…,]*$")
END_VARIANTS = (".", ",", "…", ".")


def clone_attempts(text: str) -> list[tuple[str, dict]]:
    """(읽힐 글, 추가 설정) 후보 목록. 육아일지 문체 끝이면 끝 부호를 바꾼 후보를 더 만든다."""
    t = text.strip()
    if not NOMINAL_END.search(t):
        return [(sentence_end(t), {})] * TRIES
    core = re.sub(r"[.!?…,]+$", "", t)
    return [(core + END_VARIANTS[0], {}), (core + END_VARIANTS[1], {}), (core + END_VARIANTS[2], {}),
            (core + END_VARIANTS[3], {"temperature": 0.3})]


def _clone(text: str, vc: dict, out: Path) -> None:
    """만든 음성을 받아쓰기로 확인해서, 말을 건너뛰거나 반복했거나 문장 끝이 바뀌었으면 다시 만든다 (LLM 기반 TTS 의 약점)."""
    best: tuple[float, Path] | None = None
    for seed, (variant, extra) in enumerate(clone_attempts(text)):
        tmp = out.with_name(f"{out.stem}.try{seed}.wav")
        _clone_once(variant, {**vc, **extra}, tmp, seed)
        tighten(tmp)  # 다듬은 최종 소리로 확인한다 (다듬기 전엔 맞았는데 뒤에 달라지는 일이 없게)
        err = check(tmp, text)
        if best is None or err < best[0]:
            if best:
                best[1].unlink(missing_ok=True)
            best = (err, tmp)
        else:
            tmp.unlink(missing_ok=True)
        if err <= MAX_CER:
            break
    err, path = best
    if err > MAX_CER:
        print(f"    ! 아빠 목소리 '{text[:14]}…' 발음 확인 오류율 {err:.0%} (가장 나은 것 사용, 들어 보고 문장을 바꿔 보세요)")
    path.replace(out)


def sentence_end(text: str) -> str:
    """문장 끝을 마침표로 닫는다 ('기록은 계속된다' → '기록은 계속된다.'). 열린 끝은 끝음을 끄는 경향."""
    t = text.strip()
    return t + "." if t and re.search(r"[가-힣A-Za-z0-9)]$", t) else t


def tighten(path: Path, keep: float = TAIL_KEEP, fade: float = TAIL_FADE, pause_max: float = PAUSE_MAX) -> dict:
    """끝음을 끌지 않게: 마지막 음절의 잔향을 짧게 자르고(페이드), 문장 사이 긴 쉼을 줄인다 (2026-10-20 사용자 요청).

    10ms 단위 음량에서 '말소리'는 최대보다 30dB 안쪽. 마지막 말소리 뒤 keep 초만 남기고 fade 초 동안 줄인다.
    중간의 무음이 pause_max 보다 길면 그만큼 잘라 낸다 (앞뒤 30ms 크로스페이드). 결과 정보를 돌려준다.
    """
    import numpy as np

    with wave.open(str(path)) as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if sw != 2:
            return {}
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(1)
    hop = max(1, int(sr * 0.01))
    n = len(a) // hop
    if n < 10:
        return {}
    db = 20 * np.log10(np.sqrt((a[:n * hop].reshape(n, hop) ** 2).mean(1)) / 32768 + 1e-9)
    speech = db > db.max() - 30
    idx = np.where(speech)[0]
    first, last = idx[0], idx[-1]
    before = len(a) / sr
    # 문장 사이 긴 쉼 줄이기
    pieces, i, cut = [], first, 0.0
    start = max(0, first * hop - int(sr * 0.05))
    xf = int(sr * 0.03)
    while i <= last:
        if not speech[i]:
            j = i
            while j <= last and not speech[j]:
                j += 1
            gap = (j - i) / 100
            if gap > pause_max:
                keep_gap = int(pause_max * 100)
                a0, b0 = (i + keep_gap // 2) * hop, (j - keep_gap // 2) * hop
                pieces.append((start, a0))
                start = b0
                cut += gap - pause_max
            i = j
        else:
            i += 1
    end = min(len(a), (last + 1) * hop + int(sr * keep))
    pieces.append((start, end))
    out = a[pieces[0][0]:pieces[0][1]].copy()
    for s0, s1 in pieces[1:]:
        nxt = a[s0:s1].copy()
        k = min(xf, len(out), len(nxt))
        if k:
            ramp = np.linspace(0, 1, k, dtype=np.float32)
            out[-k:] = out[-k:] * (1 - ramp) + nxt[:k] * ramp
            nxt = nxt[k:]
        out = np.concatenate([out, nxt])
    f = min(len(out), int(sr * fade))
    if f:
        out[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
    out = np.concatenate([out, np.zeros(int(sr * 0.05), np.float32)])  # 아주 짧은 여백
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(out, -32768, 32767).astype(np.int16).tobytes())
    return {"before": round(before, 2), "after": round(len(out) / sr, 2), "pauses_cut": round(cut, 2)}


def check(wav: Path, text: str) -> float:
    """받아쓰기 글자 오류율 (0~1). 받아쓰기를 못 쓰면 0 (검사 생략)."""
    from . import transcribe

    try:
        heard = transcribe.text_of(wav)
    except Exception:  # noqa: BLE001
        return 0.0
    if heard is None:
        return 0.0
    return cer(text, fix_names(heard))


def cer(expected: str, heard: str) -> float:
    a = re.sub(r"[^가-힣]", "", kotext.spoken(expected))
    b = re.sub(r"[^가-힣]", "", kotext.spoken(heard))
    if not a:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[len(b)] / len(a)


# --- 녹음 등록 ---------------------------------------------------------------------

# 받아쓰기가 자주 틀리는 채널 고유 이름 (ref.txt 는 녹음 내용과 같아야 따라 읽기가 정확하다)
# [공개 사본] 실제 교정 규칙(채널·가족 고유 이름)은 비공개 설정에 있다. 형식: [(정규식, 바꿀 말), ...]
NAME_FIXES: list[tuple[str, str]] = [(r"(?<![가-힣])에이(?=[는가와랑야이를의도])", "아이A")]


def fix_names(text: str) -> str:
    for pat, rep in NAME_FIXES:
        text = re.sub(pat, rep, text)
    return text


# 녹음 대본은 내레이션과 같은 문체로: 모델이 녹음의 말투를 따라 읽기 때문
SCRIPT = "<녹음 대본은 비공개 설정에서 읽음>"  # [공개 사본] 채널 말투로 쓴 등록용 대본


def to_wav(src: Path, out: Path, rate: int = 24000) -> None:
    """녹음 → 모노 16bit wav. m4a·mp3·wav·mov 는 afconvert, 텔레그램 음성 메시지(ogg/opus)는 .venv-voice 의 soundfile."""
    cmd = ["afconvert", "-f", "WAVE", "-d", f"LEI16@{rate}", "-c", "1"]
    if subprocess.run(cmd + [str(src), str(out)], capture_output=True).returncode == 0:
        return
    py = VENV / "bin" / "python"
    if not py.exists():
        raise ValueError(f"{src.suffix} 녹음을 읽지 못했습니다 (m4a·wav 로 보내 주세요)")
    mid = out.with_name(out.stem + ".raw.wav")
    code = ("import sys, soundfile as sf; a, sr = sf.read(sys.argv[1], dtype='float32'); "
            "sf.write(sys.argv[2], a if a.ndim == 1 else a.mean(1), sr, subtype='PCM_16')")
    r = subprocess.run([str(py), "-c", code, str(src), str(mid)], capture_output=True, text=True)
    if r.returncode:
        raise ValueError(f"녹음을 읽지 못했습니다: {r.stderr.strip()[-200:]}")
    subprocess.run(cmd + [str(mid), str(out)], check=True, capture_output=True)
    mid.unlink()


REF_MIN, REF_MAX = 5.0, 15.0  # Qwen3-TTS 는 3초부터 되지만 5~15초가 안정적


def enroll(src: Path, name: str = "아빠") -> dict:
    """녹음 파일(음성 메모 m4a·wav·영상도 가능) → assets/voice/<name>/ref.wav + ref.txt.

    받아쓰기로 문장 단위를 찾아 5~15초 구간을 고르고(가장 크고 또렷한 연속 문장), 그 글을 ref.txt 에 적는다.
    ref.txt 는 실제 녹음 내용과 똑같아야 따라 읽기가 정확하다 → 틀린 글자는 직접 고쳐도 된다.
    """
    import array
    import math
    import tempfile

    from . import transcribe

    if name not in CLONE_ALLOWED:
        raise ValueError(f"목소리 복제는 {', '.join(CLONE_ALLOWED)} 본인만 허용합니다")
    if not src.exists():
        raise FileNotFoundError(src)
    d = VOICES / name
    d.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        full = Path(tmp) / "full.wav"
        to_wav(src, full)
        segs = transcribe.segments_of(full)
        with wave.open(str(full)) as w:
            sr = w.getframerate()
            a = array.array("h")
            a.frombytes(w.readframes(w.getnframes()))
        total = len(a) / sr

        def level(t0: float, t1: float) -> float:
            seg = a[int(t0 * sr):int(t1 * sr)]
            r = math.sqrt(sum(x * x for x in seg) / max(1, len(seg))) / 32768
            return 20 * math.log10(r) if r > 0 else -120.0

        segs = [s for s in segs if re.search(r"[가-힣]", s["text"]) and s["end"] > s["start"]]
        if not segs:
            raise ValueError("말소리를 찾지 못했습니다. 조용한 곳에서 또박또박 10초 이상 읽어 주세요")
        # 연속 문장 묶음 중 5~15초이고 가장 큰 소리
        best = None
        for i in range(len(segs)):
            for j in range(i, len(segs)):
                t0, t1 = segs[i]["start"], segs[j]["end"]
                if t1 - t0 > REF_MAX:
                    break
                if t1 - t0 >= REF_MIN or (i == 0 and j == len(segs) - 1):
                    score = level(t0, t1) + min(t1 - t0, 12) * 0.5
                    if best is None or score > best[0]:
                        best = (score, t0, t1, " ".join(s["text"].strip() for s in segs[i:j + 1]))
        _, t0, t1, text = best
        text = fix_names(text)
        t0, t1 = max(0.0, t0 - 0.15), min(total, t1 + 0.25)
        out = d / "ref.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(a[int(t0 * sr):int(t1 * sr)].tobytes())
        (d / "ref.txt").write_text(text + "\n", encoding="utf-8")
        noise = min((level(x, x + 0.3) for x in [k * 0.3 for k in range(int(total / 0.3))]), default=-120.0)
        info = {"seconds": round(t1 - t0, 1), "text": text, "level_db": round(level(t0, t1), 1),
                "noise_db": round(noise, 1), "source": src.name, "wav": str(out)}
        (d / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    style = build_style_reference(name)
    info["style"] = style
    return info
