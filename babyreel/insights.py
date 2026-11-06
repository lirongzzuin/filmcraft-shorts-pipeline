"""YouTube 성과 수집 → 편집 개선 (YouTube Data API v3 + YouTube Analytics API, 읽기 전용, 무료).

하는 일 (./br insights, 자동 실행 시 하루 한 번)
1. 채널에 올라간 영상 목록(Data API) ↔ 우리 에피소드 제목 대조 → 영상 링크 자동 기록 (쇼츠 설명란의 롱폼 링크로 쓰임)
2. 공개 점검: 올릴 날이 지났는데 비공개·일부공개인 영상 → 경고 (수익 창출 '유효 시청 시간' 0 의 가장 흔한 원인)
3. 수익 창출 진행: 공개 롱폼 시청 시간(최근 365일) / 공개 쇼츠 조회수(최근 90일) / 구독자 (추정치. 최종 기준은 Studio '수익 창출' 탭)
4. 영상별 성과: 조회수·평균 시청 비율·구독 전환 + 시청 지속 곡선(Analytics elapsedVideoTimeRatio)
5. 이탈 지점 → 우리 편집(build.json 구간·자막·장치)에 대응 → 어떤 편집 패턴에서 시청자가 빠지는지
6. 결과: docs/INSIGHTS.md (Claude 가 다음 편 기획 전에 읽음) + data/insights/<날짜>.json (최근 2개만, 조회수 추이는 data/views.json)

반영 지연: Analytics 수치는 보통 2~3일 늦게 확정되고, 수익 창출 탭은 그보다 더 늦을 수 있다.
그래서 공개 3일이 안 된 영상은 '집계 중'으로 표시하고 결론에 쓰지 않으며, 매일 다시 수집해 늦게 들어온 수치를 반영한다.
표본 수가 적을 때(성과 데이터가 있는 영상 6편 미만)는 '경향'만 보여 주고 규칙을 바꾸라고 권하지 않는다.

설정 (docs/AUTOMATION.md 2-3): Google Cloud 의 같은 OAuth 클라이언트 JSON 을 secrets/youtube_insights_client.json 으로 저장
→ ./br insights login (브라우저 로그인 1회). 업로드용 youtube_client_secret.json 과 파일을 나눠, API 감사 전에도
읽기 전용 분석은 켤 수 있게 한다 (업로드는 계속 폴더 방식).
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

from . import config
from .config import DATA, EPISODES
from .fc import ROOT

SECRETS = ROOT / "secrets"
CLIENT = SECRETS / "youtube_insights_client.json"
API_KEY = SECRETS / "youtube_api_key.txt"  # 간단 모드: OAuth 없이 공개 영상 정보만 (공개 상태·규정 점검·조회수)
TOKEN = SECRETS / "youtube_insights_token.json"
LOGIN = SECRETS / "youtube_insights_login.txt"  # 마지막 로그인 날짜 (테스트 상태 앱의 7일 만료 미리 알림)
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]
OUT_DIR = DATA / "insights"
DOC = ROOT / "docs" / "INSIGHTS.md"
SETTLE_DAYS = 3  # Analytics 수치가 거의 확정되는 데 걸리는 날
MIN_VIDEOS = 6
KEEP_SNAPSHOTS = 2  # 날짜별 스냅샷은 최근 2개만 (조회수 추이는 data/views.json 에 작게)  # 이보다 적으면 '경향'만
YPP_HOURS, YPP_SHORTS_VIEWS, YPP_SUBS = 4000, 10_000_000, 1000


# --- 인증·API ----------------------------------------------------------------------------

def configured() -> bool:
    return CLIENT.exists() or TOKEN.exists() or API_KEY.exists()


def mode() -> str:
    """oauth(전체: 시청 지속 곡선·시청 시간·비공개 영상까지) | key(간단: 공개 영상만) | none."""
    if TOKEN.exists():
        return "oauth"
    if API_KEY.exists():
        return "key"  # OAuth 로그인 전이면 키로 먼저 (로그인을 마치면 자동으로 전체 모드)
    return "oauth" if CLIENT.exists() else "none"


def key_service():
    from googleapiclient.discovery import build

    return build("youtube", "v3", developerKey=API_KEY.read_text().strip(), cache_discovery=False)


def services(interactive: bool = True):
    """(youtube Data API, youtubeAnalytics) 클라이언트. 토큰이 없으면(interactive 일 때만) 브라우저 로그인."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:  # noqa: BLE001
                if not interactive:
                    raise RuntimeError(f"invalid_grant: {e}") from e
                creds = None
        if creds and creds.valid:
            pass
        else:
            if not CLIENT.exists():
                raise RuntimeError(f"{CLIENT} 가 없습니다 (docs/AUTOMATION.md 2-3)")
            if not interactive:
                raise RuntimeError("로그인 필요 (invalid_grant/만료)")
            creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES).run_local_server(port=0)
        SECRETS.mkdir(exist_ok=True)
        TOKEN.write_text(creds.to_json())
    return (build("youtube", "v3", credentials=creds, cache_discovery=False),
            build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False))


