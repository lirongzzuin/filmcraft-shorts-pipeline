"""연재 기록부: episodes/*/plan.json → docs/SERIES.md

편마다 썸네일·영상·내용이 이어지도록, 지금까지의 육아일지(번호·이야기 날짜·제목·시리즈·등장인물·
요약·반복 개그·다음 편 예고)를 한 문서로 모은다. Claude 는 새 편을 기획하기 전에 이 문서를 읽는다.
build/publish 때 자동으로 갱신된다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from . import config
from .config import EPISODES
from .fc import ROOT

SERIES_DOC = ROOT / "docs" / "SERIES.md"


def episodes() -> list[dict]:
    """육아일지 번호가 있는 에피소드 (테스트 '_' 제외), 번호순."""
    out = []
    for f in EPISODES.glob("*/plan.json"):
        if f.parent.name.startswith("_"):
            continue
        p = json.loads(f.read_text(encoding="utf-8"))
        if not p.get("episode_no"):
            continue
        pub = f.parent / "publish.json"
        p["_publish"] = json.loads(pub.read_text(encoding="utf-8")) if pub.exists() else {}
        p["_dir"] = f.parent.name
        out.append(p)
    return sorted(out, key=lambda p: p["episode_no"])


def previous(episode_no: int) -> dict | None:
    prev = [p for p in episodes() if p["episode_no"] < episode_no]
    return prev[-1] if prev else None


def url_of(p: dict, *targets: str) -> str | None:
    for t in targets:
        u = p.get("_publish", {}).get(t, {}).get("url")
        if u:
            return u
    return None


def write() -> Path:
    eps = episodes()
    lines = [
        "# 연재 기록부: 육아일지 (자동 생성)",
        "",
        "`./br build`·`./br publish`·`./br series` 때 episodes/*/plan.json 에서 자동으로 만들어진다. 직접 고치지 말 것.",
        "Claude 는 새 편을 기획하기 전에 이 문서를 읽고 **번호·시간 순서·등장인물·반복 개그·말투를 이어 간다** (CONCEPT.md 10번).",
        "",
        "| No. | 이야기 날짜 | 시리즈 | 등장 | 제목 | 업로드 |",
        "|---|---|---|---|---|---|",
    ]
    for p in eps:
        ages = config.ages(date.fromisoformat(p["date"]), p.get("cast") or None)
        up = "✅" if url_of(p, "youtube_shorts", "youtube_long", "instagram_reels") else "⬜"
        lines.append(f"| {p['episode_no']} | {p['date']} ({ages or '-'}) | {config.series_label(p.get('series', ''))} | "
                     f"{', '.join(p.get('cast', []))} | {p.get('youtube_title', '')} | {up} |")
    lines += ["", "## 편별 요약", ""]
    for p in eps:
        lines.append(f"### No.{p['episode_no']} {p.get('youtube_title', '')}")
        if p.get("story_notes"):
            lines.append(f"- 내용: {p['story_notes']}")
        if p.get("running_jokes"):
            lines.append(f"- 반복 개그: {', '.join(p['running_jokes'])}")
        if p.get("next_hook"):
            lines.append(f"- 다음 편 예고: {p['next_hook']}")
        lines.append("")
    jokes: list[str] = []
    for p in eps:
        jokes += [j for j in p.get("running_jokes", []) if j not in jokes]
    lines += ["## 채널 반복 개그 (다시 써먹을 것)", ""] + [f"- {j}" for j in jokes] + [""]
    if eps:
        last = eps[-1]
        lines += ["## 다음 편 기획 메모", "",
                  f"- 다음 번호: **No.{last['episode_no'] + 1}**",
                  f"- 직전 편: No.{last['episode_no']} {last.get('youtube_title', '')} (이야기 날짜 {last['date']})",
                  f"- 직전 편 예고: {last.get('next_hook') or '(없음)'}", ""]
    SERIES_DOC.write_text("\n".join(lines), encoding="utf-8")
    return SERIES_DOC
