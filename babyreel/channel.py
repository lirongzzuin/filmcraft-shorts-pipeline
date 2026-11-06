"""공개 채널 읽기: API 키·로그인 없이 채널 페이지에서 공개된 영상(제목·공개 시각·링크)을 읽는다.

용도 (2026-10-29 사용자 요청 "이미 올린 영상과 날짜가 어긋나는 폴더를 전체 점검"):
- 어떤 회차의 어떤 영상(쇼츠 1부·2부·롱폼)이 실제로 언제 공개됐는지 → 업로드 일정을 실제에 맞춰 다시 잡기
- 공개 링크를 publish 기록에 남겨 → 쇼츠 설명의 '전체 이야기(롱폼) 링크', 연재 '지난 편' 링크가 자동으로 채워짐

한계: 예약(비공개) 상태 영상은 채널 페이지에 안 보인다 → 공개된 것만 확인된다.
"""

from __future__ import annotations

import difflib
import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .config import EPISODES

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15",
      "Accept-Language": "ko-KR,ko;q=0.9"}


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def handle() -> str:
    return config.load()["channel"]["handle"]


def public_videos() -> list[dict]:
    """[{id, title, kind: 'short'|'long', published: datetime(KST), url}] 최신순."""
    base = f"https://www.youtube.com/{handle()}"
    ids: dict[str, str] = {}
    for tab, kind in (("shorts", "short"), ("videos", "long")):
        for vid in re.findall(r'"videoId":"([\w-]{11})"', _get(f"{base}/{tab}")):
            ids.setdefault(vid, kind)
    out = []
    for vid, kind in ids.items():
        html = _get(f"https://www.youtube.com/watch?v={vid}")
        title = re.search(r'<meta name="title" content="([^"]*)"', html)
        when = re.search(r'"publishDate":"([^"]+)"', html)
        if not (title and when):
            continue
        import html as H

        num = lambda pat: int(m.group(1)) if (m := re.search(pat, html)) else None  # noqa: E731
        out.append({
            "id": vid, "kind": kind, "title": H.unescape(title.group(1)),
            "published": datetime.fromisoformat(when.group(1)).astimezone(KST),
            "url": f"https://youtube.com/shorts/{vid}" if kind == "short" else f"https://youtu.be/{vid}",
            "views": num(r'"viewCount":"(\d+)"'), "likes": num(r'"likeCount":"?(\d+)'),
            "seconds": num(r'"lengthSeconds":"(\d+)"'),
        })
    return sorted(out, key=lambda v: v["published"], reverse=True)


def _norm(t: str) -> str:
    """비교용: 공백·ㅋ·문장부호·'| 육아일지 No.N' 꼬리를 뺀다 (올릴 때 제목을 조금 고쳐도 맞춰지게)."""
    t = t.split("|")[0]
    return re.sub(r"[\sㅋㅎ.,!?·~()\[\]'\"-]", "", t).lower()


def episode_titles() -> list[tuple[str, str, str]]:
    """[(에피소드, 영상, 제목)] 모든 회차의 쇼츠 1부·2부·롱폼 제목."""
    from .plan import Plan

    rows = []
    for d in sorted(EPISODES.iterdir()):
        if d.name.startswith("_") or not (d / "plan.json").exists():
            continue
        p = Plan.load(d)
        rows.append((d.name, "short", p.youtube_title))
        if p.short2 and p.short2.youtube_title:
            rows.append((d.name, "short2", p.short2.youtube_title))
        if p.youtube_long_title:
            rows.append((d.name, "long", p.youtube_long_title))
    return rows


TARGETS = {"short": "youtube_shorts", "short2": "youtube_shorts_2", "long": "youtube_long"}


def known_ids() -> dict[str, tuple[str, str]]:
    """이미 기록된 공개 링크의 영상 id → (에피소드, 영상). 올린 뒤 YouTube 에서 제목을 바꿔도 이걸로 찾는다."""
    from .publish import load_state

    out = {}
    for d in sorted(EPISODES.iterdir()):
        if d.name.startswith("_") or not (d / "plan.json").exists():
            continue
        st = load_state(d)
        for video, target in TARGETS.items():
            url = (st.get(target) or {}).get("url") or ""
            if "youtu" in url:
                out[url.rstrip("/").split("/")[-1].split("=")[-1]] = (d.name, video)
    return out


def match(videos: list[dict], min_ratio: float = 0.82) -> dict[tuple[str, str], dict]:
    """공개 영상 ↔ (에피소드, 영상). 이미 기록된 영상 id 가 먼저, 나머지는 제목이 가장 비슷한 것끼리
    (쇼츠는 쇼츠끼리·롱폼은 롱폼끼리)."""
    out, used = {}, set()
    for vid, key in known_ids().items():
        v = next((x for x in videos if x["id"] == vid), None)
        if v and key not in out:
            out[key] = v
            used.add(vid)
    cands = episode_titles()
    pairs = []
    for v in videos:
        for ep, video, title in cands:
            if (video == "long") != (v["kind"] == "long"):
                continue
            r = difflib.SequenceMatcher(None, _norm(v["title"]), _norm(title)).ratio()
            if r >= min_ratio:
                pairs.append((r, v["id"], ep, video))
    for r, vid, ep, video in sorted(pairs, reverse=True):
        if vid in used or (ep, video) in out:
            continue
        used.add(vid)
        out[(ep, video)] = next(v for v in videos if v["id"] == vid)
    return out


def sync(dry_run: bool = False) -> tuple[dict[tuple[str, str], dict], list[dict]]:
    """공개 영상을 회차에 맞추고, 공개 링크를 publish 기록에 남긴다. (맞춘 것, 못 맞춘 공개 영상)"""
    from .publish import PublishResult, load_state, record

    videos = public_videos()
    found = match(videos)
    unmatched = [v for v in videos if v["id"] not in {x["id"] for x in found.values()}]
    if not dry_run:
        for (ep, video), v in found.items():
            target = TARGETS[video]
            d = EPISODES / ep
            if (load_state(d).get(target) or {}).get("url") != v["url"]:
                record(d, ep, PublishResult(target=target, status="uploaded", url=v["url"], detail="channel-sync"))
        cache = config.DATA / "public_videos.json"
        cache.write_text(json.dumps([{**v, "published": v["published"].isoformat()} for v in videos],
                                    ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        from . import stats

        stats.record(videos)
        stats.write_doc(videos, found)
    return found, unmatched


def cached() -> list[dict]:
    p: Path = config.DATA / "public_videos.json"
    try:
        return [{**v, "published": datetime.fromisoformat(v["published"])} for v in json.loads(p.read_text())]
    except (OSError, ValueError):
        return []