def _iso_seconds(dur: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", dur or "")
    if not m:
        return 0.0
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def fetch_videos(yt, handle: str | None = None) -> tuple[dict, list[dict]]:
    """(채널 정보, 업로드 영상 목록). handle 이 있으면 공개 채널 조회(API 키 모드: 공개 영상만 보임)."""
    q = {"forHandle": handle} if handle else {"mine": True}
    ch = yt.channels().list(part="contentDetails,statistics,snippet", **q).execute()["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, token = [], None
    while True:
        r = yt.playlistItems().list(playlistId=uploads, part="contentDetails", maxResults=50,
                                    pageToken=token).execute()
        ids += [it["contentDetails"]["videoId"] for it in r.get("items", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    videos = []
    for i in range(0, len(ids), 50):
        r = yt.videos().list(id=",".join(ids[i:i + 50]), part="snippet,contentDetails,status,statistics").execute()
        for v in r.get("items", []):
            st = v.get("status", {})
            cd = v.get("contentDetails", {})
            videos.append({
                "id": v["id"], "title": v["snippet"]["title"],
                "published_at": v["snippet"].get("publishedAt", ""),
                "duration": _iso_seconds(cd.get("duration", "")),
                "privacy": st.get("privacyStatus", ""), "publish_at": st.get("publishAt"),
                "made_for_kids": st.get("madeForKids"),
                "upload_status": st.get("uploadStatus", ""), "rejection": st.get("rejectionReason"),
                "failure": st.get("failureReason"),
                "region_blocked": cd.get("regionRestriction", {}).get("blocked", []),
                "region_allowed": cd.get("regionRestriction", {}).get("allowed"),
                "age_restricted": cd.get("contentRating", {}).get("ytRating") == "ytAgeRestricted",
                "views_total": int(v.get("statistics", {}).get("viewCount", 0)),
                "likes_total": int(v.get("statistics", {}).get("likeCount", 0) or 0),
            })
    info = {"id": ch["id"], "title": ch["snippet"]["title"],
            "subscribers": int(ch["statistics"].get("subscriberCount", 0)),
            "hidden_subs": ch["statistics"].get("hiddenSubscriberCount", False)}
    return info, videos


def _query(yta, **kw) -> list[dict]:
    r = yta.reports().query(ids="channel==MINE", **kw).execute()
    names = [h["name"] for h in r.get("columnHeaders", [])]
    return [dict(zip(names, row)) for row in r.get("rows", []) or []]


def video_metrics(yta, ids: list[str], start: str, end: str) -> dict[str, dict]:
    # engagedViews: 2025-03-31 부터 쇼츠 views 는 반복 재생까지 센다. 수익 창출 기준은 engagedViews (2025-04-30 API 추가)
    base = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,shares,subscribersGained"
    out = {}
    for i in range(0, len(ids), 200):
        q = dict(startDate=start, endDate=end, dimensions="video", filters="video==" + ",".join(ids[i:i + 200]))
        try:
            rows = _query(yta, metrics=base + ",engagedViews", **q)
        except Exception:  # noqa: BLE001  (지표를 못 받는 경우: 예전 지표만)
            rows = _query(yta, metrics=base, **q)
        for row in rows:
            out[row["video"]] = row
    return out


def retention(yta, vid: str, start: str, end: str) -> list[tuple[float, float]]:
    """[(영상 진행 비율 0~1, 그 지점까지 남아 보는 비율)] (Analytics audienceWatchRatio)."""
    try:
        rows = _query(yta, startDate=start, endDate=end, dimensions="elapsedVideoTimeRatio",
                      metrics="audienceWatchRatio", filters=f"video=={vid}")
    except Exception:  # noqa: BLE001  (조회수가 적으면 데이터 없음)
        return []
    return sorted((float(r["elapsedVideoTimeRatio"]), float(r["audienceWatchRatio"])) for r in rows)


# --- 우리 에피소드와 대조 ---------------------------------------------------------------------

def _norm(t: str) -> str:
    t = unicodedata.normalize("NFC", t or "")
    t = re.sub(r"[\s\W_]+", "", t.split("|")[0])
    return t.lower()


def our_videos() -> list[dict]:
    """우리가 만든 업로드 대상 목록: 에피소드·대상·제목·업로드 예정일·편집 구간."""
    from . import schedule
    from .publish import video_of

    days = schedule.load()
    out = []
    for d in sorted(EPISODES.glob("*/metadata.json")):
        ep = d.parent
        if ep.name.startswith("_"):
            continue
        meta = json.loads(d.read_text(encoding="utf-8"))
        build = json.loads((ep / "build.json").read_text(encoding="utf-8")) if (ep / "build.json").exists() else {}
        for target in ("youtube_shorts", "youtube_shorts_2", "youtube_long"):
            m = meta.get(target)
            if not m:
                continue
            video = video_of(target)
            out.append({"episode": ep.name, "target": target, "video": video, "title": m["title"],
                        "upload_date": days.get(ep.name, {}).get(video),
                        "segments": build.get("outputs", {}).get(video, {}).get("segments", []),
                        "duration": build.get("outputs", {}).get(video, {}).get("duration")})
    return out


def match(ours: list[dict], videos: list[dict]) -> list[tuple[dict, dict]]:
    """제목으로 짝짓기 (앞부분이 같으면 같은 영상: 사람이 업로드하며 제목을 조금 고쳐도 대부분 맞음)."""
    from .channel import known_ids

    pairs, used = [], set()
    ids = {k: vid for vid, k in known_ids().items()}  # 이미 기록된 영상 id 먼저 (올린 뒤 제목을 바꿔도 맞음)
    by_id = {v["id"]: v for v in videos}
    for o in ours:
        v = by_id.get(ids.get((o.get("episode"), o.get("video")), ""))
        if v and v["id"] not in used:
            used.add(v["id"])
            pairs.append((o, v))
    done = {id(o) for o, _ in pairs}
    for o in ours:
        if id(o) in done:
            continue
        key = _norm(o["title"])
        best = None
        for v in videos:
            if v["id"] in used:
                continue
            vk = _norm(v["title"])
            if vk == key or (len(key) >= 8 and (vk.startswith(key[:12]) or key.startswith(vk[:12]))):
                # 쇼츠/롱폼 구분: 롱폼은 길이로 확인
                if (o["video"] == "long") == (v["duration"] > 75):
                    best = v
                    break
        if best:
            used.add(best["id"])
            pairs.append((o, best))
    return pairs


def record_links(pairs: list[tuple[dict, dict]]) -> int:
    """찾은 영상 링크를 publish.json 에 'uploaded' 로 기록 (이미 있으면 그대로)."""
    from .publish import PublishResult, load_state, record

    n = 0
    for o, v in pairs:
        ep = EPISODES / o["episode"]
        if (load_state(ep).get(o["target"]) or {}).get("url"):
            continue
        url = f"https://youtube.com/shorts/{v['id']}" if o["video"] != "long" else f"https://youtu.be/{v['id']}"
        record(ep, o["episode"], PublishResult(target=o["target"], status="uploaded", url=url, remote_id=v["id"],
                                               detail="insights 자동 기록"))
        n += 1
    return n


# --- 분석 ----------------------------------------------------------------------------------

def drops(curve: list[tuple[float, float]], duration: float, top: int = 3) -> list[tuple[float, float]]:
    """가장 많이 빠지는 구간 [(초, 이탈 폭)] (곡선 기울기가 가장 가파른 곳)."""
    out = []
    for (r0, w0), (r1, w1) in zip(curve, curve[1:]):
        if r1 > r0 and w1 < w0:
            out.append((r0 * duration, w0 - w1))
    return sorted(out, key=lambda x: -x[1])[:top]


def segment_at(segments: list[dict], t: float) -> dict | None:
    return next((s for s in segments if s["start"] <= t < s["end"]), None)


def _features(ep: str, seg: dict) -> dict:
    """그 구간의 편집 특징: 실제 대사·음소거·big 자막·효과 수·컷 길이."""
    from .plan import Plan

    try:
        plan = Plan.load(EPISODES / ep)
        c = plan.clips[seg["clip_index"]]
    except (OSError, IndexError, KeyError, ValueError):
        return {}
    return {"talk": any(x.style == "talk" for x in c.captions), "mute": c.mute,
            "big": any(x.style == "big" for x in c.captions),
            "effects": len(c.stickers) + len(c.sfx) + len(c.punch) + sum(1 for x in c.captions if x.sfx),
            "cut": round(seg["end"] - seg["start"], 1),
            "caption": " / ".join(x.text.replace("\n", " ")[:16] for x in c.captions[:2]),
            "src": Path(c.src).name}


def _one_scene(ep: str, video: str) -> bool | None:
    """쇼츠 첫 6초가 한 장면(같은 원본)인지 (검증기와 같은 기준)."""
    from .plan import Plan, _opening_warnings

    try:
        p = Plan.load(EPISODES / ep).part(video)
        p.fill_ends()
    except (OSError, KeyError, ValueError):
        return None
    return not any("장면이" in w for w in _opening_warnings([c for c in p.clips if "short" in c.targets]))


def login_alerts(today: date) -> list[str]:
    """테스트 상태 앱은 로그인 7일 뒤 끊긴다 → 5~6일째에 미리 알림 (8일째에도 잘 되면 프로덕션이라 알림 없음)."""
    try:
        days = (today - date.fromisoformat(LOGIN.read_text().strip())).days
    except (OSError, ValueError):
        return []
    if days in (5, 6):
        return [f"YouTube 분석 로그인이 {7 - days}일 뒤 끊길 수 있음 (앱이 '테스트' 상태일 때) → ./br insights login"]
    return []


def extras(yta, videos: list[dict], end: str) -> dict:
    """채널 단위: 롱폼별 유입 경로(쇼츠에서 넘어온 조회 확인) · 시청자 연령/성별 · 기기."""
    longs = [v["id"] for v in videos if v["privacy"] == "public" and v["duration"] > 75]
    out: dict = {"long_sources": {}, "audience": [], "devices": []}
    try:
        if longs:
            for r in _query(yta, startDate="2024-01-01", endDate=end, dimensions="video,insightTrafficSourceType",
                            metrics="views", filters="video==" + ",".join(longs)):
                out["long_sources"].setdefault(r["video"], {})[r["insightTrafficSourceType"]] = int(r["views"])
        out["audience"] = sorted(_query(yta, startDate="2024-01-01", endDate=end, dimensions="ageGroup,gender",
                                        metrics="viewerPercentage"), key=lambda r: -float(r["viewerPercentage"]))
        out["devices"] = _query(yta, startDate="2024-01-01", endDate=end, dimensions="deviceType", metrics="views",
                                sort="-views")
    except Exception as e:  # noqa: BLE001  (부가 정보: 실패해도 보고서는 쓴다)
        out["error"] = str(e)[:200]
    return out


def analyze(info: dict, videos: list[dict], pairs, metrics: dict, curves: dict, today: date) -> dict:
    # 1) 공개 점검
    alerts = []
    for o, v in pairs:
        due = o.get("upload_date")
        if v["privacy"] != "public" and not v.get("publish_at") and due and date.fromisoformat(due) <= today:
            alerts.append(f"'{v['title'][:30]}' 이(가) {v['privacy']} 상태 (올릴 날 {due}) → 공개로 바꿔야 시청 시간이 쌓임")
    alerts += policy_alerts(videos)
    ours_ids = {v["id"] for _, v in pairs}
    for v in videos:
        if v["id"] not in ours_ids and v["privacy"] in ("private", "unlisted") and v["duration"] > 75:
            alerts.append(f"'{v['title'][:30]}' 롱폼이 {v['privacy']} → 공개가 아니면 수익 창출 시청 시간 0")
        if v.get("made_for_kids"):
            alerts.append(f"'{v['title'][:30]}' 이(가) '아동용'으로 설정됨 → 우리 채널은 부모 대상 '아동용 아님' (docs/POLICY.md)")
    # 2) 수익 창출 진행 (추정)
    long_pub = [v for v in videos if v["privacy"] == "public" and v["duration"] > 75]
    short_pub = [v for v in videos if v["privacy"] == "public" and v["duration"] <= 75]
    hours = sum(float(metrics.get("_365", {}).get(v["id"], {}).get("estimatedMinutesWatched", 0)) for v in long_pub) / 60
    m90 = metrics.get("_90", {})
    shorts_views = sum(int(m90.get(v["id"], {}).get("engagedViews", m90.get(v["id"], {}).get("views", 0)))
                       for v in short_pub)  # 수익 창출 1천만 회는 engaged views 기준
    ypp = {"long_public_hours": round(hours, 1), "shorts_public_views_90d": shorts_views,
           "subscribers": info.get("subscribers", 0), "long_public": len(long_pub), "long_total": sum(
               1 for v in videos if v["duration"] > 75)}
    # 3) 영상별 성과 + 이탈 지점
    per_video, rows = [], []
    for o, v in pairs:
        m = metrics.get("_life", {}).get(v["id"], {})
        age = (today - date.fromisoformat(v["published_at"][:10])).days if v.get("published_at") else 0
        item = {"id": v["id"], "episode": o["episode"], "target": o["target"], "title": v["title"], "privacy": v["privacy"],
                "age_days": age, "settled": age >= SETTLE_DAYS,
                "views": int(m.get("views", 0)), "avg_view_pct": round(float(m.get("averageViewPercentage", 0)), 1),
                "avg_view_s": round(float(m.get("averageViewDuration", 0)), 1),
                "subs": int(m.get("subscribersGained", 0)), "likes": int(m.get("likes", 0)),
                "shares": int(m.get("shares", 0)), "drops": []}
        if m.get("engagedViews") is not None and item["views"]:
            item["stay"] = round(int(m["engagedViews"]) / item["views"], 2)  # 넘기지 않고 본 비율(쇼츠)
        dur = o.get("duration") or v["duration"]
        curve = curves.get(v["id"], [])
        if curve:
            at = lambda t: next((w for r, w in curve if r * dur >= t), curve[-1][1])  # noqa: E731
            if o["video"] != "long":
                item["cliff"] = round(at(3) - at(7), 2)  # 3→7초 이탈 (docs/STYLE.md 2-1)
                item["one_scene"] = _one_scene(o["episode"], o["video"])
            item["hold_3s"] = round(next((w for r, w in curve if r * dur >= 3), curve[-1][1]), 2)
            item["end_ratio"] = round(curve[-1][1], 2)
            for t, size in drops(curve, dur):
                seg = segment_at(o["segments"], t)
                f = _features(o["episode"], seg) if seg else {}
                item["drops"].append({"t": round(t, 1), "drop": round(size, 3), **f})
            # 구간별 유지율 변화 (학습용)
            for seg in (o["segments"] if o["video"] != "long" else []):
                if seg["start"] < 3:  # 첫 3초는 어떤 편집이든 크게 빠짐 → 비교에서 뺀다 (첫 6초는 5번 절)
                    continue
                a, b = seg["start"] / dur, seg["end"] / dur
                pts = [w for r, w in curve if a <= r <= b]
                if len(pts) >= 2:
                    rows.append({**_features(o["episode"], seg), "delta": pts[-1] - pts[0], "video": o["video"]})
        per_video.append(item)
    # 4) 학습: 편집 특징별 평균 유지율 변화 (구간 길이로 나눈 초당 변화)
    learn = {}
    settled_n = sum(1 for p in per_video if p["settled"] and p["views"] >= 100)
    feats = [r for r in rows if r.get("cut")]
    def avg(sel):
        xs = [r["delta"] / max(0.5, r["cut"]) for r in feats if sel(r)]
        return (round(sum(xs) / len(xs), 4), len(xs)) if xs else (None, 0)
    for name, sel in (("실제 대사(talk)", lambda r: r.get("talk")), ("음소거", lambda r: r.get("mute")),
                      ("big 자막", lambda r: r.get("big")), ("효과 2개 이상", lambda r: r.get("effects", 0) >= 2),
                      ("효과 없음", lambda r: r.get("effects", 0) == 0), ("컷 4초 이상", lambda r: r.get("cut", 0) >= 4),
                      ("컷 2초 미만", lambda r: r.get("cut", 0) < 2)):
        learn[name] = avg(sel)
    learn["_baseline"] = avg(lambda r: True)
    return {"date": today.isoformat(), "channel": info, "alerts": alerts, "ypp": ypp, "videos": per_video,
            "learn": learn, "settled_videos": settled_n, "matched": len(pairs), "uploads": len(videos)}


REJECTION = {"copyright": "저작권 침해", "duplicate": "중복 업로드", "inappropriate": "부적절한 콘텐츠",
             "legal": "법적 사유", "length": "길이 초과", "termsOfUse": "이용약관 위반", "trademark": "상표권",
             "uploaderAccountClosed": "계정 폐쇄", "uploaderAccountSuspended": "계정 정지"}


def policy_alerts(videos: list[dict]) -> list[str]:
    """YouTube 가 영상에 표시한 규정 관련 상태 (API 로 확인 가능한 것)."""
    out = []
    for v in videos:
        t = v["title"][:30]
        if v.get("upload_status") == "rejected":
            out.append(f"'{t}' 업로드 거부: {REJECTION.get(v.get('rejection'), v.get('rejection'))}")
        elif v.get("upload_status") == "failed":
            out.append(f"'{t}' 업로드 실패: {v.get('failure')}")
        if v.get("region_blocked") or v.get("region_allowed") is not None:
            out.append(f"'{t}' 일부 국가에서 차단됨 → 음원 저작권 신고 가능성. Studio > 콘텐츠 > 제한사항 확인")
        if v.get("age_restricted"):
            out.append(f"'{t}' 연령 제한 → 노출·수익에 불리. Studio 에서 사유 확인")
    return out


def collect(today: date | None = None, interactive: bool = False) -> dict:
    today = today or date.today()
    if mode() == "key":
        return collect_public(today)
    if not TOKEN.exists() and not interactive:
        # 자동 실행 중에는 브라우저 로그인을 띄우지 않는다 (멈춤 방지)
        if API_KEY.exists():
            return collect_public(today, note="전체 분석 로그인이 없어 간단 모드로 수집 (./br insights login)")
        raise RuntimeError("YouTube 로그인이 필요합니다: ./br insights login (또는 간단 모드 API 키)")
    try:
        yt, yta = services(interactive=interactive)
    except Exception as e:  # noqa: BLE001  (테스트 상태 앱은 7일마다 로그인이 만료됨)
        if "invalid_grant" in str(e) or "expired" in str(e).lower() or "로그인" in str(e):
            TOKEN.unlink(missing_ok=True)
            note = "전체 분석 로그인이 만료됨(테스트 상태 앱은 7일마다) → ./br insights login 다시"
            if API_KEY.exists():
                return collect_public(today, note=note)
            raise RuntimeError(note) from e
        raise
    info, videos = fetch_videos(yt)
    pairs = match(our_videos(), videos)
    record_links(pairs)
    ids = [v["id"] for v in videos]
    end = today.isoformat()
    metrics = {
        "_life": video_metrics(yta, ids, "2024-01-01", end) if ids else {},
        "_365": video_metrics(yta, ids, (today - timedelta(days=365)).isoformat(), end) if ids else {},
        "_90": video_metrics(yta, ids, (today - timedelta(days=90)).isoformat(), end) if ids else {},
    }
    curves = {v["id"]: retention(yta, v["id"], "2024-01-01", end) for _, v in pairs if v["privacy"] == "public"}
    result = analyze(info, videos, pairs, metrics, curves, today)
    result["extras"] = extras(yta, videos, end)
    result["alerts"] += login_alerts(today)
    _snapshot(today, result)
    write_doc(result)
    return result


def collect_public(today: date, note: str = "") -> dict:
    """간단 모드(API 키): 공개 영상 목록·규정 상태·누적 조회수. 시청 시간·시청 지속 곡선·비공개 영상은 OAuth 필요."""
    yt = key_service()
    handle = config.load()["channel"].get("handle", "")
    info, videos = fetch_videos(yt, handle=handle)
    pairs = match(our_videos(), videos)
    record_links(pairs)
    alerts = ([note] if note else []) + policy_alerts(videos)
    for o in our_videos():
        due = o.get("upload_date")
        if due and date.fromisoformat(due) <= today - timedelta(days=1) and not any(
                po is o or (po["episode"], po["target"]) == (o["episode"], o["target"]) for po, _ in pairs):
            alerts.append(f"'{o['title'][:30]}' 올릴 날({due})이 지났는데 공개 영상에서 안 보임 → 비공개·일부공개면 공개로")
    per_video = []
    for o, v in pairs:
        age = (today - date.fromisoformat(v["published_at"][:10])).days if v.get("published_at") else 0
        per_video.append({"episode": o["episode"], "target": o["target"], "title": v["title"], "privacy": v["privacy"],
                          "age_days": age, "settled": age >= SETTLE_DAYS, "views": v["views_total"],
                          "avg_view_pct": 0, "avg_view_s": 0, "subs": 0, "likes": v.get("likes_total", 0),
                          "shares": 0, "drops": []})
    shorts = sum(v["views_total"] for v in videos if v["duration"] <= 75)
    result = {"date": today.isoformat(), "mode": "key", "channel": info, "alerts": alerts,
              "ypp": {"long_public_hours": None, "shorts_public_views_90d": shorts, "subscribers": info["subscribers"],
                      "long_public": sum(1 for v in videos if v["duration"] > 75), "long_total": None},
              "videos": per_video, "learn": {}, "settled_videos": 0, "matched": len(pairs), "uploads": len(videos)}
    _snapshot(today, result)
    write_doc(result)
    return result


def _snapshot(today: date, result: dict) -> None:
    """오늘 스냅샷 저장. 용량 최소화: 어제와 비교(write_doc)에 필요한 최근 2개만 남기고, 공백 없이."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{today.isoformat()}.json").write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                                                       encoding="utf-8")
    for old in sorted(OUT_DIR.glob("*.json"))[:-KEEP_SNAPSHOTS]:
        old.unlink(missing_ok=True)


# --- 보고서 ---------------------------------------------------------------------------------

def _previous(today: str) -> dict | None:
    snaps = sorted(p for p in OUT_DIR.glob("*.json") if p.stem < today)
    return json.loads(snaps[-1].read_text(encoding="utf-8")) if snaps else None


def write_doc(r: dict) -> Path:
    prev = _previous(r["date"])
    y = r["ypp"]
    lines = [
        "# 성과 인사이트 (자동 생성: ./br insights)",
        "",
        f"기준일 {r['date']} · 채널 영상 {r['uploads']}개 중 우리 에피소드와 짝지은 영상 {r['matched']}개.",
        "**Analytics 수치는 보통 2~3일 늦게 확정되고, 수익 창출 탭(Studio)은 더 늦을 수 있다.** 공개 3일 미만 영상은 '집계 중'.",
        "Claude 는 다음 편 기획 전에 이 문서를 읽는다. 표본 수가 적을 때는 규칙을 바꾸지 말고 '경향'으로만 참고한다.",
        "",
        "## 1. 확인 필요 (공개 상태)",
        "",
    ]
    lines += [f"- ⚠ {a}" for a in r["alerts"]] or ["- 없음 (올릴 날이 지난 영상은 모두 공개)"]
    lines += ["", "## 2. 수익 창출 진행 (추정, 최종 기준은 Studio '수익 창출' 탭)", ""]
    def delta(k):
        if not prev or y.get(k) is None or prev["ypp"].get(k) is None:
            return ""
        d = y[k] - prev["ypp"].get(k, 0)
        return f" (어제보다 +{d:,.1f})" if isinstance(d, float) else f" (어제보다 +{d:,})"
    hours = (f"**{y['long_public_hours']:,.1f} / {YPP_HOURS:,}시간**{delta('long_public_hours')}"
             if y.get("long_public_hours") is not None else "확인 불가 (간단 모드: 시청 시간은 OAuth 로그인 필요)")
    lines += [
        f"- 공개 롱폼 시청 시간(최근 365일): {hours} · 공개 롱폼 {y['long_public']}개",
        f"- 공개 쇼츠 조회수({'최근 90일' if r.get('mode') != 'key' else '누적'}): {y['shorts_public_views_90d']:,} / {YPP_SHORTS_VIEWS:,}{delta('shorts_public_views_90d')}",
        f"- 구독자: {y['subscribers']:,} / {YPP_SUBS:,}",
        "- 쇼츠 조회는 시청 시간 4,000시간에 들어가지 않는다 → 쇼츠 '관련 동영상'으로 롱폼에 연결, 롱폼은 길고 끝까지 보게",
        "", "## 3. 영상별 성과", "",
        "| 영상 | 상태 | 조회 | 넘기지 않음 | 3→7초 이탈 | 평균 시청 | 끝까지 | 구독+ | 가장 많이 빠진 곳 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for v in sorted(r["videos"], key=lambda v: (v["episode"], v["target"])):
        state = v["privacy"] + ("" if v["settled"] else " · 집계 중")
        d = v["drops"][0] if v["drops"] else None
        where = f"{d['t']:.0f}s {d.get('caption', '')[:18]} (−{d['drop'] * 100:.0f}%)" if d else "-"
        stay = f"{v['stay'] * 100:.0f}%" if v.get("stay") is not None else "-"
        lines.append(f"| {v['title'].replace('|', '/')[:26]} | {state} | {v['views']:,} | {stay} | {v.get('cliff', '-')} | "
                     f"{v['avg_view_pct']}% | {v.get('end_ratio', '-')} | {v['subs']} | {where} |")
    lines += ["", "## 4. 편집 패턴별 시청 유지 (쇼츠, 3초 이후 구간. 1초당 유지율 변화, 0 에 가까울수록 덜 빠짐)", ""]
    base, nb = r["learn"].get("_baseline", (None, 0))
    enough = r["settled_videos"] >= MIN_VIDEOS
    for name, (val, n) in r["learn"].items():
        if name.startswith("_") or val is None:
            continue
        diff = f" (평균 대비 {val - base:+.4f})" if base is not None else ""
        lines.append(f"- {name}: {val:+.4f}/초 · 구간 {n}개{diff}")
    if not enough:
        lines += ["", f"> 확정 데이터가 있는 영상 {r['settled_videos']}편 (조회 100 이상·공개 3일 이상). "
                      f"{MIN_VIDEOS}편이 되기 전까지는 경향만 참고하고 규칙은 바꾸지 않는다."]
    else:
        lines += ["", "> 표본 충분. 평균보다 많이 빠지는 패턴은 줄이고, 덜 빠지는 패턴은 늘린다 (사용자 피드백 > 이 수치)."]
    lines += opening_lines(r) + long_source_lines(r) + audience_lines(r)
    from . import stats

    try:  # 공개 지표 절(stats.py, schedule sync 가 씀)은 남긴다
        tail = DOC.read_text(encoding="utf-8").split(stats.MARK, 1)[1]
        lines += ["", stats.MARK + tail.rstrip("\n")]
    except (OSError, IndexError):
        pass
    DOC.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return DOC


def opening_lines(r: dict) -> list[str]:
    """쇼츠 첫 6초: 한 장면으로 이어 간 쇼츠 vs 바로 장면이 바뀐 쇼츠의 3→7초 이탈 (docs/STYLE.md 2-1)."""
    rows = [v for v in r["videos"] if v.get("cliff") is not None and v["views"] >= 100 and v.get("one_scene") is not None]
    if not rows:
        return []
    same = [v["cliff"] for v in rows if v["one_scene"]]
    diff = [v["cliff"] for v in rows if not v["one_scene"]]
    avg = lambda xs: f"{sum(xs) / len(xs):.2f} ({len(xs)}편)" if xs else "-"  # noqa: E731
    top = sorted(rows, key=lambda v: -v["views"])[:3]
    return ["", "## 5. 쇼츠 첫 6초 (3→7초 이탈, 작을수록 좋음. docs/STYLE.md 2-1)", "",
            f"- 첫 6초 한 장면: {avg(same)} · 바로 다른 장면: {avg(diff)}",
            "- 조회 상위: " + " · ".join(f"{v['title'][:14]} {v['cliff']}" for v in top),
            "- 값이 클수록 첫 장면을 다시 보기 (판단 기준은 비공개 문서)"]


def long_source_lines(r: dict) -> list[str]:
    src = (r.get("extras") or {}).get("long_sources") or {}
    if not src:
        return []
    out = ["", "## 6. 롱폼 유입 경로 (쇼츠에서 넘어온 조회 = SHORTS_CONTENT_LINKS)", "",
           "| 롱폼 | 조회 | 쇼츠에서 | 관련 동영상 | 구독 피드·홈 | 검색 |", "|---|---|---|---|---|---|"]
    for v in sorted((v for v in r["videos"] if v["target"] == "youtube_long" and v.get("id") in src),
                    key=lambda v: v["episode"]):
        s = src[v["id"]]
        out.append(f"| {v['title'].split('|')[0].strip()[:28]} | {sum(s.values()):,} | {s.get('SHORTS_CONTENT_LINKS', 0)} | {s.get('RELATED_VIDEO', 0)} | "
                   f"{s.get('SUBSCRIBER', 0) + s.get('YT_OTHER_PAGE', 0)} | {s.get('YT_SEARCH', 0)} |")
    return out


def audience_lines(r: dict) -> list[str]:
    ex = r.get("extras") or {}
    aud = ex.get("audience") or []
    if not aud:
        return []
    g = {"female": "여성", "male": "남성"}
    top = " · ".join(f"{a['ageGroup'].replace('age', '').replace('-', '~')}세 {g.get(a['gender'], a['gender'])} "
                     f"{float(a['viewerPercentage']):.0f}%" for a in aud[:4])
    fem = sum(float(a["viewerPercentage"]) for a in aud if a["gender"] == "female")
    dev = ex.get("devices") or []
    tot = sum(int(d["views"]) for d in dev) or 1
    return ["", "## 7. 시청자", "", f"- 여성 {fem:.0f}% · 상위: {top}",
            "- 기기: " + " · ".join(f"{d['deviceType'].lower()} {int(d['views']) / tot * 100:.0f}%" for d in dev[:4])]


def summary_text(r: dict) -> str:
    y = r["ypp"]
    hours = f"{y['long_public_hours']:,.1f}/4,000h" if y.get("long_public_hours") is not None else "(OAuth 필요)"
    head = f"📊 시청 시간 {hours} · 쇼츠 {y['shorts_public_views_90d']:,}회 · 구독 {y['subscribers']:,}"
    return "\n".join([head] + [f"⚠ {a}" for a in r["alerts"][:5]])
