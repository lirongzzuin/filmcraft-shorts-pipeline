"""저작권 걱정 없는 효과음을 직접 합성한다 (표준 라이브러리만 사용).

예능형 쇼츠에서 자주 쓰는 '팝, 띠용, 딩, 휙, 두둥, 뾰로롱' 류를 만든다.
직접 만든 소리라 YouTube·Instagram 어디서도 저작권 클레임이 생기지 않는다.
더 좋은 효과음이 필요하면 assets/sfx/ 에 같은 이름의 wav 를 넣어 덮어쓰면 된다
(출처·라이선스는 assets/sfx/LICENSES.md 에 기록).
"""

from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

from .config import ASSETS

SR = 48_000
SFX_DIR = ASSETS / "sfx"

# 이름: 설명 (plan.json 의 sfx name 으로 사용)
CATALOG = {
    "pop": "뿅/톡 - 자막·스티커 등장",
    "boing": "띠용 - 황당, 넘어짐, 반전",
    "ding": "딩 - 정답, 성공, 깨달음",
    "whoosh": "휙 - 장면 전환, 빠른 움직임",
    "dundun": "두둥 - 긴장, 결정적 순간 직전",
    "sparkle": "뾰로롱 - 귀여움, 반짝, 애교",
    "bloop": "뾱 - 작은 리액션, 갸웃",
    "fall": "슬라이드휘슬 하강 - 실패, 시무룩",
    "rise": "슬라이드휘슬 상승 - 기대, 신남",
    "tick": "똑딱 - 시간 흐름, 기다림",
}


def _env(i: int, n: int, attack: float = 0.005, release: float = 0.08) -> float:
    t = i / SR
    a = min(1.0, t / attack) if attack else 1.0
    rem = (n - i) / SR
    r = min(1.0, rem / release) if release else 1.0
    return a * r


def _write(name: str, samples: list[float]) -> Path:
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    peak = max(1e-9, max(abs(s) for s in samples))
    gain = 0.7 / peak
    path = SFX_DIR / f"{name}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = bytearray()
        for s in samples:
            v = int(max(-1.0, min(1.0, s * gain)) * 32767)
            frames += struct.pack("<hh", v, v)
        w.writeframes(bytes(frames))
    return path


def _sweep(f0: float, f1: float, secs: float, shape: str = "sine", vib: float = 0.0,
           vib_rate: float = 0.0, decay: float = 0.0, exp: bool = True) -> list[float]:
    n = int(secs * SR)
    out, phase = [], 0.0
    for i in range(n):
        x = i / n
        f = f0 * (f1 / f0) ** x if exp else f0 + (f1 - f0) * x
        if vib:
            f *= 1 + vib * math.sin(2 * math.pi * vib_rate * i / SR)
        phase += 2 * math.pi * f / SR
        s = math.sin(phase)
        if shape == "tri":
            s = 2 / math.pi * math.asin(s)
        amp = math.exp(-decay * i / SR) if decay else 1.0
        out.append(s * amp * _env(i, n))
    return out


def _mix(*tracks: list[float]) -> list[float]:
    n = max(len(t) for t in tracks)
    return [sum(t[i] if i < len(t) else 0.0 for t in tracks) for i in range(n)]


def _delay(track: list[float], secs: float) -> list[float]:
    return [0.0] * int(secs * SR) + track


def _bell(freq: float, secs: float, decay: float = 5.0) -> list[float]:
    n = int(secs * SR)
    partials = [(1, 1.0), (2.0, 0.5), (3.01, 0.25), (4.2, 0.12)]
    return [
        sum(a * math.sin(2 * math.pi * freq * p * i / SR) for p, a in partials)
        * math.exp(-decay * i / SR) * _env(i, n, 0.002, 0.05)
        for i in range(n)
    ]


def _noise(secs: float, lp_from: float, lp_to: float, rng: random.Random) -> list[float]:
    """대역이 움직이는 필터 노이즈 (휙 소리)."""
    n = int(secs * SR)
    out, y = [], 0.0
    for i in range(n):
        x = i / n
        cutoff = lp_from + (lp_to - lp_from) * math.sin(math.pi * x)
        alpha = 1 - math.exp(-2 * math.pi * cutoff / SR)
        y += alpha * (rng.uniform(-1, 1) - y)
        out.append(y * math.sin(math.pi * x) ** 1.5)
    return out


def generate(force: bool = False) -> list[Path]:
    rng = random.Random(7)
    recipes = {
        "pop": lambda: _sweep(900, 260, 0.09, decay=30),
        "boing": lambda: _sweep(140, 420, 0.55, vib=0.18, vib_rate=16, decay=4, shape="tri"),
        "ding": lambda: _mix(_bell(1318.5, 1.2), _delay(_bell(1975.5, 1.0, 6), 0.09)),
        "whoosh": lambda: _noise(0.45, 300, 5000, rng),
        "dundun": lambda: _mix(
            _sweep(110, 55, 0.35, decay=6), _delay(_sweep(104, 48, 0.6, decay=4), 0.32)
        ),
        "sparkle": lambda: _mix(*[_delay(_bell(f, 0.45, 9), d) for f, d in
                                  [(1568, 0), (1976, 0.06), (2349, 0.12), (3136, 0.18), (3951, 0.24)]]),
        "bloop": lambda: _sweep(300, 900, 0.12, decay=14),
        "fall": lambda: _sweep(1400, 250, 0.9, vib=0.015, vib_rate=7),
        "rise": lambda: _sweep(300, 1500, 0.7, vib=0.015, vib_rate=7),
        "tick": lambda: _mix(_bell(2600, 0.06, 60), _delay(_bell(2000, 0.06, 60), 0.5)),
    }
    made = []
    for name, make in recipes.items():
        path = SFX_DIR / f"{name}.wav"
        if path.exists() and not force:
            continue
        made.append(_write(name, make()))
    return made


def path_for(name: str) -> Path:
    if name not in CATALOG and not (SFX_DIR / f"{name}.wav").exists():
        raise KeyError(f"알 수 없는 효과음 '{name}'. 사용 가능: {', '.join(CATALOG)}")
    p = SFX_DIR / f"{name}.wav"
    if not p.exists():
        generate()
    return p
