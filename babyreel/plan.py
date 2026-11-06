"""에피소드 기획서(episodes/<id>/plan.json) 스키마와 검증.

시간 단위는 모두 초.
- 클립의 start/end 는 원본 파일 기준.
- 클립 안의 captions/sfx/stickers/punch 의 `at` 은 그 클립이 타임라인에서 시작하는 지점 기준
  (속도 적용 후). 그래서 클립 순서를 바꾸거나 구간을 바꿔도 다시 계산할 필요가 없다.
- music.cuts 만 완성 영상(타임라인) 기준.

편집 문법은 비공개 스타일 가이드(docs/, 공개 사본에서 제외) 참고.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from . import config, music
from .media import probe

PLAN_FILE = "plan.json"
CAPTION_STYLES = ("sub", "talk", "big", "chip")
LAYOUTS = ("frame", "full")

# FilmCraft 텍스트 엔진은 컬러 이모지를 그리지 못한다(□ 로 나옴).
# 화면 텍스트에는 금지하고, 이모지는 stickers / 화자 아이콘(PNG)으로 넣는다.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0000FE0F\U0000200D]"
)


@dataclass
class Caption:
    at: float
    text: str
    dur: float = 2.5
    style: str = "sub"  # sub(내레이션) | talk(대사, 화자 아이콘) | big(가운데 펀치라인) | chip(시간/상황 라벨)
    speaker: str | None = None  # talk 일 때 channel.toml [speakers] 의 키 (예: "아빠", "아기")
    sfx: str | None = None  # 자막이 뜰 때 함께 낼 효과음 (sfx.CATALOG)
    voice: bool | str | None = None  # AI 음성으로 읽기. true = speaker 음색(없으면 narrator), 문자열 = [voices] 키


@dataclass
class Sfx:
    at: float
    name: str
    gain_db: float = 0.0


@dataclass
class Sticker:
    at: float
    emoji: str  # 컬러 이모지 1개 (PNG 로 구워서 얹음)
    dur: float = 1.5
    x: float = 0.8  # 영상 영역 기준 0~1
    y: float = 0.25
    size: float = 0.18  # 영상 영역 가로 대비 크기
    sfx: str | None = None  # 효과음은 필요할 때만 지정 (기본 무음: 과함 방지, STYLE 11번)


@dataclass
class Cover:
    """가림 스티커: 원본 화면의 한 자리에 고정해서 덮는다 (목욕 장면의 몸, 이름표 등). 일반 스티커와 달리
    얼굴·자막을 피해 옮겨 가지 않고, 웃음 장치 수에도 세지 않는다. 클립 전체에 걸쳐 나온다.

    points: [[원본 초, x, y], ...] (원본 화면 0~1, 컨택트시트 기준). 움직이면 여러 점을 주면 그 사이를 따라간다
    size: 원본 화면 가로 대비 크기 (0.35 = 35%). 화면에 실제로 보이는 크기는 클립 확대에 따라 커진다"""

    emoji: str = "🐥"
    size: float = 0.35
    points: list[list[float]] = field(default_factory=list)


@dataclass
class Blur:
    """위치가 드러나는 곳 흐리게: 창밖 풍경(건물·하늘·도로·간판), 현관 호수 등 (2026-11-03 사용자 요청: 집 위치가 특정되지 않게).
    클립 전체에 걸쳐 사각형 영역을 강하게 흐린다 (가장자리는 부드럽게). 썸네일에도 같이 적용.

    box: [x0, y0, x1, y1] 원본 화면 0~1 (컨택트시트 기준, 왼쪽 위 원점). 넉넉하게 (창틀까지)
    points: 카메라가 움직여 창이 옮겨 가면 [[원본 초, x0, y0, x1, y1], ...] (그 사이를 따라간다). box 대신 쓴다
    what: 무엇을 가리는지 (기록용, 예: "창밖 아파트")"""

    box: list[float] = field(default_factory=list)
    points: list[list[float]] = field(default_factory=list)
    what: str = "창밖"

    def keys(self) -> list[tuple[float | None, list[float]]]:
        """[(원본 초 또는 None, [x0, y0, x1, y1])]"""
        if self.points:
            return [(p[0], list(p[1:5])) for p in sorted(self.points)]
        return [(None, list(self.box))]

    def at(self, t: float) -> list[float]:
        """원본 t 초의 영역 (점 사이는 직선으로, 범위 밖은 끝 값)."""
        keys = self.keys()
        if keys[0][0] is None or t <= keys[0][0]:
            return keys[0][1]
        for (ta, a), (tb, b) in zip(keys, keys[1:]):
            if t <= tb:
                k = (t - ta) / max(1e-6, tb - ta)
                return [x + (y - x) * k for x, y in zip(a, b)]
        return keys[-1][1]


@dataclass
class Punch:
    """리액션 순간 빠르게 확대했다가 돌아오는 펀치줌."""

    at: float
    dur: float = 0.8
    scale: float = 1.3
    x: float | None = None  # 확대 중심 (0~1). None = 클립 focus
    y: float | None = None


@dataclass
class Clip:
    src: str
    start: float = 0.0  # 원본 파일 기준 시작(초)
    end: float | None = None  # 원본 파일 기준 끝(초). None = 파일 끝
    hold: bool = False  # true: start 프레임을 (end-start) 동안 정지 (정지 화면 개그)
    fit: str = "auto"  # auto | fill(크롭해서 꽉) | blur(전체 보이고 배경 블러)
    focus: float = 0.5  # 크롭될 때 남길 가로 위치 (0=왼쪽, 0.5=가운데, 1=오른쪽)
    focus_y: float = 0.5  # 크롭될 때 남길 세로 위치
    zoom: float = 1.0  # 1.0 = 화면에 꽉 차게. 1.2 = 20% 더 확대
    crop: list[float] = field(default_factory=list)  # [왼, 위, 오른, 아래] % 잘라내기 (병원명·이름표 등 가리기)
    face_guard: bool = True  # 보이는 얼굴이 확대·펀치줌에 잘리지 않게 자동 보정 (babyreel/faces.py)
    stabilize: bool | None = None  # 흔들림 보정. None = 자동(손떨림 잰 뒤 결정, babyreel/stabilize.py), true/false 로 강제
    people: list[str] = field(default_factory=list)  # 화면에 얼굴이 나오는 사람 ["첫째", "엄마"] (가족 등장 점검용)
    # 부모 외 인물 얼굴 블러: 화면에서 그 얼굴이 있는 위치 [[x, y], ...] (0~1, 컨택트시트 기준).
    # 빌더가 Vision 으로 얼굴을 찾아 따라가며 흐리게 한다 (못 찾으면 그 자리 고정 블러). 컷 전체를 버리지 않아도 됨
    blur_faces: list[list[float]] = field(default_factory=list)
    # 위치가 드러나는 곳(창밖 풍경 등) 흐리게 (Blur 참고). 집에서 찍은 클립은 창이 보이면 반드시 (docs/POLICY.md)
    blur_areas: list[Blur] = field(default_factory=list)
    window_ok: bool = False  # 확인함: 창밖이 안 보이거나 보여도 위치를 알 수 없음 (검증기의 '창밖 의심' 경고를 끈다)
    highlight: bool = False  # 롱폼 맨 앞 '미리보기'에 쓸 순간 (지정이 없으면 쇼츠의 big 자막 순간을 자동으로)
    speed: float = 1.0  # 0.5 = 슬로모션
    mute: bool = False  # 원본 소리 전부 끄기 (배경 TV·잡음뿐일 때만. 대사가 있으면 mute_ranges 로 일부만)
    mute_ranges: list[list[float]] = field(default_factory=list)  # [[원본 시작초, 끝초], ...] 이 구간만 소리 끔 (이름·타인 대화)
    gain_db: float = 0.0  # 원본 소리 보정
    captions: list[Caption] = field(default_factory=list)
    sfx: list[Sfx] = field(default_factory=list)
    stickers: list[Sticker] = field(default_factory=list)
    punch: list[Punch] = field(default_factory=list)
    covers: list[Cover] = field(default_factory=list)  # 가림 스티커 (목욕 장면 몸 가리기 등, Cover 참고)
    chapter: str | None = None  # 롱폼 챕터 제목 (설명란 타임스탬프 생성)
    # 이 클립부터 BGM 을 바꾼다: catalog 곡 id (예: "jingle_bells"), "off" = 음악 없이(원본 소리만).
    # 다음에 music 을 지정한 클립까지 이어지고, 바뀌는 순간 1초 크로스페이드. 없으면 앞 곡이 계속
    music: str | None = None
    targets: list[str] = field(default_factory=lambda: ["short", "long"])

    @property
    def duration(self) -> float:
        assert self.end is not None
        return (self.end - self.start) / (1.0 if self.hold else self.speed)


@dataclass
class Music:
    track: str = ""  # assets/music/catalog.json 의 id (권장) 또는 비우고 file 지정
    file: str = ""
    start: float = 0.0  # 음악 파일 안에서 시작 지점
    gain_db: float | None = None  # None = channel.toml 기본값
    cuts: list[list[float]] = field(default_factory=list)  # [[t0, t1], ...] 타임라인 기준 무음 구간
    license: str = ""  # file 을 직접 지정할 때 출처/라이선스 (catalog 곡은 자동)


@dataclass
class Thumbnail:
    clip: int = 0  # clips 인덱스
    at: float = 0.0  # 그 클립 원본 기준 시간
    text: list[str] = field(default_factory=list)  # 큰 글자 1~2줄, 줄당 6자 안팎. 제목 반복 말고 '궁금하게' (STYLE 13번)
    focus: float | None = None  # (예전 템플릿용, 지금은 얼굴 자동 배치)
    focus_y: float | None = None
    zoom: float = 1.0  # 얼굴을 더 크게 (1.2 = 20%)
    stickers: list[str] = field(default_factory=list)  # 첫 번째 하나만 얼굴 옆에 (반응 이모지)
    # 주인공 얼굴 위치 [[x, y], ...] (컨택트시트 기준 0~1). 비우면 가장 큰 얼굴. 어른과 함께면 아기 얼굴을 꼭 지정,
    # 여럿을 함께 담으려면 여러 개 (예: 아빠+첫째). 지정 안 한 다른 얼굴(행인 등)은 자동으로 흐려진다
    faces: list[list[float]] = field(default_factory=list)
    no_face: bool = False  # 얼굴 없는 썸네일을 일부러 쓸 때만 true (풍경·음식). 기본은 얼굴을 못 찾으면 QA 오류


SHORT_TARGETS = ("short", "short2")  # 쇼츠 1부(1일차) · 2부(2일차). 롱폼은 "long"
TARGET_NAMES = {"short": "쇼츠 1부", "short2": "쇼츠 2부", "long": "롱폼"}


@dataclass
class ShortPart:
    """쇼츠 2부: 한 번 만들 때 '쇼츠 2개 + 롱폼 1개'(이틀치)를 만든다 (docs/FEEDBACK.md 2026-10-17).

    1부는 plan 의 title_lines·thumbnail·music·youtube_title… 을 그대로 쓰고,
    2부는 여기 값을 쓴다. 비운 필드(music·hashtags 등)는 1부 값을 물려받는다.
    2부에 쓸 클립은 clips[].targets 에 "short2" 를 넣는다.
    """

    title_lines: list[str] = field(default_factory=list)
    thumbnail: Thumbnail = field(default_factory=Thumbnail)
    music: Music | None = None
    youtube_title: str = ""
    description: str = ""
    instagram_caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    pinned_comment: str = ""  # 2부 고정 댓글 (비우면 자동)


@dataclass
class Plan:
    id: str
    date: str
    concept: str
    clips: list[Clip]
    series: str = "observe"  # channel.toml [series] 키: observe | siblings | growth | secret | firsts
    cast: list[str] = field(default_factory=list)  # 등장하는 아이 애칭 ["첫째", "둘째"] → 나이 표기·태그
    episode_no: int = 0  # 육아일지 번호 (./br new 가 자동 부여)
    layout: str = "frame"  # 쇼츠 레이아웃: frame(레퍼런스형: 캔버스+상단 제목+영상 박스) | full(꽉 찬 화면)
    title_lines: list[str] = field(default_factory=list)  # 쇼츠 상단 제목 2줄 [포인트색 줄, 본문 줄]
    music: Music | None = None
    outputs: dict[str, bool] = field(default_factory=lambda: {"short": True, "long": False})
    thumbnail: Thumbnail = field(default_factory=Thumbnail)
    short2: ShortPart | None = None  # 쇼츠 2부 (outputs.short2 = true 일 때)
    youtube_title: str = ""
    youtube_long_title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)  # 에피소드 전용 해시태그 (#포함)
    instagram_caption: str = ""
    # 연재 일관성 (docs/SERIES.md 로 모인다)
    story_notes: str = ""  # 이번 편에서 일어난 일 1~2줄 (다음 편 Claude 가 읽고 이어 감)
    running_jokes: list[str] = field(default_factory=list)  # 이번 편에서 쓴/생긴 반복 개그 ("<반복 개그 예시>")
    next_hook: str = ""  # 다음 편 예고 한 줄 (영상 끝·설명란에 쓸 수 있음)
    # 고정 댓글 (업로드 직후 채널 계정으로 달고 고정 → 댓글 참여 유도). 비우면 metadata.pinned_comment 가 자동으로
    pinned_comment: str = ""  # 쇼츠 1부
    long_pinned_comment: str = ""  # 롱폼
    safety_reviewed: bool = False  # 사람(부모)이 결과물을 직접 확인했는지. 패키징 전 필수
    safety_notes: str = ""
    # 댓글 챌린지 (series "challenge"): {"idea": 아이디어 번호, "text": 글, "author": 제안자, "child": 아이, "days": N}
    challenge: dict = field(default_factory=dict)
    notes: str = ""
    part_no: int = 0  # 내부용: part("short2") 가 돌려준 2부 보기면 2, 2부가 있는 회차의 1부면 1, 아니면 0

    # --- 입출력 ---------------------------------------------------------------------

    @classmethod
    def load(cls, episode_dir: Path) -> "Plan":
        raw = json.loads((episode_dir / PLAN_FILE).read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Plan":
        raw = dict(raw)
        _check_keys(cls, raw, "plan")
        clips = []
        for i, c in enumerate(raw.get("clips", [])):
            _check_keys(Clip, c, f"clips[{i}]")
            clips.append(Clip(**{
                **c,
                "captions": [Caption(**x) for x in c.get("captions", [])],
                "sfx": [Sfx(**x) for x in c.get("sfx", [])],
                "stickers": [Sticker(**x) for x in c.get("stickers", [])],
                "covers": [Cover(**x) for x in c.get("covers", [])],
                "blur_areas": [Blur(**x) for x in c.get("blur_areas", [])],
                "punch": [Punch(**x) for x in c.get("punch", [])],
            }))
        raw["clips"] = clips
        if raw.get("music"):
            _check_keys(Music, raw["music"], "music")
            raw["music"] = Music(**raw["music"])
        if raw.get("thumbnail"):
            raw["thumbnail"] = Thumbnail(**raw["thumbnail"])
        if raw.get("short2"):
            s2 = dict(raw["short2"])
            _check_keys(ShortPart, s2, "short2")
            if s2.get("thumbnail"):
                s2["thumbnail"] = Thumbnail(**s2["thumbnail"])
            if s2.get("music"):
                _check_keys(Music, s2["music"], "short2.music")
                s2["music"] = Music(**s2["music"])
            raw["short2"] = ShortPart(**s2)
        raw.pop("part_no", None)
        return cls(**raw)

    def save(self, episode_dir: Path) -> None:
        episode_dir.mkdir(parents=True, exist_ok=True)
        (episode_dir / PLAN_FILE).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def clips_for(self, target: str) -> list[Clip]:
        return [c for c in self.clips if target in c.targets]

    def shorts(self) -> list[str]:
        """만들 쇼츠 대상: ["short"] 또는 ["short", "short2"]."""
        return [t for t in SHORT_TARGETS if self.outputs.get(t)]

    def part(self, target: str) -> "Plan":
        """쇼츠 target 을 '1부처럼' 다루는 보기. 빌더·검증기·메타데이터가 쇼츠 1부 코드를 그대로 쓴다.

        short2 → title_lines·thumbnail·music·제목·설명을 2부 값으로 바꾸고, targets 의 "short2" 를 "short" 로.
        클립 순서(인덱스)는 그대로라 thumbnail.clip 이 같은 클립을 가리킨다.
        """
        from dataclasses import replace

        split = bool(self.outputs.get("short2"))
        if target != "short2":
            return replace(self, part_no=1 if split and target == "short" else 0)
        p = self.short2 or ShortPart()
        clips = [replace(c, targets=["short" if t == "short2" else t for t in c.targets if t != "short"])
                 for c in self.clips]
        return replace(
            self, clips=clips, title_lines=p.title_lines, thumbnail=p.thumbnail, music=p.music or self.music,
            youtube_title=p.youtube_title, description=p.description or self.description,
            instagram_caption=p.instagram_caption, hashtags=p.hashtags or self.hashtags,
            outputs={"short": True, "long": False}, short2=None, part_no=2,
        )

    def fill_ends(self) -> None:
        """end 가 비어 있는 클립을 원본 길이로 채운다."""
        for c in self.clips:
            if c.end is None:
                c.end = probe(config.resolve(c.src))["duration_s"]


def _check_keys(cls: type, raw: dict, where: str) -> None:
    unknown = set(raw) - set(cls.__dataclass_fields__)
    if unknown:
        raise ValueError(f"{where}: 알 수 없는 필드 {sorted(unknown)}")


# --- 검증 ---------------------------------------------------------------------------


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def print(self) -> None:
        for e in self.errors:
            print(f"  ✗ {e}")
        for w in self.warnings:
            print(f"  ! {w}")
        if self.ok and not self.warnings:
            print("  ✓ 문제 없음")


def has_emoji(text: str) -> bool:
    return bool(_EMOJI.search(text))


def display_width(text: str) -> int:
    """한글/전각 2, 나머지 1 로 센 폭. 자막 줄 길이 점검용."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


