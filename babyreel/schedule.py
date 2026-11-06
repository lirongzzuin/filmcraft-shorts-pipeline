"""업로드 일정: 쇼츠는 매일, 롱폼은 이틀에 한 번 (docs/FEEDBACK.md 2026-10-17).

한 번 만들면 쇼츠 1부·2부 + 롱폼 1편 = 이틀치. 일정은 이렇게 잡는다 ([schedule] long_day):
  long_day = "short2" (기본, 2026-10-29 사용자 실제 방식): 1일차 쇼츠 1부, 2일차 쇼츠 2부 + 롱폼
  long_day = "short"  : 1일차 쇼츠 1부 + 롱폼, 2일차 쇼츠 2부
  다음 회차는 그다음 날부터 이어서 (쇼츠 재고가 쌓이면 뒤로 밀린다). 올리는 시각은 [schedule] upload_time
  실제 공개와 어긋나면 `./br schedule sync` (채널에서 공개 영상을 읽어 실제 날짜로 고정하고 남은 것을 다시 잡음)

data/schedule.json  {"<episode id>": {"short": "2026-10-18", "short2": "2026-10-19", "long": "2026-10-18"}}
- publish 때 처음 한 번 정하고 다시 바꾸지 않는다 (업로드방법 txt 첫 줄 = 올릴 날짜)
- 간격은 config/channel.toml [schedule] short_every_days / long_every_days
- 회차 폴더들(episodes/, SSD 의 episodes/) 맨 위에 '업로드일정.txt' 를 만들어 오늘 올릴 것을 본다
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from . import config
from .config import DATA
from .plan import SHORT_TARGETS, Plan

PATH = DATA / "schedule.json"


def _path() -> Path:
    """BABYREEL_SCHEDULE 로 바꿀 수 있다 (selftest 가 사용자의 실제 일정을 건드리지 않게)."""
    return Path(os.environ["BABYREEL_SCHEDULE"]) if os.environ.get("BABYREEL_SCHEDULE") else PATH
WEEKDAYS = "월화수목금토일"


def load() -> dict[str, dict[str, str]]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _rules() -> tuple[int, int]:
    sc = config.load().get("schedule", {})
    return int(sc.get("short_every_days", 1)), int(sc.get("long_every_days", 2))


def _long_day() -> str:
    return config.load().get("schedule", {}).get("long_day", "short2")


def upload_time() -> str:
    """매일 같은 시각에 올린다 (구독자 알림·추천이 그 시각 시청 패턴에 맞춰짐). 예: "18:00" """
    return config.load().get("schedule", {}).get("upload_time", "")


def _last(state: dict, kinds: tuple[str, ...], skip: str = "") -> date | None:
    days = [date.fromisoformat(v) for ep, s in state.items() if ep != skip for k, v in s.items() if k in kinds]
    return max(days) if days else None


def assign(plan: Plan, today: date | None = None) -> dict[str, date]:
    """이 회차의 영상별 업로드 날짜. 이미 정해졌으면 그대로 (새로 생긴 영상만 추가로 정한다)."""
    today = today or date.today()
    state = load()
    mine = state.get(plan.id, {})
    short_gap, long_gap = _rules()
    videos = [t for t in plan.shorts()] + (["long"] if plan.outputs.get("long") else [])

    nxt = None
    for v in [t for t in SHORT_TARGETS if t in videos]:
        if v in mine:
            nxt = date.fromisoformat(mine[v]) + timedelta(days=short_gap)
            continue
        if nxt is None:
            last = _last(state, SHORT_TARGETS, skip=plan.id)
            nxt = max(today, last + timedelta(days=short_gap)) if last else today
        mine[v] = nxt.isoformat()
        nxt += timedelta(days=short_gap)
    if "long" in videos and "long" not in mine:
        anchor = _long_anchor(mine) or today
        last_long = _last(state, ("long",), skip=plan.id)
        day = anchor if not last_long else max(anchor, last_long + timedelta(days=long_gap))
        mine["long"] = day.isoformat()
    state[plan.id] = mine
    save(state)
    write_board()
    return {k: date.fromisoformat(v) for k, v in mine.items()}


def _long_anchor(videos: dict[str, str]) -> date | None:
    """롱폼을 붙일 쇼츠 날짜: 기본은 2부 날 (없으면 1부 날)."""
    key = "short2" if _long_day() == "short2" and "short2" in videos else "short"
    return date.fromisoformat(videos[key]) if key in videos else None


def rebase(start: date, published: dict[tuple[str, str], date] | None = None) -> list[tuple[str, str, str, str]]:
    """일정 다시 잡기. 실제로 공개된 영상은 공개일로 고정, start 이전 날짜도 그대로 두고,
    나머지는 회차 순서대로 start 부터 쇼츠 매일 · 롱폼은 규칙(long_day, long_every_days)대로.
    [(에피소드, 영상, 이전 날짜, 새 날짜)] 바뀐 것만 돌려준다."""
    published = published or {}
    state = load()
    before = {ep: dict(v) for ep, v in state.items()}
    for (ep, video), d in published.items():
        if ep in state:
            state[ep][video] = d.isoformat()
    short_gap, long_gap = _rules()

    def fixed(ep: str, v: str) -> bool:
        return (ep, v) in published or date.fromisoformat(state[ep][v]) < start

    order = sorted(state, key=lambda e: (min(before[e].values()), e))  # 기존 순서 유지
    day = start
    longs = [date.fromisoformat(state[e]["long"]) for e in state if "long" in state[e] and fixed(e, "long")]
    last_long = max(longs) if longs else None
    for ep in order:
        vids = state[ep]
        for v in [t for t in SHORT_TARGETS if t in vids]:
            if fixed(ep, v):
                continue
            vids[v] = day.isoformat()
            day += timedelta(days=short_gap)
        if "long" in vids and not fixed(ep, "long"):
            d = _long_anchor(vids) or day
            if last_long:
                d = max(d, last_long + timedelta(days=long_gap))
            vids["long"] = d.isoformat()
            last_long = d
    save(state)
    write_board()
    return [(ep, v, before[ep].get(v, ""), d) for ep, vids in state.items() for v, d in vids.items()
            if before[ep].get(v) != d]


def day_of(plan: Plan, video: str) -> date | None:
    v = load().get(plan.id, {}).get(video)
    return date.fromisoformat(v) if v else None


def schedule_label(day: str) -> str:
    return label(date.fromisoformat(day))


def label(d: date) -> str:
    return f"{d.isoformat()}({WEEKDAYS[d.weekday()]})"


def upcoming(start: date | None = None, days: int = 14) -> list[tuple[date, str, str, str]]:
    """[(날짜, 에피소드, 영상, 제목)] start 부터 days 일 동안 + 그 전에 밀린 것."""
    from .config import EPISODES
    from .publish import load_state

    start = start or date.today()
    rows = []
    for ep, videos in load().items():
        d = EPISODES / ep
        title = titles(ep)
        done = load_state(d) if d.exists() else {}
        for video, day in videos.items():
            day = date.fromisoformat(day)
            uploaded = any(v.get("status") == "uploaded" and _video_of(t) == video for t, v in done.items())
            if day > start + timedelta(days=days) or (day < start and uploaded):
                continue
            rows.append((day, ep, video, title.get(video, "")))
    return sorted(rows)


def titles(ep: str) -> dict[str, str]:
    """{영상: 유튜브 제목} (일정판·날짜순 업로드 폴더 이름)."""
    from .config import EPISODES

    d = EPISODES / ep
    if not (d / "plan.json").exists():
        return {}
    p = Plan.load(d)
    return {"short": p.youtube_title, "long": p.youtube_long_title or p.youtube_title,
            "short2": p.short2.youtube_title if p.short2 else ""}


def uploaded(ep: str, video: str) -> bool:
    """이 영상이 실제로 올라갔는지 (publish 기록: ./br published 또는 ./br schedule sync 로 채널에서 확인)."""
    from .config import EPISODES
    from .publish import load_state

    d = EPISODES / ep
    done = load_state(d) if d.exists() else {}
    return any(v.get("status") == "uploaded" and _video_of(t) == video and t.startswith("youtube") for t, v in done.items())


def _video_of(target: str) -> str:
    if target == "youtube_long":
        return "long"
    return "short2" if target.endswith("_2") else "short"


def stock(today: date | None = None) -> tuple[int, int]:
    """오늘 이후로 준비된 쇼츠·롱폼 수 (재고)."""
    today = today or date.today()
    s = l = 0
    for _, videos in load().items():
        for k, v in videos.items():
            if date.fromisoformat(v) >= today:
                if k == "long":
                    l += 1
                else:
                    s += 1
    return s, l


def today_text(today: date | None = None) -> str:
    """오늘 올릴 것 (알림·./br today)."""
    today = today or date.today()
    rows = [r for r in upcoming(today, 0) if r[0] <= today]
    if not rows:
        s, _ = stock(today)
        return "오늘 올릴 영상 없음" + ("" if s else " · 쇼츠 재고 0개: 촬영 폴더에 영상을 넣어 주세요")
    late = [r for r in rows if r[0] < today]
    from .deliver import LABELS

    parts = [f"{LABELS[v]} '{t[:24]}'" + (" (밀림)" if d < today else "") for d, _, v, t in rows]
    return "오늘 올릴 영상: " + ", ".join(parts) + (f" · 밀린 것 {len(late)}개" if late else "")


def _link_check() -> list[str]:
    """공개 지표(data/public_videos.json)로 본 '쇼츠→롱폼 연결 확인' 목록. 실패하면 빈 목록."""
    from . import channel, stats

    try:
        videos = channel.cached()
        return stats.alerts(stats.analyze(videos, channel.match(videos))) if videos else []
    except Exception:  # noqa: BLE001  (일정판은 항상 써져야 한다)
        return []


def write_board(today: date | None = None) -> Path | None:
    """'업로드일정.txt': episodes/ 맨 위(Mac·SSD) + SSD 업로드/ 맨 위. 줄마다 올릴 폴더 위치(업로드/ 안 또는 episodes/<회차>/<구분>)."""
    from . import deliver

    today = today or date.today()
    short_gap, long_gap = _rules()
    s, l = stock(today)
    ordered = deliver.order_dir() is not None
    lines = [
        f"[업로드 일정] 숏폼 매일 · 롱폼 이틀에 한 번 ({'숏폼2와 같은 날' if _long_day() == 'short2' else '숏폼1과 같은 날'})"
        + (f" · 매일 {upload_time()} 예약" if upload_time() else ""),
        f"숏폼 = YouTube 쇼츠 + 인스타 릴스 + 틱톡 (같은 날 세 곳)  ·  재고: 숏폼 {s}개, 롱폼 {l}개",
        ("올릴 폴더: 외장 SSD filmcraft/업로드/ 안의 회차 폴더(이름순 = 올릴 순서) → 숏폼1·숏폼2·롱폼 폴더 "
         "(올린 것은 이름 끝에 '올림', 회차를 다 올리면 '올린 영상' 안으로)."
         if ordered else "올릴 폴더: episodes/<회차>/숏폼1·숏폼2·롱폼 (줄 끝의 → 위치).")
        + " 폴더 안 '유튜브_·인스타_·틱톡_업로드방법.txt' 대로",
        "",
    ]
    for d, ep, video, title in upcoming(today - timedelta(days=7), 30):
        done = uploaded(ep, video)
        mark = "✓ 올림" if done else "← 오늘" if d == today else ("(지남, 아직 안 올림)" if d < today else "")
        lines.append(f"{label(d)}  {deliver.LABELS[video]:5s} {title}  {mark}".rstrip())
        if not done:
            where = deliver.order_path(ep, video) if ordered else f"{ep}/{deliver.LABELS[video]}"
            lines.append(f"{'':16s}  → {where}")
        if video == "long" and (not done or today - d <= timedelta(days=2)):
            # 롱폼은 공개 전(예약 = 비공개)엔 쇼츠의 '관련 동영상'으로 못 고른다 → 공개된 뒤 1부·2부에 연결
            # (공개돼 '✓ 올림'이 된 뒤가 바로 할 때라서, 공개 후 이틀까지 남긴다)
            lines.append(f"{'':16s}  └ 할 일: 롱폼 공개 후(18시 이후), 이 회차 숏폼1·숏폼2의 '관련 동영상'을 이 롱폼으로")
    check = _link_check()
    if check:
        lines += ["", "[확인 필요] 롱폼 조회가 쇼츠보다 너무 적어요 → '고급 기능'을 연 뒤 '쇼츠-롱폼 연결 목록.txt' 대로 연결"]
        lines += [f"  · {a}" for a in check]
    path = None
    for root in deliver.board_dirs():  # Mac 의 episodes/ 맨 위 + SSD 의 업로드/·episodes/ 맨 위
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / deliver.BOARD).write_text("\n".join(lines) + "\n", encoding="utf-8")
            write_links(root)
        except OSError:
            continue
        path = path or root / deliver.BOARD
    return path


def link_rows() -> list[tuple[str, str, list[tuple[str, str]]]]:
    """[(롱폼 제목, 롱폼 링크, [(쇼츠 이름, 쇼츠 Studio 편집 링크)])] 공개 링크가 기록된 회차만 (schedule sync 가 기록)."""
    from .config import EPISODES
    from .publish import load_state

    rows = []
    for d in sorted(EPISODES.iterdir()):
        if d.name.startswith("_") or not (d / "plan.json").exists():
            continue
        st = load_state(d)
        long = (st.get("youtube_long") or {}).get("url")
        meta = d / "metadata.json"
        if not long or not meta.exists():
            continue
        title = json.loads(meta.read_text(encoding="utf-8")).get("youtube_long", {}).get("title", d.name)
        shorts = [(name, "https://studio.youtube.com/video/" + url.rstrip("/").split("/")[-1] + "/edit")
                  for target, name in (("youtube_shorts", "쇼츠 1부"), ("youtube_shorts_2", "쇼츠 2부"))
                  if (url := (st.get(target) or {}).get("url"))]
        if shorts:
            rows.append((title, long, shorts))
    return rows


def write_links(root: Path) -> Path | None:
    """episodes/ 맨 위 '쇼츠-롱폼 연결 목록.txt': 쇼츠마다 '관련 동영상'으로 고를 롱폼 (docs/ALGORITHM.md 1-1)."""
    rows = link_rows()
    if not rows:
        return None
    lines = ["[쇼츠 → 롱폼 연결 목록]  (YouTube '고급 기능'이 열려야 '관련 동영상' 칸이 보입니다)",
             "쇼츠 링크를 컴퓨터에서 열기 → 세부정보 → 관련 동영상 → 아래 롱폼 선택 → 저장. 공개 뒤에도 바꿀 수 있어요.", ""]
    for title, long, shorts in rows:
        lines.append(f"■ {title}\n  롱폼: {long}")
        lines += [f"  {name}: {link}" for name, link in shorts]
        lines.append("")
    path = root / "쇼츠-롱폼 연결 목록.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
