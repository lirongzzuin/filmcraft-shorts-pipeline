"""YouTube 규정·한도 점검 (업로드 전, 로그인 없이). publish 가 실행하고 오류가 있으면 멈춘다.

API 로 확인할 수 있는 '올린 뒤' 상태(거부·국가 차단·연령 제한·비공개)는 insights.policy_alerts 가 본다.
여기서는 '올리기 전'에 우리가 만든 메타데이터·영상이 YouTube 한도와 채널 정책에 맞는지 본다.
"""

from __future__ import annotations

import re


TITLE_MAX = 100  # 자
DESC_MAX_BYTES = 5000
TAGS_MAX = 500  # 자 (쉼표 포함, 공백 있는 태그는 따옴표 2자 추가)
HASHTAGS_MAX = 60  # 이보다 많으면 YouTube 가 해시태그를 모두 무시 (제목 위에는 앞의 3개만 표시)
HASHTAGS_SHOWN = 3
SHORTS_MAX_S = 180  # 쇼츠 최대 길이 (2024-10 부터 3분)


def _tags_len(tags: list[str]) -> int:
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


def check(meta: dict, manifest: dict | None = None, plan=None) -> tuple[list[str], list[str]]:
    """(오류, 경고). meta = metadata.json, manifest = build.json."""
    errors, warnings = [], []
    for target, m in meta.items():
        if not target.startswith("youtube"):
            continue
        tag = f"[{target}]"
        title = m.get("title", "")
        if not title.strip():
            errors.append(f"{tag} 제목이 비어 있음")
        if len(title) > TITLE_MAX:
            errors.append(f"{tag} 제목 {len(title)}자 > {TITLE_MAX}자")
        if re.search(r"[<>]", title + m.get("description", "")):
            errors.append(f"{tag} 제목·설명에 '<' '>' 문자는 쓸 수 없음 (YouTube 가 거부)")
        desc = m.get("description", "")
        if len(desc.encode("utf-8")) > DESC_MAX_BYTES:
            errors.append(f"{tag} 설명 {len(desc.encode())}바이트 > {DESC_MAX_BYTES}")
        n_tags = _tags_len(m.get("tags", []))
        if n_tags > TAGS_MAX:
            errors.append(f"{tag} 태그 합계 {n_tags}자 > {TAGS_MAX}자")
        hashtags = re.findall(r"(?<!\S)#[^\s#]+", desc)
        if len(hashtags) > HASHTAGS_MAX:
            errors.append(f"{tag} 해시태그 {len(hashtags)}개 > {HASHTAGS_MAX}개 (넘으면 YouTube 가 전부 무시)")
        if m.get("made_for_kids"):
            warnings.append(f"{tag} '아동용'으로 설정됨 → 이 채널은 부모 대상 '아동용 아님' (docs/POLICY.md 2번)")
        if target.startswith("youtube_shorts") and manifest:
            video = "short2" if target.endswith("_2") else "short"
            dur = manifest.get("outputs", {}).get(video, {}).get("duration") or 0
            if dur > SHORTS_MAX_S:
                errors.append(f"{tag} 쇼츠 {dur:.0f}초 > {SHORTS_MAX_S}초")
    if plan is not None:
        # 정책상 '변경·합성 콘텐츠' 공개 대상: 실제 인물의 목소리를 만들어 그 사람이 말한 것처럼 들리게 할 때
        # (우리 AI 음성은 '아빠 내레이션'·'아이 속마음' 더빙 → 실제 목소리가 아니고 자막으로 더빙임이 드러남)
        # 아빠 대사를 아빠 본인 목소리 복제로 읽는 건 예외 ("Cloning one's own voice to create voice overs or dubs")
        from . import voice

        def own_clone(cap) -> bool:
            try:
                vc = voice.voice_config(cap.voice if isinstance(cap.voice, str) else cap.speaker)
            except (KeyError, ValueError):
                return False
            return vc.get("clone") == cap.speaker and voice.resolve_engine(vc) == "clone"

        real_voice = [cap for c in plan.clips for cap in c.captions
                      if cap.voice and cap.style == "talk" and cap.speaker in ("엄마", "아빠") and not own_clone(cap)]
        if real_voice:
            warnings.append("AI 음성으로 엄마·아빠가 실제로 말한 것처럼 만든 대사가 있음 → 업로드 시 '변경된 콘텐츠: 예' "
                            "또는 대사를 내레이션(sub)으로 바꾸기")
    return errors, warnings
