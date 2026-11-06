"""업로드용 메타데이터(제목·설명·태그·해시태그·크레딧) 생성.

플랫폼별로 dict 를 만든다. 이 dict 가 업로더(publish/*)의 입력이다.
수동 업로드든 API 업로드든 같은 dict 를 쓰므로 자동화로 바꿔도 이 부분은 그대로다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from . import config, music
from .plan import SHORT_TARGETS, Plan


def _dedupe(seq: list[str]) -> list[str]:
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def chapters(plan: Plan, segments: list[dict]) -> list[str]:
    """YouTube 챕터: 00:00 부터 시작, 3개 이상, 각 10초 이상일 때만."""
    marks: list[tuple[float, str]] = []
    for seg in segments:
        if not 0 <= seg["clip_index"] < len(plan.clips):
            continue  # 롱폼 미리보기(콜드 오픈) 클립
        title = plan.clips[seg["clip_index"]].chapter
        if title:
            if marks and seg["start"] - marks[-1][0] < 10:
                # 앞 챕터가 10초 미만이면 버리지 않고 합친다 ("챕터A · 챕터B")
                marks[-1] = (marks[-1][0], f"{marks[-1][1]} · {title}")
                continue
            marks.append((seg["start"], title))
    if marks and marks[0][0] <= 15:
        marks[0] = (0.0, marks[0][1])  # 미리보기·오프닝 카드(최대 15초) 뒤에 시작해도 첫 챕터는 00:00
    if len(marks) < 3 or marks[0][0] > 0.5:
        return []
    return [f"{_ts(t)} {title}" for t, title in marks]


def build_metadata(plan: Plan, manifest: dict) -> dict:
    cfg = config.load()
    md = cfg["metadata"]
    ch = cfg["channel"]
    ages = config.ages(date.fromisoformat(plan.date), plan.cast or None)
    label = config.series_label(plan.series)
    # 설명란 서명: "📔 <채널 이름> | 육아일지 No.12 · 남매 대전 · 첫째 4살 · 둘째 8개월"
    # 텍스트에서는 '#7' 이 해시태그로 인식되므로 'No.7' (화면 태그는 '#7' 그대로)

    def music_line(target: str) -> str:
        """그 영상에 실제로 쓴 곡 전부 (장면마다 곡을 바꾸면 여러 줄). CC BY 출처 표기 조건."""
        out = manifest["outputs"].get(target, {})
        used = out.get("music_all") or [out.get("music") or manifest.get("music") or {}]
        return "\n".join(f"🎵 Music: {m['attribution']}" for m in used if m.get("attribution"))

    # 댓글 챌린지: 이번 편이 챌린지면 제안자 크레딧, 실시간에 가까워지면(warmup/live) '챌린지 추천 받아요' 문구
    from . import challenges

    ch_credit = ""
    if plan.series == "challenge" and plan.challenge.get("text"):
        ch_credit = challenges.credit_line(challenges.Idea(id=0, text=plan.challenge["text"],
                                                           source=plan.challenge.get("source", "youtube"),
                                                           author=plan.challenge.get("author", "")))
    ch_cta = challenges.cta()
    challenge_lines = "\n".join(x for x in (ch_credit, ch_cta) if x)
    # 구독·좋아요 부탁 한 줄 (아주 살짝, [cta]). 하이프는 롱폼만 (쇼츠는 하이프 대상 아님)
    cta = cfg.get("cta", {})
    ask_line = cta.get("description", "")
    long_ask = "\n".join(x for x in (ask_line, cta.get("hype_description", "") if cta.get("hype") else "") if x)

    def credits(target: str, view: Plan = plan) -> str:
        """크레딧. AI 음성 고지는 그 영상(쇼츠/롱폼)에 실제로 AI 음성이 들어간 경우에만 붙인다
        (Supertonic 모델 라이선스 OpenRAIL-M 의 'AI 생성 사실 고지' 조건 + 시청자 신뢰)."""
        ai = any(cap.voice for c in view.clips_for("short" if target != "long" else "long") for cap in c.captions)
        engines = set(manifest.get("tts_engines", []))
        if engines == {"clone"}:
            note = "🔊 내레이션은 아빠 목소리를 AI 로 합성했어요."
        elif "clone" in engines:
            note = "🔊 내레이션은 아빠 목소리를 AI 로 합성했고, 속마음 더빙은 AI 음성이에요."
        else:
            note = "🔊 내레이션/더빙은 AI 음성으로 제작했어요."
        return "\n".join(x for x in [music_line(target), note if ai else ""] if x)

    age_tags = [a.replace(" ", "") for a in ages.split(" · ")] if ages else []  # "첫째4살"
    # 연재 연결: "◀ 지난 육아일지 No.N: 제목 (링크)"
    from . import schedule, series

    prev = series.previous(plan.episode_no) if plan.episode_no else None
    prev_line = ""
    if prev:
        u = series.url_of(prev, "youtube_long", "youtube_shorts")
        prev_line = f"◀ 지난 육아일지 No.{prev['episode_no']}: {prev.get('youtube_title', '')}" + (f"\n{u}" if u else "")

    yt_tags = _dedupe(plan.tags + plan.cast + md["default_tags"] + age_tags + ([label.replace(" ", "")] if label else []))
    out: dict = {}
    days = schedule.load().get(plan.id, {})
    publish_time = cfg.get("schedule", {}).get("youtube_publish_time", "")  # 예: "19:00" (감사 승인 후에만)

    def when(video: str) -> dict:
        d = days.get(video, "")
        extra = {"upload_date": d}
        if d and publish_time:
            extra["publish_at"] = f"{d}T{publish_time}:00+09:00"  # 비공개 + 예약 공개
        return extra

    # 쇼츠 → 롱폼 유도 (쇼츠 시청은 수익 창출 '시청 시간'에 안 들어감. 롱폼 공개 시청 시간만 인정)
    from .publish import load_state

    long_url = (load_state(config.EPISODES / plan.id).get("youtube_long") or {}).get("url")
    long_line = ""
    if "long" in manifest["outputs"]:
        long_line = f"▶ 전체 이야기(롱폼): {long_url}" if long_url else \
            f"▶ 전체 이야기는 채널의 롱폼 '육아일지 No.{plan.episode_no}' 에서"
    for target in SHORT_TARGETS:
        if target not in manifest["outputs"]:
            continue
        view = plan.part(target)
        sfx = "" if target == "short" else "_2"
        o = manifest["outputs"][target]
        cover = o.get("cover") or (manifest.get("cover") if target == "short" else None)
        # 1·2부: "육아일지 No.3-1 · 성장 기록 · 첫째 D+21"
        no = f"육아일지 No.{plan.episode_no}" + (f"-{view.part_no}" if view.part_no else "") if plan.episode_no else ""
        sig = "📔 " + " | ".join(x for x in [ch["name"], " · ".join(y for y in (no, label, ages) if y)] if x)
        sig = "\n".join(x for x in [sig, prev_line] if x)
        desc_body = view.description.strip()
        yt_hashtags = _dedupe(view.hashtags + md["youtube_hashtags"])[:5]
        ig_hashtags = _dedupe(md["instagram_hashtags"] + view.hashtags)[: md["instagram_max_hashtags"]]
        out[f"youtube_shorts{sfx}"] = {
            "file": o["file"],
            "thumbnail": cover,  # YouTube 앱에서 쇼츠 썸네일로 직접 지정
            "title": view.youtube_title,
            "description": "\n\n".join(x for x in [desc_body, challenge_lines, long_line, ask_line, sig, credits(target, view),
                                                      " ".join(yt_hashtags + ["#shorts"])] if x),
            "tags": yt_tags,
            "category_id": md["youtube_category_id"],
            "privacy": md["default_privacy"],
            "made_for_kids": md["made_for_kids"],
            "contains_synthetic_media": False,
            "language": ch["language"],
            "pinned_comment": pinned_comment(plan, target),
            "playlist": md.get("playlist_short", ""),
            **when(target),
        }
        caption = view.instagram_caption.strip() or view.youtube_title
        out[f"instagram_reels{sfx}"] = {
            "file": o["file"],
            "cover": cover,
            "caption": "\n\n".join(x for x in [caption, challenge_lines, ask_line, sig, credits(target, view),
                                                  " ".join(ig_hashtags)] if x),
            "share_to_feed": True,
            **when(target),
        }
        # TikTok: 쇼츠와 같은 파일(BGM 이 막히면 BGM 뺀 파일). 캡션 첫 줄이 제목 역할, 해시태그 3~5개
        tt_hashtags = _dedupe(md.get("tiktok_hashtags", md["instagram_hashtags"]) + view.hashtags)[:5]
        tiktok_nobgm = "tiktok" in o.get("nobgm_for", []) and o.get("file_nobgm")
        tiktok_credit = credits(target, view)
        if tiktok_nobgm:  # BGM 을 뺀 파일이므로 음악 크레딧도 뺀다
            tiktok_credit = "\n".join(l for l in tiktok_credit.split("\n") if not l.startswith("🎵"))
        out[f"tiktok{sfx}"] = {
            "file": o["file_nobgm"] if tiktok_nobgm else o["file"],
            "nobgm": bool(tiktok_nobgm),
            "music_search": music.tiktok_search(view.music.track) if view.music and view.music.track else "",
            "cover": cover,
            "caption": "\n\n".join(x for x in [view.youtube_title or caption, sig, tiktok_credit,
                                                  " ".join(tt_hashtags)] if x),
            **when(target),
        }

    if "long" in manifest["outputs"]:
        no = f"육아일지 No.{plan.episode_no}" if plan.episode_no else ""
        sig = "📔 " + " | ".join(x for x in [ch["name"], " · ".join(y for y in (no, label, ages) if y)] if x)
        sig = "\n".join(x for x in [sig, prev_line] if x)
        segs = manifest["outputs"]["long"]["segments"]
        chap = chapters(plan, segs)
        out["youtube_long"] = {
            "file": manifest["outputs"]["long"]["file"],
            "thumbnail": manifest.get("thumbnail"),
            "title": plan.youtube_long_title or plan.youtube_title,
            "description": "\n\n".join(x for x in [
                plan.description.strip(), challenge_lines, long_ask, "\n".join(chap), sig, credits("long"),
                " ".join(_dedupe(plan.hashtags + md["youtube_hashtags"])[:5]),
            ] if x),
            "tags": yt_tags,
            "category_id": md["youtube_category_id"],
            "privacy": md["default_privacy"],
            "made_for_kids": md["made_for_kids"],
            "contains_synthetic_media": False,
            "language": ch["language"],
            "pinned_comment": pinned_comment(plan, "long"),
            "playlist": md.get("playlist_long", ""),
            **when("long"),
        }
    return out


# 고정 댓글: 시청자가 답하기 쉬운 질문 하나 (댓글 수·답글은 추천 신호). 회차마다 돌려 써서 같은 문구가 반복되지 않게
# [공개 사본] 실제 문구는 비공개 설정에 있다. 아래는 구조만 보여 주는 자리표시자
SHORT_QUESTIONS = (
    "<고정 댓글 질문 1 ({nick})>",
    "<고정 댓글 질문 2>",
    "<고정 댓글 질문 3>",
    "<고정 댓글 질문 4>",
)
LONG_QUESTION = "<롱폼 고정 댓글 문구>"


def pinned_comment(plan: Plan, video: str) -> str:
    """영상별 고정 댓글. plan 에 직접 쓴 게 있으면 그것, 없으면 회차 번호로 질문을 돌려 쓴다."""
    nick = (plan.cast or ["첫째"])[0]
    if video == "long":
        if plan.long_pinned_comment:
            return plan.long_pinned_comment
        if plan.next_hook:
            return f"<다음 편 예고 문구>: {plan.next_hook}"
        return LONG_QUESTION
    own = plan.short2.pinned_comment if video == "short2" and plan.short2 else plan.pinned_comment
    if own:
        return own
    i = (plan.episode_no * 2 + (1 if video == "short2" else 0)) % len(SHORT_QUESTIONS)
    return SHORT_QUESTIONS[i].format(nick=nick)


def write(episode_dir: Path, plan: Plan) -> dict:
    manifest = json.loads((episode_dir / "build.json").read_text(encoding="utf-8"))
    meta = build_metadata(plan, manifest)
    (episode_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta
