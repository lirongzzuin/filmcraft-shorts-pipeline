"""BGM 카탈로그.

기본 음원은 Kevin MacLeod(incompetech.com)의 CC BY 4.0 곡이다.
- 라이선스가 명확해서 YouTube·Instagram 양쪽에 같은 파일을 올려도 된다(쇼츠=릴스 공용 파일).
- 조건은 '출처 표기' 하나. 크레딧은 설명란/캡션에 자동으로 붙는다(metadata.py).

YouTube 오디오 보관함 곡은 YouTube 밖(Instagram) 사용 범위가 불분명하므로
카탈로그에 넣을 때 platforms=["youtube"] 로 등록한다. 검증기가 쇼츠(=릴스)에 쓰는 것을 막는다.

곡 추가: assets/music/catalog.json 에 항목을 넣고 파일을 assets/music/ 에 둔다.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ASSETS
from .config import resolve as abs_path

if TYPE_CHECKING:
    from .plan import Music

MUSIC_DIR = ASSETS / "music"
CATALOG_PATH = MUSIC_DIR / "catalog.json"
INCOMPETECH = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/"
CC_BY = "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)"


def _km(title: str, moods: list[str], note: str, situations: list[str] | None = None, energy: int = 2) -> dict:
    slug = title.lower().replace(" - ", "_").replace(" ", "_")
    return {
        "id": slug,
        "title": title,
        "artist": "Kevin MacLeod",
        "moods": moods,
        "situations": situations or [],  # 어울리는 장면 (music.suggest 가 고를 때 쓰는 한국어 태그)
        "energy": energy,  # 1 잔잔 · 2 보통 · 3 신남 (장면 전환 때 너무 튀지 않게)
        "note": note,
        "file": f"assets/music/{slug}.mp3",
        "url": INCOMPETECH + urllib.parse.quote(title) + ".mp3",
        "license": CC_BY,
        "attribution": f'"{title}" Kevin MacLeod (incompetech.com) / Licensed under {CC_BY}',
        "platforms": ["youtube", "instagram"],  # TikTok 은 자동 감지로 음소거됨 → BGM 없는 버전을 따로 만든다
        "blocked_on": ["tiktok"],
    }


# 상황 태그 (plan 을 쓸 때 장면에 맞는 태그로 고른다: ./br music suggest <태그>)
SITUATIONS = {
    "웃김": "허당·엉뚱·예능형 반전", "장난": "장난·우당탕·신남", "몰래": "눈치·꿍꿍이·수상함", "작전": "계획·도전·운동·훈련",
    "정신없음": "폭주·울음 대소동·빠른 전개", "귀여움": "몽글몽글·놀이·옹알이", "일상": "평화로운 하루·브이로그",
    "나들이": "외출·여행·드라이브", "감동": "첫 경험·성장·뭉클한 순간", "잠": "재우기·잠든 얼굴·밤",
    "크리스마스": "연말·트리·선물", "몰아보기": "롱폼 배경·회상",
}

# 모두 incompetech.com 의 CC BY 4.0 곡.
# [공개 사본] 장면별로 큐레이션한 전체 목록은 비공개 설정(assets/music/catalog.json)에 있고, 여기엔 코드·테스트가 참조하는 곡만 남겼다.
# moods 는 TikTok 앱에서 직접 음악을 고를 때 검색어로도 쓰인다 (tiktok_search).
DEFAULT_CATALOG = [
    _km("Fluffing a Duck", ["silly", "funny"], "엉뚱/허당", ["웃김", "귀여움"], 2),
    _km("Monkeys Spinning Monkeys", ["playful", "funny"], "장난, 신남, 우당탕", ["장난", "웃김"], 3),
    _km("Carefree", ["happy", "warm"], "평화로운 일상, 나들이", ["일상", "나들이"], 2),
    _km("Merry Go", ["cute", "warm"], "귀여움, 놀이, 몽글몽글", ["귀여움", "일상"], 2),
    _km("Wallpaper", ["calm", "warm"], "잔잔, 성장 기록, 몰아보기 배경", ["몰아보기", "감동"], 1),
    _km("Wholesome", ["cute", "warm"], "포근한 귀여움, 가족", ["귀여움", "감동"], 2),
    _km("Quirky Dog", ["silly", "funny"], "엉뚱한 표정·허당 순간", ["웃김", "몰래"], 2),
    _km("Hyperfun", ["chaos", "upbeat"], "에너지 폭발, 대소동", ["정신없음", "장난"], 3),
    _km("Heartwarming", ["calm", "warm"], "따뜻한 가족 장면", ["감동", "잠"], 1),
    _km("Jingle Bells", ["festive", "happy"], "크리스마스 (신나게)", ["크리스마스", "장난"], 3),
]


# 곡 분위기 → TikTok 앱 '사운드' 검색어
MOOD_SEARCH = {
    "silly": "귀여운 웃긴 브금, funny cute", "funny": "웃긴 브금, funny", "playful": "신나는 귀여운, playful",
    "sneaky": "몰래 브금, sneaky", "mischief": "장난 브금, mischief", "curious": "궁금 브금, curious",
    "chaos": "정신없는 브금, chaos", "happy": "밝은 브금, happy", "warm": "따뜻한 잔잔, warm",
    "cute": "귀여운 브금, cute baby", "upbeat": "경쾌한 브금, upbeat", "calm": "잔잔한 감성, calm piano",
    "festive": "크리스마스 브금, christmas", "epic": "웅장한 응원 브금, epic", "piano": "잔잔한 피아노, piano",
}


def tiktok_search(track_id: str) -> str:
    entry = next((e for e in load_catalog() if e["id"] == track_id), None)
    moods = entry["moods"] if entry else ["cute"]
    return " / ".join(MOOD_SEARCH.get(m, m) for m in moods)


def blocked_platforms(m: "Music | None") -> list[str]:
    """이 BGM 을 쓰면 음소거·차단되는 플랫폼 → 그 플랫폼용 'BGM 없는 버전'을 만든다."""
    if not m:
        return []
    try:
        info = resolve(m)
    except (KeyError, FileNotFoundError):
        return []
    return list(info.get("blocked_on", []))


def load_catalog() -> list[dict]:
    """catalog.json (내려받은 상태·메모 포함) + DEFAULT_CATALOG 에 새로 추가된 곡. 같은 id 는 기본값의
    situations·energy 를 채워 넣는다 (예전 catalog.json 에는 없던 필드)."""
    defaults = {e["id"]: e for e in DEFAULT_CATALOG}
    if not CATALOG_PATH.exists():
        return [dict(e) for e in DEFAULT_CATALOG]
    saved = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    out = []
    for e in saved:
        d = defaults.get(e["id"], {})
        out.append({**{k: d[k] for k in ("situations", "energy") if k in d}, **e})
    have = {e["id"] for e in out}
    return out + [dict(e) for e in DEFAULT_CATALOG if e["id"] not in have]


def entry(track_id: str) -> dict | None:
    return next((e for e in load_catalog() if e["id"] == track_id), None)


def recent_tracks(before: str | None = None, episodes: int = 3) -> list[str]:
    """최근 에피소드(이야기 날짜 기준, before 이전)에 쓴 곡 id (최신 순, 중복 제거). 같은 곡 반복을 피하려고."""
    from .config import EPISODES

    plans = []
    for p in EPISODES.glob("*/plan.json"):
        if p.parent.name.startswith("_"):
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if before and raw.get("date", "") >= before:
            continue
        plans.append(raw)
    plans.sort(key=lambda r: r.get("date", ""), reverse=True)
    out: list[str] = []
    for raw in plans[:episodes]:
        ids = [(raw.get("music") or {}).get("track"), ((raw.get("short2") or {}).get("music") or {}).get("track")]
        ids += [c.get("music") for c in raw.get("clips", [])]
        for i in ids:
            if i and i != "off" and i not in out:
                out.append(i)
    return out


def suggest_like(track_id: str, avoid: list[str], n: int = 3) -> list[dict]:
    """이 곡과 상황 태그가 겹치는 다른 곡 (최근에 쓴 곡 제외)."""
    base = entry(track_id) or {}
    tags = base.get("situations", [])
    have = [e for e in load_catalog() if abs_path(e["file"]).exists() and e["id"] != track_id and e["id"] not in avoid]
    scored = sorted(have, key=lambda e: (-len(set(tags) & set(e.get("situations", []))),
                                         abs(e.get("energy", 2) - base.get("energy", 2)), e["id"]))
    return scored[:n]


def suggest(situation: str, avoid: list[str] | None = None, energy: int | None = None, n: int = 3) -> list[dict]:
    """상황 태그에 맞는 곡 후보. 최근에 쓴 곡(avoid)은 뒤로, 에너지가 가까운 곡을 앞으로."""
    avoid = avoid or []
    have = [e for e in load_catalog() if abs_path(e["file"]).exists()]
    hits = [e for e in have if situation in e.get("situations", [])]

    def rank(e: dict) -> tuple:
        return (e["id"] in avoid, e.get("situations", []).index(situation),
                abs(e.get("energy", 2) - energy) if energy else 0, e["id"])

    return sorted(hits, key=rank)[:n]


def save_catalog(entries: list[dict]) -> None:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def download(force: bool = False) -> list[str]:
    """카탈로그에 url 이 있는 곡을 받는다. 실패한 곡은 카탈로그에서 'missing' 으로 표시."""
    entries = load_catalog()
    got = []
    for e in entries:
        path = abs_path(e["file"])
        if path.exists() and not force:
            continue
        if not e.get("url"):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            req = urllib.request.Request(e["url"], headers={"User-Agent": "babyreel/1"})
            with urllib.request.urlopen(req, timeout=180) as r, path.open("wb") as f:
                f.write(r.read())
            got.append(e["id"])
            print(f"  ♪ {e['id']}")
        except Exception as ex:  # 네트워크 실패는 다음에 다시 시도
            print(f"  ! {e['id']} 다운로드 실패: {ex}")
    save_catalog(entries)
    return got


def resolve(m: "Music") -> dict:
    """plan.music → {path, title, license, attribution, platforms}."""
    if m.track:
        entry = next((e for e in load_catalog() if e["id"] == m.track), None)
        if not entry:
            ids = ", ".join(e["id"] for e in load_catalog())
            raise KeyError(f"카탈로그에 없는 곡 '{m.track}' (사용 가능: {ids})")
        path = abs_path(entry["file"])
        if not path.exists():
            raise FileNotFoundError(f"{entry['file']} 없음 → python3 -m babyreel setup")
        return {**entry, "path": path}
    if not m.file:
        raise KeyError("music.track 또는 music.file 중 하나는 필요합니다")
    path = abs_path(m.file)
    if not path.exists():
        raise FileNotFoundError(f"{m.file} 없음")
    return {
        "id": path.stem, "title": path.stem, "path": path, "license": m.license,
        "attribution": m.license, "platforms": ["youtube", "instagram"],
    }



GAPS_CACHE = MUSIC_DIR / "gaps.json"


def gaps(path: Path, floor_db: float = -38.0, min_len: float = 0.3) -> list[tuple[float, float]]:
    """곡 안의 '완전히 쉬는' 구간 (브레이크·곡 끝 무음). 영상 소리가 꺼진 장면과 겹치면
    영상이 뚝 끊긴 것처럼 들리므로 add_music 이 건너뛴다 (2026-10-17 롱폼 1:29.75 끊김).
    macOS afconvert 로 풀어서 50ms 창 RMS 로 찾는다. 결과는 assets/music/gaps.json 에 캐시."""
    import array
    import math
    import subprocess
    import tempfile
    import wave

    path = Path(path).resolve()
    key = f"{path.name}|{path.stat().st_mtime:.0f}|{floor_db}|{min_len}"
    try:
        cache = json.loads(GAPS_CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return [tuple(g) for g in cache[key]]
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "m.wav"
        try:
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@22050", "-c", "1", str(path), str(wav)],
                           check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            return []
        with wave.open(str(wav)) as w:
            sr = w.getframerate()
            a = array.array("h")
            a.frombytes(w.readframes(w.getnframes()))
    win = int(sr * 0.05)
    found, start = [], None
    n = len(a) // win
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
            if end - start >= min_len and start > 0.5:  # 곡 맨 앞 무음은 어차피 시작점 문제가 아니다
                found.append((round(start, 2), round(end, 2)))
            start = None
    cache[key] = found
    GAPS_CACHE.write_text(json.dumps(cache))
    return found


def playable(path: Path, start: float, length: float) -> list[tuple[float, float]]:
    """start 부터 쉬는 구간을 건너뛰며 이어 붙일 원본 구간들 [(in, out)]. 곡 끝 무음 전에서 처음으로 돌아간다."""
    from .media import probe

    mlen = probe(Path(path).resolve())["duration_s"]
    holes = gaps(path)
    tail = next((a for a, b in holes if b >= mlen - 0.2), mlen)  # 곡 끝 무음(페이드아웃 꼬리) 제외
    inner = [(a, b) for a, b in holes if b < mlen - 0.2]
    pieces, t, src = [], 0.0, start
    while t < length - 0.05:
        nxt = next(((a, b) for a, b in inner if b > src + 0.05), None)
        stop = min(tail, nxt[0]) if nxt and nxt[0] > src else (tail if not nxt else nxt[0])
        if nxt and nxt[0] <= src < nxt[1]:
            src = nxt[1]
            continue
        dur = min(length - t, stop - src)
        if dur <= 0.05:
            src = nxt[1] if nxt and stop == nxt[0] else 0.0
            continue
        pieces.append((src, src + dur))
        t += dur
        src = nxt[1] if nxt and stop == nxt[0] else 0.0
    return pieces


LOUD_CACHE = MUSIC_DIR / "loudness.json"
REF_DB = -20.0  # 곡 음량 기준 (RMS dBFS). 곡마다 녹음 크기가 달라 곡을 바꿀 때 소리가 튀지 않게 맞춘다
MAX_TRIM_DB = 6.0


def loudness(path: Path) -> float:
    """곡의 평균 음량 (소리 나는 부분의 RMS, dBFS). assets/music/loudness.json 캐시."""
    import array
    import math
    import subprocess
    import tempfile
    import wave

    path = Path(path).resolve()
    key = f"{path.name}|{path.stat().st_mtime:.0f}"
    try:
        cache = json.loads(LOUD_CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "m.wav"
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@11025", "-c", "1", str(path), str(wav)],
                       check=True, capture_output=True)
        with wave.open(str(wav)) as w:
            sr = w.getframerate()
            a = array.array("h")
            a.frombytes(w.readframes(w.getnframes()))
    win = int(sr * 0.4)
    levels = []
    for i in range(0, len(a) - win, win):
        seg = a[i:i + win]
        rms = math.sqrt(sum(x * x for x in seg) / win) / 32768
        if rms > 0:
            levels.append(20 * math.log10(rms))
    loud = [v for v in levels if v > -45] or levels or [REF_DB]
    loud.sort()
    value = round(loud[len(loud) // 2], 1)  # 중앙값: 브레이크·페이드에 덜 흔들림
    cache[key] = value
    LOUD_CACHE.write_text(json.dumps(cache))
    return value


def trim_db(path: Path) -> float:
    """기준 음량에 맞추는 보정 (±6dB 안)."""
    return max(-MAX_TRIM_DB, min(MAX_TRIM_DB, REF_DB - loudness(path)))

