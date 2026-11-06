"""config/channel.toml 로더."""

from __future__ import annotations

import re

import tomllib
from datetime import date
from functools import lru_cache
import os
from pathlib import Path
from typing import Any

from .fc import ROOT

CONFIG_PATH = ROOT / "config" / "channel.toml"
# 다른 채널 프로필로 빌드할 때 (외부 렌더 작업자: babyreel/render.py). .toml 또는 .json
# 지정하지 않으면 지금처럼 config/channel.toml (이 채널 고정 형식)
# 촬영 폴더 inbox/ = 로컬 폴더(~/Movies/촬영폴더)로 연결된 심볼릭 링크 (2026-10-19: iCloud 는 넣는 데 오래 걸려 로컬로).
# 다른 폴더로 바꾸려면 링크만 바꾸면 된다 (docs/AUTOMATION.md 1번). BABYREEL_INBOX 로 덮어쓸 수 있다 (selftest)
INBOX = Path(os.environ["BABYREEL_INBOX"]) if os.environ.get("BABYREEL_INBOX") else ROOT / "inbox"
LIBRARY = ROOT / "library"
EPISODES = ROOT / "episodes"
ASSETS = ROOT / "assets"
DATA = ROOT / "data"


def config_path() -> Path:
    env = os.environ.get("BABYREEL_CONFIG")
    return Path(env) if env else CONFIG_PATH


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    path = config_path()
    if path.suffix == ".json":
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    with path.open("rb") as f:
        return tomllib.load(f)


def use_profile(path: Path | None) -> None:
    """이후 load() 가 이 프로필을 읽게 한다 (None = 기본 channel.toml)."""
    if path is None:
        os.environ.pop("BABYREEL_CONFIG", None)
    else:
        os.environ["BABYREEL_CONFIG"] = str(path)
    load.cache_clear()


def children() -> list[dict]:
    return load().get("children", [])


def age_of(birth: str, on: date) -> str | None:
    """만 나이 표기: 100일 전 'D+45', 36개월 전 '14개월', 그 뒤 '4살'."""
    if not birth:
        return None
    b = date.fromisoformat(birth)
    days = (on - b).days
    if days < 0:
        return None
    if days < 100:
        return f"D+{days}"
    months = (on.year - b.year) * 12 + on.month - b.month - (on.day < b.day)
    if months < 36:
        return f"{months}개월"
    return f"{months // 12}살"


def ages(on: date, cast: list[str] | None = None, since: date | None = None) -> str:
    """'첫째 4살 · 둘째 8개월'. cast 가 있으면 그 아이들만, 생일이 없는 아이는 빠진다.

    since 를 주면 그 영상에 담긴 기간으로 '첫째 D+57~63' (여러 날 찍은 영상을 한 날 나이로 적지 않게).
    """
    parts = []
    for c in children():
        if cast and c["nick"] not in cast:
            continue
        a = age_of(c.get("birth_date", ""), on)
        if not a:
            continue
        first = age_of(c.get("birth_date", ""), since) if since and since < on else None
        if first and first != a:
            # 같은 단위면 숫자만 이어서: D+57~63, 3~4개월 / 단위가 바뀌면 그대로: D+95~3개월
            m1, m2 = re.match(r"(D\+)?(\d+)(\D*)$", first), re.match(r"(D\+)?(\d+)(\D*)$", a)
            if m1 and m2 and (m1[1], m1[3]) == (m2[1], m2[3]):
                a = f"{m1[1] or ''}{m1[2]}~{m2[2]}{m2[3]}"
            else:
                a = f"{first}~{a}"
        parts.append(f"{c['nick']} {a}")
    return " · ".join(parts)


def shot_date(src: str) -> date | None:
    """library 파일 이름 앞의 촬영 날짜 (2024-12-10_IMG_7225.MOV)."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})_", Path(src).name)
    try:
        return date.fromisoformat(m[1]) if m else None
    except ValueError:
        return None


def series_label(key: str) -> str:
    return load().get("series", {}).get(key, "")


def resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p