# 편집 강도 점검: '최소치'와 '예산(최대치)' 둘 다 본다.
# [공개 사본] 기준값의 근거·채널 스타일 가이드는 비공개 문서에 있다
FUN_MIN = {"sfx": 2, "stickers": 1, "moves": 2, "big": 1}
FUN_BUDGET_PER_30S = {"stickers": 2, "sfx": 3, "punch": 2, "big": 2}  # 쇼츠 30초당 최대
JOKE_CAPTION_RATIO = 0.3  # 전체 자막 중 big(펀치라인) 비율 상한
READ_CPS = 13.0  # 자막 읽기 속도 (글자/초)
MIN_CAPTION_S = 1.0
PUNCH_MAX_SCALE = 1.25
PUNCHLINE_HOLD_S = 1.2  # 펀치라인 자막 뒤 그 장면이 유지돼야 하는 시간
CALM_TRACKS = {"wallpaper"}  # 쇼츠 전체 BGM 으로는 잔잔함. 롱폼 배경용
# 아이 성별 호칭 (사용자가 정하기 전까지 금지, CLAUDE.md). '오빠'는 엄마가 아빠를 부르는 말이라 제외
GENDERED_WORDS = ("아가씨", "공주님", "공주", "왕자님", "왕자", "우리 딸", "우리 아들", "딸내미", "아들내미", "누나", "언니", "형아")


