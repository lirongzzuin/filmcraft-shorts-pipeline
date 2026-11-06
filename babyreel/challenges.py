"""댓글 챌린지: 시청자가 댓글로 제안한 '개월 수에 맞는 순한 챌린지'를 골라 실제로 해 보는 시리즈 (2026-10-23 사용자 요청).

형식: 'N일 동안 ~하면 생기는 일' (도전 → 과정 → 결과). **아기에게 안전하고, 평소 육아의 범위 안**에서만.

흐름
  1. 시점 확인 (status): 지금은 과거 영상을 올리는 중이라 '실시간'이 아니다. 올리는 영상의 이야기 날짜가 오늘에
     가까워지면(warmup → live) 설명란에 '챌린지 추천 받아요' 문구가 자동으로 붙는다 (metadata.py)
  2. 아이디어 모으기 (collect): YouTube 댓글(API 키 또는 OAuth) + 텔레그램 '챌린지: …' + ./br challenge add
  3. 안전 거르기 (screen): 아이의 지금 개월 수에 맞는지, 금지 항목(굶기기·잠 안 재우기·위험 음식·놀래키기 …)이 없는지
  4. 고르기 → 촬영 → ./br challenge new <번호> 로 에피소드 생성 (series "challenge", 아이디어·제안자 기록, 크레딧 자동)

안전 원칙 (docs/CONCEPT.md 14번): 판단이 애매하면 하지 않는다. 최종 결정은 엄마 아빠가, 촬영 중 아이가 힘들어하면 바로 중단.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from . import config
from .config import DATA, EPISODES

IDEAS = DATA / "challenge_ideas.json"


# --- 안전 규칙 -------------------------------------------------------------------------------
# (이유, 정규식). 하나라도 맞으면 그 아이디어는 '불가'. 소아과 일반 권고(먹는 것·잠·안전·정서)를 바탕으로 보수적으로.
BLOCKED = [
    ("먹을 것·물을 굶기거나 끼니를 거르게 함", r"굶|금식|단식|안\s*먹이|밥\s*(을\s*)?안\s*주|우유\s*(를\s*)?안\s*주|분유\s*(를\s*)?안|물\s*(을\s*)?안\s*주|끼니\s*거르"),
    ("잠을 줄이거나 안 재움", r"안\s*재우|잠\s*(을\s*)?안\s*재|못\s*자게|밤새|낮잠\s*(을\s*)?(없|빼|안)|수면\s*부족|\d+\s*시간\s*(동안\s*)?(안|못)\s*자"),
    ("일부러 울리거나 놀라게 함", r"울리|울게\s*하|놀래|놀라게|겁\s*주|무섭게|깜짝\s*카메라|몰카|공포|귀신"),
    ("혼자 두거나 떼어 놓음", r"혼자\s*두|혼자\s*놔|방치|무시하기|모른\s*척|외면"),
    ("위험한 음식·물질", r"꿀(?!잠|잼|팁)|생우유|통포도|땅콩|견과|떡(?!뻥)|사탕|젤리|팝콘|술|커피|카페인|매운|불닭|약\s*먹|영양제|생선회|날것"),
    ("체중·다이어트", r"다이어트|살\s*빼|체중\s*감량|칼로리\s*제한"),
    ("위험한 신체 활동·환경", r"높은\s*곳|던지|거꾸로|흔들|물속|잠수|뜨거운|차가운\s*물|얼음물|추운\s*데|더운\s*데|차\s*안에|맨발로\s*밖"),
    ("벌·창피 주기", r"벌\s*주|혼내기|창피|놀리기|비교하기"),
]
# (최소 개월 수, 이유, 정규식): 이 나이 전에는 안 되는 것
AGE_GATED = [
    (12, "간식·음식 메뉴 바꾸기는 돌 이후 (이유식 단계에서는 정해진 식단)", r"간식|과자|메뉴|반찬|밥만"),
    (18, "영상·화면 노출은 18개월 이후 (영상 통화 제외)", r"유튜브|영상\s*보|티비|TV|태블릿|스마트폰\s*보"),
    (24, "혼자 하기 챌린지(숟가락·양치·옷 입기)는 두 돌 무렵부터", r"스스로|혼자서|혼자\s*(먹|입|양치)"),
    (12, "바깥 활동 하루 종일은 돌 이후", r"하루\s*종일\s*(밖|야외|외출)"),
]
MAX_DAYS = 7  # 'N일 동안'은 7일까지 (길어지면 아이 일상이 무리하게 바뀐다)


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)  # 안 되는 이유
    notes: list[str] = field(default_factory=list)  # 주의할 점 (되긴 하지만)


def months_old(child: str, on: date | None = None) -> int | None:
    on = on or date.today()
    c = next((c for c in config.children() if c["nick"] == child), None)
    if not c or not c.get("birth_date"):
        return None
    b = date.fromisoformat(c["birth_date"])
    return (on.year - b.year) * 12 + on.month - b.month - (on.day < b.day)


def screen(text: str, months: int | None) -> Verdict:
    """아이디어 글 → 해도 되는지. 판단이 애매한 표현은 막는 쪽으로 (사람이 다시 볼 수 있게 reasons 에 남김)."""
    t = text.replace("\n", " ")
    v = Verdict(ok=True)
    for why, pat in BLOCKED:
        if re.search(pat, t, re.I):
            v.ok = False
            v.reasons.append(why)
    if months is not None:
        for min_m, why, pat in AGE_GATED:
            if months < min_m and re.search(pat, t, re.I):
                v.ok = False
                v.reasons.append(f"{why} (지금 {months}개월)")
    for n in re.findall(r"(\d+)\s*일\s*(?:동안|간)", t):
        if int(n) > MAX_DAYS:
            v.ok = False
            v.reasons.append(f"{n}일은 너무 김 (최대 {MAX_DAYS}일)")
    if re.search(r"밥만|간식", t) and v.ok:
        v.notes.append("식사량은 평소대로, 간식 대신 과일·물은 OK. 아이가 배고파하면 바로 중단")
    if re.search(r"TV|티비|영상|유튜브", t, re.I) and v.ok:
        v.notes.append("영상 '안 보기' 방향만 (더 보여 주기는 하지 않음)")
    return v


# --- 개월 수별 추천 템플릿 (댓글이 적을 때 후보로) ------------------------------------------------
# [공개 사본] 실제 개월 수별 아이디어 목록은 비공개 설정에 있다. 형식: (최소 개월, 최대 개월, 아이디어)
TEMPLATES = [
    (0, 60, "<개월 수별 챌린지 아이디어>"),
]


def templates_for(months: int) -> list[str]:
    return [t for lo, hi, t in TEMPLATES if lo <= months < hi]


# --- 시점: 과거 영상을 올리는 중인가, 실시간에 가까운가 -----------------------------------------------

def story_lag(today: date | None = None) -> tuple[int | None, str | None]:
    """(가장 최근 에피소드의 이야기 날짜가 오늘보다 며칠 전인지, 그 에피소드 id). 번외편·테스트 제외."""
    today = today or date.today()
    latest = None
    for p in EPISODES.glob("*/plan.json"):
        if p.parent.name.startswith("_"):
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if raw.get("series") == "special" or not raw.get("date"):
            continue
        d = date.fromisoformat(raw["date"])
        if latest is None or d > latest[0]:
            latest = (d, p.parent.name)
    if latest is None:
        return None, None
    return (today - latest[0]).days, latest[1]


def phase(today: date | None = None) -> str:
    """archive(과거 영상 올리는 중: 챌린지 안 함) | warmup(곧 따라잡음: 댓글로 아이디어 미리 받기) | live(실시간: 진행)."""
    cfg = config.load().get("challenge", {})
    forced = cfg.get("mode", "auto")
    if forced in ("archive", "warmup", "live"):
        return forced
    lag, _ = story_lag(today)
    if lag is None:
        return "archive"
    if lag <= int(cfg.get("live_within_days", 21)):
        return "live"
    if lag <= int(cfg.get("warmup_within_days", 75)):
        return "warmup"
    return "archive"


def cta(child_months: dict[str, int] | None = None) -> str:
    """설명란·캡션에 붙일 '챌린지 추천' 문구 (warmup/live 때만)."""
    p = phase()
    if p == "archive":
        return ""
    ages = child_months or {c["nick"]: m for c in config.children() if (m := months_old(c["nick"])) is not None}
    who = " · ".join(f"{k}({_age_label(v)})" for k, v in ages.items())
    lead = "다음 육아일지는 댓글 챌린지!" if p == "live" else "곧 댓글 챌린지를 시작해요!"
    return (f"💬 {lead} {who}에게 해 보고 싶은 순한 챌린지를 댓글로 추천해 주세요.\n"
            "   (예: 하루 동안 간식 대신 밥만 먹기 · 안전한 것만 골라 실제로 해 봐요)")


def _age_label(m: int) -> str:
    return f"{m}개월" if m < 36 else f"{m // 12}살"


# --- 아이디어 저장소 -------------------------------------------------------------------------------

@dataclass
class Idea:
    id: int
    text: str
    source: str  # youtube | telegram | manual
    author: str = ""  # 댓글 작성자 표시 이름 (공개 정보). 크레딧에 씀
    likes: int = 0
    video: str = ""
    added: str = ""
    status: str = "new"  # new | ok | blocked | planned | done | skip
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    child: str = ""
    episode: str = ""


def load() -> list[Idea]:
    try:
        return [Idea(**x) for x in json.loads(IDEAS.read_text(encoding="utf-8"))]
    except (OSError, ValueError):
        return []


def save(ideas: list[Idea]) -> None:
    IDEAS.parent.mkdir(parents=True, exist_ok=True)
    IDEAS.write_text(json.dumps([asdict(i) for i in ideas], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


CHALLENGE_WORDS = re.compile(r"챌린지|해\s*(주|보)세요|해\s*봐|해\s*줘|하기\b|해보면|했으면|도전|\d+\s*일\s*(동안|간)|하루\s*동안|일주일")


def looks_like_idea(text: str) -> bool:
    return bool(CHALLENGE_WORDS.search(text)) and 4 <= len(text.strip()) <= 200


def _norm(t: str) -> str:
    return re.sub(r"[^가-힣a-z0-9]", "", t.lower())


def add(text: str, source: str = "manual", author: str = "", likes: int = 0, video: str = "",
        child: str | None = None) -> Idea | None:
    """아이디어 하나 추가 (같은 글이면 좋아요만 갱신). 추가하자마자 안전 검사."""
    ideas = load()
    key = _norm(text)
    for i in ideas:
        if _norm(i.text) == key:
            i.likes = max(i.likes, likes)
            save(ideas)
            return None
    child = child or default_child(text)
    v = screen(text, months_old(child) if child else None)
    idea = Idea(id=max((i.id for i in ideas), default=0) + 1, text=text.strip(), source=source, author=author,
                likes=likes, video=video, added=datetime.now().isoformat(timespec="minutes"),
                status="ok" if v.ok else "blocked", reasons=v.reasons, notes=v.notes, child=child or "")
    ideas.append(idea)
    save(ideas)
    return idea


def default_child(text: str) -> str:
    """글에 이름이 있으면 그 아이, 없으면 첫째."""
    kids = [c["nick"] for c in config.children()]
    for k in kids:
        if k in text:
            return k
    return kids[0] if kids else ""


def collect_comments(yt, video_ids: list[str], per_video: int = 100) -> int:
    """YouTube 댓글에서 챌린지 아이디어를 모은다 (공개 댓글, 읽기 전용). 추가된 개수."""
    added = 0
    for vid in video_ids:
        try:
            r = yt.commentThreads().list(part="snippet", videoId=vid, maxResults=min(100, per_video),
                                         order="relevance", textFormat="plainText").execute()
        except Exception as e:  # noqa: BLE001 - 댓글이 꺼진 영상 등
            print(f"  ! 댓글 읽기 실패 {vid}: {str(e)[:80]}")
            continue
        for item in r.get("items", []):
            s = item["snippet"]["topLevelComment"]["snippet"]
            text = s.get("textDisplay", "")
            if looks_like_idea(text):
                if add(text, "youtube", author=s.get("authorDisplayName", ""), likes=int(s.get("likeCount", 0)),
                       video=vid):
                    added += 1
    return added


def channel_video_ids(yt, limit: int = 30) -> list[str]:
    """우리 채널의 최근 공개 영상 id (업로드 재생목록)."""
    handle = config.load()["channel"].get("handle", "").lstrip("@")
    ch = yt.channels().list(part="contentDetails", forHandle=handle).execute().get("items", [])
    if not ch:
        return []
    uploads = ch[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    r = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=min(50, limit)).execute()
    return [i["contentDetails"]["videoId"] for i in r.get("items", [])]


def youtube_client():
    """API 키(간단) → OAuth 순서. 둘 다 없으면 None."""
    from . import insights

    if insights.API_KEY.exists():
        return insights.key_service()
    try:
        return insights.services(interactive=False)[0]
    except Exception:  # noqa: BLE001
        return None


def ranked(ideas: list[Idea] | None = None, child: str | None = None) -> list[Idea]:
    """할 수 있는 아이디어를 좋아요 순으로."""
    ideas = load() if ideas is None else ideas
    pool = [i for i in ideas if i.status == "ok" and (child is None or i.child == child)]
    return sorted(pool, key=lambda i: (-i.likes, i.id))


def title_for(idea: Idea, days: int | None = None) -> str:
    """'첫째에게 하루 동안 간식 대신 밥만 줬더니' 같은 제목 초안 (기획 때 다듬는다)."""
    t = re.sub(r"(해\s*(주|보)세요|해\s*봐요?|해\s*줘요?|챌린지|부탁해요|ㅋ+|!+|\?+)", "", idea.text).strip(" .~")
    m = re.search(r"(\d+\s*일|하루|일주일)\s*(동안|간)?", t)
    span = m.group(0) if m else (f"{days}일 동안" if days else "하루 동안")
    body = t.replace(m.group(0), "").strip() if m else t
    return f"{idea.child}에게 {span} {body} 했더니 생기는 일".replace("  ", " ")


def credit_line(idea: Idea) -> str:
    if idea.source == "youtube" and idea.author:
        return f"💡 이번 챌린지는 {idea.author} 님의 댓글 아이디어예요. 감사합니다!"
    return "💡 이번 챌린지는 댓글로 받은 아이디어예요."
