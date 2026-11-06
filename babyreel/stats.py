"""공개 지표 기록·분석 (API 키 없이, 2026-11-01 사용자 요청 "쌓이는 데이터를 분석해서 개선 + 용량 최소화").

`./br schedule sync`(매일 auto.sh 의 --check 포함)가 채널 공개 페이지를 읽을 때 조회수·좋아요도 함께 읽어
data/views.json 에 영상마다 몇 개의 숫자만 남긴다:
  {"영상 id": {"k": "s"|"l", "p": "공개 시각", "h": [[공개 후 날짜, 조회수, 좋아요], ...]}}
용량 최소화: 공개 후 0·1·2·3·7·14·30·60·90·180·365일째 값과 마지막 값만 남긴다 (영상 1개당 ~150바이트,
영상 1,000개여도 200KB 이하). 날짜별 스냅샷 파일은 만들지 않는다.

분석 결과는 docs/INSIGHTS.md 의 '공개 지표' 절 (Claude 가 다음 편 기획 전에 읽음).
시청 지속·클릭률·시청 시간은 공개 페이지에 없다 → OAuth 분석(./br insights login)이 필요.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone

from .config import DATA
from .fc import ROOT

HISTORY = DATA / "views.json"
DOC = ROOT / "docs" / "INSIGHTS.md"
MARK = "<!-- 공개 지표 (babyreel/stats.py) -->"
KEEP_DAYS = {0, 1, 2, 3, 7, 14, 30, 60, 90, 180, 365}
SETTLE = 3  # 이 날 수 이상 지난 영상만 비교에 쓴다 (첫날은 아직 노출 중)
MIN_N = 6  # 비교 표본 수가 이보다 적으면 '경향'만
LONG_LINK_RATIO = 0.01  # 롱폼 조회가 그 회차 쇼츠 조회의 1% 미만이면 쇼츠→롱폼 연결 확인
KST = timezone(timedelta(hours=9))
LAST: dict = {}  # 마지막 분석 결과 (schedule sync 가 경고 출력에 씀)


def load() -> dict:
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _thin(h: list[list[int]]) -> list[list[int]]:
    return [p for i, p in enumerate(h) if p[0] in KEEP_DAYS or i == len(h) - 1]


def record(videos: list[dict], now: datetime | None = None) -> dict:
    """공개 영상들의 오늘 조회수·좋아요를 기록 (같은 날짜 칸은 덮어씀)."""
    hist = load()
    for v in videos:
        if v.get("views") is None:
            continue
        now_ = now or datetime.now(v["published"].tzinfo)
        age = max(0, (now_ - v["published"]).days)
        e = hist.setdefault(v["id"], {"k": "l" if v["kind"] == "long" else "s",
                                      "p": v["published"].isoformat(timespec="minutes"), "h": []})
        point = [age, v["views"], v.get("likes") or 0]
        e["h"] = _thin([p for p in e["h"] if p[0] != age] + [point])
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return hist


def at_day(entry: dict, day: int) -> int | None:
    """공개 후 day 일째 조회수 (그날 기록이 없으면 그 이전 가장 가까운 값, 없으면 None)."""
    pts = [p for p in entry.get("h", []) if p[0] <= day]
    return pts[-1][1] if pts and entry["h"][-1][0] >= day else None


def _med(xs):
    return statistics.median(xs) if xs else 0


def analyze(videos: list[dict], found: dict[tuple[str, str], dict], now: datetime | None = None) -> dict:
    hist = load()
    now = now or datetime.now(KST)
    owner = {v["id"]: (ep, video) for (ep, video), v in found.items()}
    age = {v["id"]: (now - v["published"]).days for v in videos}
    shorts = [v for v in videos if v["kind"] == "short" and v.get("views") is not None]
    longs = [v for v in videos if v["kind"] == "long" and v.get("views") is not None]
    settled = [v for v in shorts if age[v["id"]] >= SETTLE]
    med = _med([v["views"] for v in settled])

    def crowded(v):  # 12시간 안에 다른 쇼츠가 공개됨 → 첫 노출을 나눠 가짐
        return any(o is not v and abs((o["published"] - v["published"]).total_seconds()) < 12 * 3600 for o in shorts)

    hits = [v for v in settled if med and v["views"] >= 2 * med]
    weak = [v for v in settled if med and v["views"] <= 0.5 * med]
    # 회차별 깔때기: 쇼츠 1·2부 → 롱폼
    eps: dict[str, dict] = {}
    for vid, (ep, video) in owner.items():
        eps.setdefault(ep, {})[video] = next(v for v in videos if v["id"] == vid)
    funnel, link_check = [], []
    for ep, d in sorted(eps.items(), key=lambda kv: min(v["published"] for v in kv[1].values())):
        s = sum(d[k]["views"] or 0 for k in ("short", "short2") if k in d)
        lv = d.get("long")
        ratio = (lv["views"] / s) if (lv and s) else None
        funnel.append((ep, d, s, ratio))
        if lv and ratio is not None and ratio < LONG_LINK_RATIO and age[lv["id"]] >= 2:
            link_check.append((ep, lv, s))
    # 첫날 → 7일 성장 (기록이 쌓이면)
    growth = []
    for v in settled:
        e = hist.get(v["id"], {})
        d1, d7 = at_day(e, 1), at_day(e, 7)
        if d1 and d7:
            growth.append(d7 / d1)
    # 공개 시각별 (각 MIN_N 개 이상일 때만)
    by_hour: dict[int, list[int]] = {}
    for v in settled:
        if not crowded(v):
            by_hour.setdefault(v["published"].hour, []).append(v["views"])
    like_rate = [(v["likes"] or 0) / v["views"] for v in settled if v["views"]]
    return {"shorts": shorts, "longs": longs, "settled": settled, "median": med, "hits": hits, "weak": weak,
            "crowded": {v["id"] for v in shorts if crowded(v)}, "funnel": funnel, "link_check": link_check,
            "growth": growth, "by_hour": by_hour, "like_rate": _med(like_rate), "age": age, "owner": owner,
            "date": now.date().isoformat()}


def lines(r: dict) -> list[str]:
    out = [MARK, "", f"## 공개 지표 (채널 공개 페이지, {r['date']} · API 없이 매일 자동 기록: data/views.json)", ""]
    out.append(f"- 공개 쇼츠 {len(r['shorts'])}개 · 롱폼 {len(r['longs'])}개. 공개 {SETTLE}일 이상 쇼츠 {len(r['settled'])}개의 "
               f"조회수 중앙값 **{r['median']:,.0f}** · 좋아요율 중앙값 {r['like_rate'] * 100:.1f}%")
    if len(r["settled"]) < MIN_N:
        out.append(f"- 표본 {len(r['settled'])}개 (<{MIN_N}) → 경향만 참고, 규칙은 바꾸지 않는다")
    out += ["", "### 확인 필요", ""]
    todo = [f"- ⚠ {_short_title(lv)} 롱폼 조회 {lv['views']:,} (그 회차 쇼츠 {s:,}의 {lv['views'] / s * 100:.1f}%) "
            f"→ 쇼츠 1부·2부의 '관련 동영상'이 이 롱폼으로 연결됐는지 YouTube Studio 에서 확인"
            for ep, lv, s in r["link_check"]]
    out += todo or ["- 없음"]
    out += ["", "### 회차별 흐름 (쇼츠 → 롱폼)", "", "| 회차 | 쇼츠 1부 | 쇼츠 2부 | 롱폼 | 롱폼/쇼츠 |", "|---|---|---|---|---|"]
    for ep, d, s, ratio in r["funnel"]:
        cell = lambda k: f"{d[k]['views']:,}" if k in d else "-"  # noqa: E731
        out.append(f"| {ep} | {cell('short')} | {cell('short2')} | {cell('long')} | "
                   f"{'-' if ratio is None else f'{ratio * 100:.1f}%'} |")
    out += ["", "### 잘 된 쇼츠 (중앙값의 2배 이상) → 제목·첫 장면 패턴 참고", ""]
    out += [f"- {v['views']:,}회 · 좋아요 {v['likes'] or 0} · {v['seconds'] or '?'}초 · {v['title']}"
            for v in sorted(r["hits"], key=lambda v: -v["views"])] or ["- 아직 없음"]
    out += ["", "### 덜 된 쇼츠 (중앙값의 절반 이하)", ""]
    out += [f"- {v['views']:,}회 · {v['title']}" + (" · 같은 날 다른 쇼츠와 겹침(첫 노출을 나눠 가짐)" if v["id"] in r["crowded"] else "")
            for v in sorted(r["weak"], key=lambda v: v["views"])] or ["- 없음"]
    out += ["", "### 경향", ""]
    if r["growth"]:
        out.append(f"- 쇼츠 7일째/1일째 조회 중앙값 ×{_med(r['growth']):.2f} ({len(r['growth'])}개) "
                   "")
    else:
        out.append("- 쇼츠 1일째→7일째 성장: 기록이 쌓이는 중 (공개 7일 뒤부터 계산)")
    hours = {h: xs for h, xs in r["by_hour"].items() if len(xs) >= MIN_N}
    if len(hours) >= 2:
        out += [f"- {h}시 공개: 조회 중앙값 {_med(xs):,.0f} ({len(xs)}개)" for h, xs in sorted(hours.items())]
    else:
        out.append(f"- 공개 시각 비교: 시각마다 {MIN_N}개 이상 쌓이면 표시")
    if r["longs"]:
        out.append(f"- 롱폼 조회 중앙값 {_med([v['views'] for v in r['longs']]):,.0f} · 쇼츠는 수익 창출 시청 시간에 안 들어감 "
                   "→ 쇼츠→롱폼 연결이 가장 큰 숙제")
    out += ["", "> 시청 지속·이탈 지점·클릭률은 공개 페이지에 없다 → ./br insights login (docs/AUTOMATION.md 2-3)"]
    return out


def write_doc(videos: list[dict], found: dict) -> dict:
    """docs/INSIGHTS.md 의 '공개 지표' 절만 바꾼다 (OAuth 분석이 쓴 앞부분은 그대로)."""
    r = analyze(videos, found)
    LAST.clear()
    LAST.update(r)
    try:
        head = DOC.read_text(encoding="utf-8").split(MARK)[0].rstrip()
    except OSError:
        head = ""
    if not head:
        head = ("# 성과 인사이트 (자동 생성)\n\n"
                "Claude 는 다음 편 기획 전에 이 문서를 읽는다. 사용자 피드백 > 이 수치. 표본 수가 적으면 경향만 참고.")
    DOC.write_text(head + "\n\n" + "\n".join(lines(r)) + "\n", encoding="utf-8")
    return r


def alerts(r: dict) -> list[str]:
    return [f"{_short_title(lv)}: 롱폼 {lv['views']:,}회 · 쇼츠 {s:,}회({lv['views'] / s * 100:.1f}%)"
            for ep, lv, s in r["link_check"]]


def _short_title(v: dict) -> str:
    """'제목 | 육아일지 No.N' → '육아일지 No.N (제목 앞부분)'."""
    head, _, tail = v["title"].partition("|")
    return f"{tail.strip()} ({head.strip()[:18]}…)" if tail else v["title"][:30]