def _effects(clips: list["Clip"]) -> list[tuple[float, str]]:
    """쇼츠 타임라인 기준 (시각, 종류) 목록: sticker / sfx / punch / big."""
    out, t0 = [], 0.0
    for c in clips:
        for st in c.stickers:
            out.append((t0 + st.at, "sticker"))
            if st.sfx:
                out.append((t0 + st.at, "sfx"))
        for s in c.sfx:
            out.append((t0 + s.at, "sfx"))
        for cap in c.captions:
            if cap.sfx:
                out.append((t0 + cap.at, "sfx"))
            if cap.style == "big":
                out.append((t0 + cap.at, "big"))
        for p in c.punch:
            out.append((t0 + p.at, "punch"))
        t0 += c.duration
    return sorted(out)


MUSIC_SECTION_MIN = {"short": 6.0, "long": 15.0}  # 곡을 바꾼 뒤 최소 이만큼은 이어져야 (너무 잦은 전환 방지)
MUSIC_SWITCH_MAX = {"short": 1, "long": 8}
LONG_ONE_TRACK_MAX = 90.0  # 롱폼이 이보다 길면 장면(챕터)에 맞게 곡을 바꾸는 것을 권장


def music_sections(clips: list["Clip"], base: str | None) -> list[tuple[float, float, str | None]]:
    """[(시작초, 끝초, 곡 id 또는 None=음악 없음)] (그 대상 타임라인 기준, 클립 길이 합으로 계산)."""
    out: list[list] = []
    t = 0.0
    cur = base or None
    for c in clips:
        if c.music is not None:
            nxt = None if c.music == "off" else c.music
            if not out or nxt != cur:
                if out:
                    out[-1][1] = t
                cur = nxt
                out.append([t, None, cur])
        elif not out:
            out.append([0.0, None, cur])
        t += c.duration
    if not out:
        out.append([0.0, t, cur])
    out[-1][1] = t
    # 0초 길이 구간 정리 (맨 앞 클립이 바로 곡을 바꾸는 경우)
    return [(a, b, k) for a, b, k in out if b - a > 1e-6]


def music_report(clips: list["Clip"], base: "Music | None", target: str, recent: list[str]) -> tuple[list[str], list[str]]:
    """(오류, 경고): 곡 id·플랫폼, 전환 횟수·구간 길이, 롱폼 한 곡 반복, 최근 편과 같은 곡."""
    errors, warnings = [], []
    base_id = base.track if base and base.track else None
    secs = music_sections(clips, base_id)
    for c in clips:
        if c.music and c.music != "off":
            e = music.entry(c.music)
            if not e:
                errors.append(f"clips[].music '{c.music}' 은 카탈로그에 없습니다 (./br music)")
            elif not config.resolve(e["file"]).exists():
                errors.append(f"clips[].music '{c.music}' 파일이 없습니다 → ./br setup")
            elif target == "short" and "instagram" not in e.get("platforms", []):
                errors.append(f"clips[].music '{c.music}' 은 Instagram 허용 음원이 아닙니다 (쇼츠 = 릴스 공용 파일)")
    switches = len(secs) - 1
    if switches > MUSIC_SWITCH_MAX[target]:
        warnings.append(f"BGM 전환 {switches}번 > {MUSIC_SWITCH_MAX[target]}번 → 장면이 크게 바뀔 때만 (산만함)")
    for i, (a, b, k) in enumerate(secs):
        if i < len(secs) - 1 and b - a < MUSIC_SECTION_MIN[target]:
            warnings.append(f"BGM '{k or 'off'}' 구간이 {b - a:.1f}s 로 짧습니다 (최소 {MUSIC_SECTION_MIN[target]:.0f}s)")
    if target == "long":
        total = secs[-1][1] if secs else 0.0
        tracks = {k for _, _, k in secs if k}
        if total > LONG_ONE_TRACK_MAX and len(tracks) < 2:
            warnings.append(f"롱폼 {total:.0f}s 가 BGM 한 곡({base_id}) → 챕터 분위기에 맞게 clips[].music 으로 바꾸기 "
                            "(./br music suggest <상황>)")
    elif base_id and base_id in recent[:2]:
        warnings.append(f"BGM '{base_id}' 은 최근 편에서 썼습니다 → 다른 곡 추천: " +
                        ", ".join(e["id"] for e in music.suggest_like(base_id, recent)))
    return errors, warnings


THUMB_LINE_MAX = 7  # 줄당 글자 (공백 제외). 넘으면 글자가 작아져 휴대폰에서 안 읽힌다


def thumbnail_report(plan: "Plan") -> list[str]:
    """썸네일 기획 점검: 글자 수·줄 수, 제목 반복, 어른과 함께인데 주인공 얼굴 미지정."""
    out = []
    th = plan.thumbnail
    lines = th.text or plan.title_lines
    if not th.text:
        out.append("thumbnail.text 가 비어 제목을 그대로 씁니다 → 제목과 다른 '궁금한 한 방' 1~2줄로")
    if len(lines) > 2:
        out.append(f"썸네일 글자 {len(lines)}줄 → 2줄 이하")
    for line in lines:
        n = len(line.replace(" ", ""))
        if n > THUMB_LINE_MAX:
            out.append(f"썸네일 '{line}' {n}자 → 줄당 {THUMB_LINE_MAX}자 이하 (작아지면 휴대폰에서 안 읽힘)")
    title = "".join(plan.title_lines).replace(" ", "")
    if th.text and "".join(th.text).replace(" ", "") == title:
        out.append("썸네일 글자가 제목과 똑같습니다 → 제목은 설명, 썸네일은 궁금증 (예: '모델이 잠듦ㅋㅋ')")
    for f in th.faces:
        if len(f) != 2 or not all(0 <= v <= 1 for v in f):
            out.append(f"thumbnail.faces {f}: [x, y] 0~1")
    if 0 <= th.clip < len(plan.clips) and plan.clips[th.clip].covers:
        out.append("썸네일 장면에 가림 스티커(covers)가 필요한 장면(목욕 등)입니다 → 다른 장면으로")
    if 0 <= th.clip < len(plan.clips):
        c = plan.clips[th.clip]
        adults = [p for p in c.people if p not in {ch["nick"] for ch in config.children()}]
        if adults and not th.faces:
            out.append(f"썸네일 장면에 {', '.join(adults)} 도 나옵니다 → thumbnail.faces 로 아기 얼굴(또는 함께 담을 얼굴)을 지정")
    return out


def fun_report(clips: list["Clip"], music: "Music | None") -> list[str]:
    """쇼츠의 웃음 장치: 너무 적으면(밋밋) 또는 너무 많으면(과함) 알려 준다."""
    eff = _effects(clips)
    n = {k: sum(1 for _, kind in eff if kind == k) for k in ("sticker", "sfx", "punch", "big")}
    moves_n = n["punch"] + n["big"] + sum(1 for c in clips if c.hold)
    out = []
    if n["sfx"] < FUN_MIN["sfx"]:
        out.append(f"효과음 {n['sfx']}개 < {FUN_MIN['sfx']}")
    if n["sticker"] < FUN_MIN["stickers"]:
        out.append(f"스티커 {n['sticker']}개 < {FUN_MIN['stickers']}")
    if n["big"] < FUN_MIN["big"]:
        out.append("펀치라인 big 자막 없음")
    if moves_n < FUN_MIN["moves"]:
        out.append(f"펀치줌·정지·big 자막 {moves_n}개 < {FUN_MIN['moves']}")
    if music and music.track in CALM_TRACKS:
        out.append(f"BGM '{music.track}' 은 잔잔한 곡 (유쾌한 곡 권장: fluffing_a_duck, monkeys_spinning_monkeys, merry_go…)")
    return out


def restraint_report(clips: list["Clip"]) -> list[str]:
    """과함 점검 (STYLE 11번): 30초당 효과 예산, 한 순간에 몰린 효과, 펀치라인 비율·여운, 자막 읽기 시간."""
    out = []
    total = sum(c.duration for c in clips) or 1.0
    eff = _effects(clips)
    scale = max(1.0, total / 30.0)
    names = {"stickers": "sticker", "sfx": "sfx", "punch": "punch", "big": "big"}
    labels = {"stickers": "스티커", "sfx": "효과음", "punch": "펀치줌", "big": "big 자막"}
    for k, kind in names.items():
        cnt = sum(1 for _, x in eff if x == kind)
        cap = round(FUN_BUDGET_PER_30S[k] * scale)
        if cnt > cap:
            out.append(f"과함: {labels[k]} {cnt}개 > {cap}개 (30초당 {FUN_BUDGET_PER_30S[k]}개). 결정적 순간에만")
    # 한 순간(0.5초 안)에 효과 3종 이상이 몰림 (스티커+효과음+펀치줌+big 동시 = 시끄러움)
    for i, (t, _) in enumerate(eff):
        kinds = {k for tt, k in eff if t <= tt < t + 0.5}
        if len(kinds) >= 3:
            out.append(f"과함: {t:.1f}s 에 {', '.join(sorted(kinds))} 가 한꺼번에 → 둘 이하로 (웃음은 한 가지 장치로)")
            break
    caps = [cap for c in clips for cap in c.captions]
    bigs = [cap for cap in caps if cap.style == "big"]
    if len(caps) >= 4 and len(bigs) / len(caps) > JOKE_CAPTION_RATIO:
        out.append(f"과함: 펀치라인(big) 자막이 {len(bigs)}/{len(caps)} → 대부분은 담담한 기록 자막이어야 웃음이 산다")
    t0 = 0.0
    for c in clips:
        for cap in c.captions:
            chars = len(cap.text.replace("\n", "").replace(" ", ""))
            need = max(MIN_CAPTION_S, chars / READ_CPS)
            if cap.dur + 0.05 < need and cap.style in ("sub", "talk"):
                out.append(f"자막이 빨리 사라짐: '{cap.text[:12]}' {cap.dur:.1f}s < {need:.1f}s ({chars}자 ÷ 13자/초)")
            if cap.style == "big" and c.duration - cap.at < PUNCHLINE_HOLD_S:
                out.append(f"펀치라인 '{cap.text[:10]}' 뒤 장면이 {c.duration - cap.at:.1f}s 뿐 → {PUNCHLINE_HOLD_S}s 이상 여운")
            if cap.style == "big" and cap.voice:
                out.append(f"펀치라인 '{cap.text[:10]}' 에 AI 음성 → 펀치라인은 정적이나 실제 소리로")
        for pch in c.punch:
            if pch.scale > PUNCH_MAX_SCALE:
                out.append(f"펀치줌 {pch.scale}배 > {PUNCH_MAX_SCALE}배 ({Path(c.src).name}) → 1.1~1.2배가 자연스러움")
        t0 += c.duration
    return out


def pacing_report(clips: list["Clip"]) -> list[str]:
    """쇼츠 템포 점검: 첫 훅, 평균 컷 길이, 변화 없이 긴 구간."""
    out = []
    if not clips:
        return out
    first = clips[0]
    if not first.captions or min(c.at for c in first.captions) > 0.6:
        out.append("첫 0.6초 안에 자막(훅)이 없습니다. 첫 화면에서 바로 상황을 알려 주세요")
    if first.duration > 3.5:
        out.append(f"첫 컷이 {first.duration:.1f}s 로 깁니다 (3초 안쪽 권장, 가장 강한 장면을 맨 앞에)")
    avg = sum(c.duration for c in clips) / len(clips)
    if avg > 4.0:
        out.append(f"평균 컷 길이 {avg:.1f}s (4초 이하 권장)")
    for i, c in enumerate(clips):
        if c.duration > 6 and not (c.punch or c.stickers or len(c.captions) >= 3):
            out.append(f"clips {Path(c.src).name} {c.duration:.1f}s 동안 변화가 적습니다 (펀치줌·스티커·자막 추가 또는 컷 분할)")
    return out


def long_report(clips: list["Clip"]) -> list[str]:
    """롱폼: 억지로 늘린 흔적(슬로모션·같은 장면 반복·말 없는 긴 정적 컷)을 잡는다 (CONCEPT 13번)."""
    out = []
    slow = [c for c in clips if c.speed < 0.9 and not c.hold]
    if len(slow) > 2:
        out.append(f"슬로모션 {len(slow)}개 → 길이를 늘리려는 슬로모션은 지루함. 결정적 순간 1~2번만")
    seen: dict[str, list[tuple[float, float]]] = {}
    for c in clips:
        rng = (c.start, c.end or c.start)
        for a, b in seen.get(c.src, []):
            if rng[0] < b - 0.5 and a < rng[1] - 0.5:
                out.append(f"같은 장면 반복: {Path(c.src).name} {a:.0f}~{b:.0f}s 와 {rng[0]:.0f}~{rng[1]:.0f}s 가 겹침")
        seen.setdefault(c.src, []).append(rng)
    for c in clips:
        if c.duration > 10 and c.mute and len(c.captions) < 2 and not c.punch:
            out.append(f"{Path(c.src).name} {c.duration:.0f}s 동안 소리도 자막도 거의 없음 → 줄이거나 기록 자막·원본 소리로 채우기")
    return out


def _offline_hint(src: Path) -> str:
    """원본 폴더가 외장 디스크로 연결(링크)돼 있는데 그 디스크가 빠져 있으면 알려 준다 (파일이 지워진 게 아님)."""
    for parent in [src, *src.parents]:
        if parent.is_symlink():
            target = Path(os.readlink(parent))
            vol = Path(*target.parts[:3]) if target.parts[:2] == ("/", "Volumes") else None
            if vol and not vol.exists():
                return f" → 외장 디스크 {vol.name} 가 연결돼 있지 않음 (파일은 그 디스크에 있음, 연결 후 다시)"
            break
    return ""


OPENING_SECONDS = 6.0  # 쇼츠 첫 6초 (시청자가 넘길지 정하는 구간)
PAYOFF_BY = 4.0  # 이 안에 반응(big 자막·실제 대사)이 하나는 나오게


def _opening_warnings(clips: list[Clip]) -> list[str]:
    """쇼츠 첫 6초 점검: 장면 전환 수와 첫 반응 시점.
    [공개 사본] 기준의 근거(채널 시청 지속 데이터 분석)는 비공개 문서에 있다."""
    out, t, srcs, payoff = [], 0.0, [], None
    for c in clips:
        if c.end is None or t >= OPENING_SECONDS:
            break
        srcs.append(c.src)
        for cap in c.captions:
            if cap.style in ("big", "talk") and payoff is None and t + cap.at < PAYOFF_BY:
                payoff = t + cap.at
        t += c.duration
    if len(set(srcs)) > 1:
        out.append(f"쇼츠 첫 {OPENING_SECONDS:.0f}초에 장면이 {len(set(srcs))}개 → 한 장면 안에서 설정→반응까지 이어 가기 "
                   "(스타일 가이드 참고)")
    if srcs and payoff is None:
        out.append(f"쇼츠 첫 {PAYOFF_BY:.0f}초 안에 반응(big 자막 또는 실제 대사)이 없습니다 → 웃긴 반응을 4초 안으로 "
                   "(docs/STYLE.md 2-1)")
    return out


# 스타일별 한 줄 최대 폭 (1080 폭 기준, 한글 1자 = 2)
_MAX_WIDTH = {"sub": 34, "talk": 26, "big": 14, "chip": 20, "title": 20}


def validate(plan: Plan, for_package: bool = False, require_review: bool = False) -> Report:
    """쇼츠 1부(+롱폼)를 검사하고, 2부가 있으면 2부 보기도 같은 규칙으로 검사한다."""
    r = _validate_one(plan.part("short"), for_package, require_review)
    if not plan.outputs.get("short2"):
        if plan.outputs.get("short") and plan.outputs.get("long"):
            r.warnings.append("쇼츠 2부(short2)가 없습니다 → 내일 올릴 쇼츠가 비어요 "
                              "(한 번에 쇼츠 2개 + 롱폼 1개, docs/CONCEPT.md 12번)")
        return r
    if plan.short2 is None:
        r.errors.append("outputs.short2 가 켜져 있는데 short2(2부 제목·썸네일)가 없습니다")
        return r
    r2 = _validate_one(plan.part("short2"), for_package, require_review)
    seen = set(r.errors) | set(r.warnings)
    r.errors += [f"[쇼츠 2부] {e}" for e in r2.errors if e not in seen]
    r.warnings += [f"[쇼츠 2부] {w}" for w in r2.warnings if w not in seen]
    # 2부 전용 규칙
    if plan.short2.youtube_title and plan.short2.youtube_title == plan.youtube_title:
        r.errors.append("short2.youtube_title 이 1부와 같습니다 (2부는 다른 제목)")
    if plan.short2.title_lines and plan.short2.title_lines == plan.title_lines:
        r.warnings.append("short2.title_lines 가 1부와 같습니다 (피드에서 같은 영상처럼 보임)")
    one = [c for c in plan.clips if "short" in c.targets]
    two = [c for c in plan.clips if "short2" in c.targets]
    for a in one:
        for b in two:
            if a.src == b.src and a.start < (b.end if b.end is not None else 1e9) and \
                    b.start < (a.end if a.end is not None else 1e9):
                r.warnings.append(f"1부와 2부가 같은 장면을 씁니다: {Path(a.src).name} "
                                  "(이틀 연속 같은 장면 → 겹치지 않게 나누기)")
    if plan.outputs.get("long"):
        first = next((c for c in plan.clips if "long" in c.targets), None)
        cap = first.captions[0] if first and first.captions else None
        if cap and cap.style == "sub" and cap.at < 1.0 and "육아일지" in cap.text:
            # 롱폼 첫 장면이 회차 설명 자막이면 경고 (근거는 비공개 스타일 가이드)
            r.warnings.append(f"롱폼 첫 장면이 설명 자막 '{cap.text.splitlines()[0]}' 으로 시작합니다 → 실제 대사·반응으로 시작하고 "
                              "회차 설명은 chip 이나 설명란으로 (docs/STYLE.md 9번)")
        cfg = config.load()
        pc = Plan.from_dict(asdict(plan))
        pc.fill_ends()
        total = sum(c.duration for c in pc.clips_for("long")) + cfg["long"]["outro_seconds"]
        if total < cfg["long"].get("min_seconds", 120):
            # 억지로 늘리지 말고, 안 쓴 진짜 재료(대화·맥락)가 있으면 그걸로 (CONCEPT 13번)
            unused = _unused_dialogue(plan)
            if unused:
                ex = " / ".join(f"{Path(src).name} {a:.0f}s '{t[:12]}'" for src, a, t in unused[:3])
                r.warnings.append(f"롱폼 {total:.0f}s: 롱폼에 아직 안 쓴 실제 대화 {len(unused)}개 ({ex}) → "
                                  "이야기에 도움이 되면 맥락 장면으로 넣기 (억지로 늘리지는 않기)")
    return r


def _unused_dialogue(plan: Plan) -> list[tuple[str, float, str]]:
    """이번 편 원본들에서, 롱폼 클립에 들어가지 않은 '진짜 대사' (원본, 시각, 문장)."""
    from . import transcribe as stt

    long_rng: dict[str, list[tuple[float, float]]] = {}
    for c in plan.clips:
        if "long" in c.targets:
            long_rng.setdefault(c.src, []).append((c.start, c.end or c.start))
    out = []
    for src in dict.fromkeys(c.src for c in plan.clips):
        path = config.resolve(src)
        if not path.exists() or probe(path)["still"] or not probe(path)["has_audio"]:
            continue
        try:
            lines = [x for x in stt.speech_lines(path) if x["real"]]
        except Exception:  # noqa: BLE001
            continue
        for x in lines:
            if not any(a - 0.2 <= x["start"] and x["end"] <= b + 0.2 for a, b in long_rng.get(src, [])):
                out.append((src, x["start"], x["text"]))
    return out


def _validate_one(plan: Plan, for_package: bool = False, require_review: bool = False) -> Report:
    from . import sfx as sfx_mod

    cfg = config.load()
    speakers = cfg.get("speakers", {})
    r = Report()

    if not plan.clips:
        r.errors.append("clips 가 비어 있습니다")
    if plan.series not in cfg.get("series", {}):
        r.errors.append(f"series '{plan.series}' 가 channel.toml [series] 에 없습니다 ({', '.join(cfg.get('series', {}))})")
    nicks = [c["nick"] for c in cfg.get("children", [])]
    for name in plan.cast:
        if name not in nicks:
            r.errors.append(f"cast '{name}' 은 channel.toml [[children]] 의 nick 이 아닙니다 ({', '.join(nicks)})")
    if not plan.cast:
        r.warnings.append("cast 가 비어 있습니다 (등장하는 아이: " + ", ".join(nicks) + ")")
    if plan.episode_no and not plan.story_notes.strip():
        r.warnings.append("story_notes 가 비어 있습니다 (연재 기록 docs/SERIES.md 에 들어갈 이번 편 요약 1~2줄)")
    full_names = [c["name"] for c in cfg.get("children", []) if c.get("name")]
    texts = [plan.youtube_title, plan.youtube_long_title, plan.description, plan.instagram_caption,
             *plan.title_lines, *(cap.text for c in plan.clips for cap in c.captions)]
    for t in (plan.youtube_title, plan.youtube_long_title, plan.description, plan.instagram_caption):
        if re.search(r"#\d", t or ""):
            r.warnings.append(f"'#숫자' 는 해시태그로 인식됩니다 → 'No.7' 처럼 쓰세요: '{t[:30]}'")
    if not cfg.get("family", {}).get("allow_gendered", False):
        ok_words = set(cfg.get("family", {}).get("terms_ok", []))  # 사용자가 직접 쓴 호칭 (예: 사촌'언니')
        for t in texts:
            hit = next((w for w in GENDERED_WORDS if w in (t or "") and w not in ok_words), None)
            if hit:
                r.errors.append(f"아이 성별 호칭 '{hit}' 은 아직 쓰지 않습니다 (사용자가 정하기 전까지): '{(t or '')[:20]}'")
    for fn in full_names:
        if any(fn in t for t in texts):
            r.warnings.append(f"아이의 성+이름 '{fn}' 이 노출됩니다. 애칭만 쓰는 것을 권장 (docs/POLICY.md)")
    if plan.layout not in LAYOUTS:
        r.errors.append(f"layout 은 {LAYOUTS} 중 하나")
    for line in plan.title_lines:
        if has_emoji(line):
            r.errors.append(f"title_lines 에 이모지는 렌더링되지 않습니다 → '{line}'")
        if display_width(line) > _MAX_WIDTH["title"]:
            r.warnings.append(f"title_lines '{line}' 이 깁니다 (한 줄 10자 내외 권장)")
    if len(plan.title_lines) > 2:
        r.errors.append("title_lines 는 최대 2줄")
    if plan.outputs.get("short") and plan.layout == "frame" and not plan.title_lines:
        r.warnings.append("frame 레이아웃인데 title_lines 가 없습니다 (상단 고정 제목이 핵심 훅)")
    r.warnings += _opening_warnings([c for c in plan.clips if "short" in c.targets])

    def check_sfx(name: str | None, where: str) -> None:
        if name and name not in sfx_mod.CATALOG and not (sfx_mod.SFX_DIR / f"{name}.wav").exists():
            r.errors.append(f"{where}: 알 수 없는 효과음 '{name}' (사용 가능: {', '.join(sfx_mod.CATALOG)})")

    for i, c in enumerate(plan.clips):
        tag = f"clips[{i}] {Path(c.src).name}"
        src = config.resolve(c.src)
        if not src.exists():
            r.errors.append(f"{tag}: 파일 없음 ({c.src})" + _offline_hint(src))
            continue
        info = probe(src)
        end = c.end if c.end is not None else info["duration_s"]
        if info["still"]:
            # 사진: start=0, end=보여줄 길이(초). 켄번즈 줌 자동
            if not 0.8 <= end - c.start <= 8:
                r.errors.append(f"{tag}: 사진 노출 시간(end-start)은 0.8~8초")
            if max(info["width"], info["height"]) < 1280:
                r.warnings.append(f"{tag}: 사진 해상도 {info['width']}x{info['height']} → 확대돼 흐려집니다 (원본 사진 권장)")
        elif c.hold:
            if c.start > info["duration_s"]:
                r.errors.append(f"{tag}: hold 프레임 start({c.start}) 가 원본 밖입니다")
            if end - c.start > 2.0:
                r.warnings.append(f"{tag}: 정지 화면 {end - c.start:.1f}s — 2초가 넘으면 영상이 끊긴 것처럼 보입니다 "
                                  "(내레이션 길이를 맞출 땐 정지 대신 speed 0.5 슬로모션이나 다음 컷으로)")
        else:
            if not 0 <= c.start < end:
                r.errors.append(f"{tag}: start({c.start}) < end({end}) 이어야 합니다")
            if end > info["duration_s"] + 0.05:
                r.errors.append(f"{tag}: end({end}) 가 원본 길이({info['duration_s']:.2f}s)보다 깁니다")
        if c.fit not in ("auto", "fill", "blur"):
            r.errors.append(f"{tag}: fit 은 auto|fill|blur")
        if not (0 <= c.focus <= 1 and 0 <= c.focus_y <= 1):
            r.errors.append(f"{tag}: focus/focus_y 는 0~1")
        if not 0.25 <= c.speed <= 4:
            r.errors.append(f"{tag}: speed 는 0.25~4")
        if c.crop and (len(c.crop) != 4 or not all(0 <= v < 60 for v in c.crop)):
            r.errors.append(f"{tag}: crop 은 [왼, 위, 오른, 아래] 퍼센트 4개 (각 0~60)")
        if c.zoom < 1:
            r.errors.append(f"{tag}: zoom 은 1.0 이상 (화면에 빈 곳이 생김)")
        for j, m in enumerate(c.mute_ranges):
            if len(m) != 2 or not m[0] < m[1]:
                r.errors.append(f"{tag} mute_ranges[{j}]: [시작, 끝] (원본 초, 시작 < 끝). 클립 밖 구간은 무시됨")
        for j, b in enumerate(c.blur_faces):
            if len(b) != 2 or not all(0 <= v <= 1 for v in b):
                r.errors.append(f"{tag} blur_faces[{j}]: [x, y] 0~1 (화면에서 가릴 얼굴 위치)")
        for j, b in enumerate(c.blur_areas):
            boxes = [k[1] for k in b.keys()]
            if bool(b.box) == bool(b.points):
                r.errors.append(f"{tag} blur_areas[{j}]: box 또는 points 중 하나만")
            elif any(len(p) != 5 for p in b.points) or not all(
                    len(x) == 4 and all(0 <= v <= 1 for v in x) and x[0] < x[2] and x[1] < x[3] for x in boxes):
                r.errors.append(f"{tag} blur_areas[{j}]: box [x0, y0, x1, y1] (0~1, x0<x1, y0<y1), "
                                "points [[원본 초, x0, y0, x1, y1], ...]")
        for j, cv in enumerate(c.covers):
            if not cv.points or any(len(pt) != 3 or not all(0 <= v <= 1 for v in pt[1:]) for pt in cv.points):
                r.errors.append(f"{tag} covers[{j}].points: [[원본 초, x, y], ...] (x·y 는 0~1)")
            elif not 0.05 <= cv.size <= 1.0:
                r.errors.append(f"{tag} covers[{j}].size: 0.05~1.0 (원본 가로 대비)")
            if not has_emoji(cv.emoji):
                r.errors.append(f"{tag} covers[{j}].emoji: 이모지 1개")
        # 목욕 장면은 몸 주변을 가림 스티커로 (2026-10-22 사용자 요청). 자막·src 에 목욕이 있는데 covers 가 없으면 경고
        bath = any(k in (cap.text or "") for cap in c.captions for k in ("목욕", "물놀이", "욕조"))
        cut_low = len(c.crop) == 4 and c.crop[3] >= 15  # 하반신을 잘라 낸 구도 (2026-10-22 사용자: 하반신 안 나오는 부분만)
        if bath and not (c.covers or cut_low):
            r.warnings.append(f"{tag}: 목욕 장면 → 하반신이 안 나오는 짧은 구간만, 튜브 아래를 crop 으로 잘라 내세요 "
                              "(오리 스티커만으로는 부족, docs/POLICY.md 0번). preview 에서 모든 프레임 확인")
        if info["hdr"]:
            r.warnings.append(f"{tag}: HDR 원본 → SDR 로 톤매핑됩니다. 색이 달라 보이면 폰에서 HDR 끄고 촬영 권장")
        length = (end - c.start) / (1.0 if c.hold else c.speed)

        def in_clip(at: float, dur: float, where: str) -> None:
            if at < 0 or at + 0.05 > length:
                r.errors.append(f"{where}: at={at} 가 클립 길이({length:.2f}s) 밖입니다")
            elif at + dur > length + 0.01:
                r.warnings.append(f"{where}: 클립 끝에서 잘립니다 ({at}+{dur} > {length:.2f})")

        for j, cap in enumerate(c.captions):
            ctag = f"{tag} captions[{j}]"
            if cap.style not in CAPTION_STYLES:
                r.errors.append(f"{ctag}: style 은 {CAPTION_STYLES} 중 하나")
                continue
            if has_emoji(cap.text):
                r.errors.append(f"{ctag}: 화면 자막에 이모지는 렌더링되지 않습니다 → stickers 로 넣으세요 '{cap.text}'")
            if cap.style == "talk" and cap.speaker and cap.speaker not in speakers:
                r.errors.append(f"{ctag}: speaker '{cap.speaker}' 가 channel.toml [speakers] 에 없습니다")
            check_sfx(cap.sfx, ctag)
            in_clip(cap.at, cap.dur, ctag)
            if cap.voice:
                vname = cap.voice if isinstance(cap.voice, str) else (cap.speaker or "narrator")
                if vname not in cfg.get("voices", {}):
                    r.errors.append(f"{ctag}: 음색 '{vname}' 이 channel.toml [voices] 에 없습니다")
                if cfg.get("voices", {}).get("engine") == "say":
                    r.warnings.append(f"{ctag}: TTS 엔진 say(macOS)는 비상업 전용 → 업로드 전 supertonic 으로 바꾸세요")
            longest = max((display_width(line) for line in cap.text.split("\n")), default=0)
            if longest > _MAX_WIDTH[cap.style]:
                r.warnings.append(f"{ctag}: 한 줄이 깁니다(폭 {longest} > {_MAX_WIDTH[cap.style]}). \\n 으로 줄바꿈")
        by_style: dict[str, list[Caption]] = {}
        for cap in c.captions:
            by_style.setdefault(cap.style, []).append(cap)
        for style, caps in by_style.items():
            caps = sorted(caps, key=lambda x: x.at)
            for a, b in zip(caps, caps[1:]):
                if a.at + a.dur > b.at + 0.05:
                    r.warnings.append(f"{tag}: '{a.text[:10]}…' 와 '{b.text[:10]}…' ({style}) 가 "
                                      f"{a.at + a.dur - b.at:.1f}s 겹칩니다 → 앞 자막이 자동으로 줄어듭니다. dur 조정 권장")
        for j, s in enumerate(c.sfx):
            check_sfx(s.name, f"{tag} sfx[{j}]")
            in_clip(s.at, 0, f"{tag} sfx[{j}]")
        for j, s in enumerate(c.stickers):
            check_sfx(s.sfx, f"{tag} stickers[{j}]")
            in_clip(s.at, s.dur, f"{tag} stickers[{j}]")
            if not has_emoji(s.emoji):
                r.errors.append(f"{tag} stickers[{j}]: emoji 에 컬러 이모지를 넣으세요")
        for j, p in enumerate(c.punch):
            in_clip(p.at, p.dur, f"{tag} punch[{j}]")
            if not 1 < p.scale <= 3:
                r.errors.append(f"{tag} punch[{j}]: scale 은 1~3")
        for unknown in set(c.targets) - {*SHORT_TARGETS, "long"}:
            r.errors.append(f"{tag}: targets 에 알 수 없는 값 '{unknown}'")

    if any(has_emoji(t) for t in plan.thumbnail.text):
        r.errors.append("thumbnail.text 에 이모지는 렌더링되지 않습니다")
    if plan.series == "challenge":
        from . import challenges

        ch = plan.challenge or {}
        if not ch.get("text"):
            r.errors.append("series 'challenge' 는 challenge.text(챌린지 내용)가 필요합니다 (./br challenge new <번호>)")
        else:
            months = challenges.months_old(ch.get("child") or "첫째", date.fromisoformat(plan.date))
            v = challenges.screen(ch["text"], months)
            if not v.ok:
                r.errors.append("챌린지 안전 기준에 맞지 않습니다: " + " / ".join(v.reasons) + " (docs/CONCEPT.md 14번)")
            for n in v.notes:
                r.warnings.append(f"챌린지 주의: {n}")
    for q in thumbnail_report(plan):
        r.warnings.append(f"썸네일 (docs/STYLE.md 13번): {q}")
    if not 0 <= plan.thumbnail.clip < max(len(plan.clips), 1):
        r.errors.append("thumbnail.clip 인덱스가 범위를 벗어났습니다")

    if plan.music:
        try:
            info = music.resolve(plan.music)
        except (KeyError, FileNotFoundError) as e:
            r.errors.append(f"music: {e}")
        else:
            if not info["license"].strip():
                r.errors.append("music.license 에 출처/라이선스를 기록하세요 (저작권 클레임 대비)")
            if plan.outputs.get("short") and "instagram" not in info["platforms"]:
                r.errors.append(
                    f"music '{info['title']}' 은 Instagram 사용이 허용되지 않은 음원입니다. "
                    "쇼츠 파일은 Reels 에도 올라가므로 양쪽 허용 음원을 쓰세요"
                )

    if r.errors:
        return r

    # 가족 등장: 아기만 나오는 영상만 계속 올리면 수익 창출에 불리 → 엄마·아빠 얼굴이 조금은 나오게
    # (docs/FEEDBACK.md 2026-10-17, CONCEPT 2번)
    parents = set(cfg.get("family", {}).get("parents", ["엄마", "아빠"]))
    allowed = parents | set(cfg.get("family", {}).get("faces_ok", [])) | set(nicks)
    for i, c in enumerate(plan.clips):
        others = [x for x in c.people if x not in allowed]
        if others and not c.blur_faces:
            r.warnings.append(f"clips[{i}] {Path(c.src).name}: 부모 외 인물({', '.join(others)})의 얼굴이 보이면 안 됩니다 "
                              "→ blur_faces 로 흐리게(권장) 또는 crop/zoom 으로 가리고 preview 확인 (얼굴 공개 허락은 channel.toml [family] faces_ok)")
    # 창밖 풍경 (2026-11-03 사용자 요청 "창 밖으로 나오는 장면은 블러, 위치가 특정되지 못하도록"):
    # Vision 장면 분류로 창밖처럼 보이는 칸을 찾아, blur_areas 로 가리지 않았으면 알려 준다 (위치는 대략 3x3 칸)
    if cfg.get("privacy", {}).get("window_check", False):
        from . import outside

        for i, c in enumerate(plan.clips):
            src = config.resolve(c.src)
            if c.window_ok or not src.exists():
                continue
            still = probe(src)["still"]
            found = outside.suspects(outside.scan(src, c.start, c.start if still else (c.end or c.start), 1 if still else 3))
            left = outside.covered(found, [k[1] for b in c.blur_areas for k in b.keys()])
            if left:
                r.warnings.append(f"clips[{i}] {Path(c.src).name}: 창밖 풍경 의심 {outside.describe(left)} → 컨택트시트에서 확인해 "
                                  "창문 영역을 blur_areas 로 흐리게 (집 위치 노출 방지, docs/POLICY.md). 창밖이 아니면 window_ok: true")
    # 말소리 활용 (2026-10-19 "대화나 재미있는 목소리는 살려서"): 음소거한 클립에 진짜 대사가 있으면 알려 준다
    if cfg.get("style", {}).get("speech_hints", True):
        from . import transcribe as stt

        for i, c in enumerate(plan.clips):
            src = config.resolve(c.src)
            if not src.exists() or probe(src)["still"] or not probe(src)["has_audio"]:
                continue
            try:
                lines = [x for x in stt.speech_lines(src, c.start, c.end) if x["real"]]
            except Exception:  # noqa: BLE001  (받아쓰기 없음 등)
                continue
            lines = [x for x in lines if not any(m[0] <= x["start"] and x["end"] <= m[1] for m in c.mute_ranges)]
            if c.mute and lines:
                ex = " / ".join(f"{x['start']:.1f}s '{x['text'][:14]}'" for x in lines[:2])
                r.warnings.append(f"clips[{i}] {Path(c.src).name}: 음소거했지만 실제 대사가 있음 ({ex}) → "
                                  "재미있거나 내용과 관련 있으면 살리고, 가릴 부분만 mute_ranges")
            elif not c.mute and lines and not any(cap.style == "talk" for cap in c.captions):
                r.warnings.append(f"clips[{i}] {Path(c.src).name}: 대사 '{lines[0]['text'][:14]}' 가 들리는데 talk 자막이 없음 "
                                  "(말소리는 자막을 달면 더 잘 전달됨)")
    if plan.thumbnail and 0 <= plan.thumbnail.clip < len(plan.clips) and "엄마" in plan.clips[plan.thumbnail.clip].people:
        r.warnings.append("썸네일에 엄마 얼굴: 아기 중심 + 엄마가 예쁘게 나온 프레임인지 확인 (docs/CONCEPT.md 2-1)")
    for target in [t for t in ("short", "long") if plan.outputs.get(t)]:
        clips = plan.clips_for(target)
        name = "롱폼" if target == "long" else ("쇼츠" + (f" {plan.part_no}부" if plan.part_no else ""))
        if not any(c.people for c in clips):
            r.warnings.append(f"{name}: clips[].people(화면에 나오는 사람)이 비어 있습니다 → 가족 등장 여부를 적어 주세요")
        elif not any(parents & set(c.people) for c in clips):
            r.warnings.append(f"{name}: 엄마·아빠 얼굴이 한 번도 안 나옵니다 → 부모 리액션·안고 있는 장면을 1컷 이상 "
                              "(아기만 나오는 영상은 수익 창출에 불리)")

    plan_copy = Plan.from_dict(asdict(plan))
    plan_copy.fill_ends()
    if plan.outputs.get("short"):
        clips = plan_copy.clips_for("short")
        total = sum(c.duration for c in clips)
        s = cfg["short"]
        if not clips:
            r.errors.append("short 대상 클립이 없습니다")
        elif total > s["hard_max_seconds"]:
            r.errors.append(f"쇼츠 길이 {total:.1f}s > {s['hard_max_seconds']}s")
        elif total > s["max_seconds"]:
            r.warnings.append(f"쇼츠 길이 {total:.1f}s > 권장 {s['max_seconds']}s")
        elif total < 5:
            r.warnings.append(f"쇼츠 길이 {total:.1f}s 가 너무 짧습니다 (Reels 는 최소 3초)")
        # music.cuts(정적)가 원본 소리 없는 클립(음소거·정지·사진) 위에 있으면 완전 무음 → 끊긴 것처럼 들림 (QA 오류)
        if plan.music and plan.music.cuts:
            t = 0.0
            for c in clips:
                info = probe(config.resolve(c.src))
                silent = c.mute or c.hold or info["still"] or not info["has_audio"]
                for c0, c1 in plan.music.cuts:
                    if silent and c0 < t + c.duration and t < c1:
                        r.warnings.append(f"music.cuts [{c0}, {c1}] 가 원본 소리 없는 클립({Path(c.src).name})과 겹칩니다 "
                                          "→ 완전 무음(소리 끊김). 원본 소리가 있는 장면으로 옮기거나 빼세요")
                t += c.duration
        m_err, m_warn = music_report(clips, plan.music, "short", music.recent_tracks(before=plan.date))
        r.errors += m_err
        r.warnings += m_warn
        fun = fun_report(clips, plan.music)
        if fun:
            r.warnings.append("유쾌함 부족 (docs/CONCEPT.md 7번): " + " / ".join(fun))
        for q in restraint_report(clips):
            r.warnings.append(f"절제 (docs/STYLE.md 11번): {q}")
        for q in pacing_report(clips):
            r.warnings.append(f"완성도 (docs/CONCEPT.md 11번): {q}")
    if plan.outputs.get("long"):
        clips = plan_copy.clips_for("long")
        total = sum(c.duration for c in clips) + cfg["long"]["outro_seconds"]
        for q in long_report(clips):
            r.warnings.append(f"롱폼 (docs/CONCEPT.md 13번): {q}")
        m_err, m_warn = music_report(clips, plan.music, "long", [])
        r.errors += m_err
        r.warnings += [f"롱폼 BGM: {q}" for q in m_warn]
        chapters = [c for c in clips if c.chapter]
        if chapters and (len(chapters) < 3 or clips[0].chapter is None):
            r.warnings.append("YouTube 챕터는 첫 클립부터 시작하는 3개 이상의 chapter 가 필요합니다")

    if for_package or plan.youtube_title:
        if not plan.youtube_title:
            r.errors.append("youtube_title 이 비어 있습니다")
        elif len(plan.youtube_title) > 100:
            r.errors.append(f"youtube_title 이 100자를 넘습니다 ({len(plan.youtube_title)})")
        if plan.outputs.get("long") and len(plan.youtube_long_title or plan.youtube_title) > 100:
            r.errors.append("youtube_long_title 이 100자를 넘습니다")
        for h in plan.hashtags:
            if not h.startswith("#") or " " in h:
                r.errors.append(f"해시태그 형식 오류: '{h}' (공백 없이 #으로 시작)")
        n_ig = len(cfg["metadata"]["instagram_hashtags"]) + len(plan.hashtags)
        if n_ig > cfg["metadata"]["instagram_max_hashtags"]:
            r.warnings.append(
                f"Instagram 해시태그 {n_ig}개 → 앞의 {cfg['metadata']['instagram_max_hashtags']}개만 사용"
            )
    if for_package and not plan.safety_reviewed:
        msg = "safety_reviewed=false: 결과 영상을 직접 보고 docs/POLICY.md 체크리스트 확인 후 true 로 바꾸세요"
        # 자동 '공개' 게시(instagram_api)는 사람 확인이 필수. YouTube 비공개 업로드·인스타 폴더는
        # 사람이 공개 버튼을 누르는 시점이 검토이므로 경고만 한다.
        (r.errors if require_review else r.warnings).append(msg)
    return r
