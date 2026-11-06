"""폴더 업로더: 완성본을 episodes/<회차>/<숏폼1|숏폼2|롱폼>/ 에 바로 올릴 수 있는 형태로 둔다 (deliver.py,
SSD 를 쓰면 이 폴더는 SSD 업로드/<회차 폴더>/<숏폼1 (11-04 수)> 로 가는 링크).

영상마다 폴더 하나에 올릴 곳(YouTube·인스타·틱톡)의 파일이 모두 들어간다. SSD 를 쓰면 실제 위치는 SSD 의 같은 경로.
숏폼1/ · 숏폼2/
    숏폼영상.mp4          YouTube 쇼츠 + 인스타 릴스 (같은 파일, BGM 포함)
    틱톡영상.mp4          BGM 없는 버전 (TikTok 이 외부 음원을 음소거해서 앱에서 사운드 추가)
    커버.jpg              쇼츠 썸네일 = 릴스 커버 = 틱톡 커버
    유튜브_제목·설명·태그·고정댓글·업로드방법.txt · 인스타_캡션·업로드방법.txt · 틱톡_캡션·업로드방법.txt
롱폼/
    롱폼영상.mp4, 썸네일.jpg (+ A/B 테스트용 썸네일_B·C.jpg), 유튜브_*.txt

업로드 후 `python3 -m babyreel published <id> <target> <url>` 로 기록하면
publish.json / data/publish_log.jsonl 에 남는다 (API 업로더와 같은 형식).
"""

from __future__ import annotations

from pathlib import Path

from .. import config, deliver
from ..fc import ROOT, rel
from ..plan import Plan
from .base import PublishResult, Publisher

INSTAGRAM_GUIDE = """[인스타그램 릴스 올리는 법]
1. '{video}' 를 휴대폰 사진 앱으로 (SSD 를 휴대폰에 연결 → '파일' 앱 → 이 폴더 → 길게 누르기 → 공유 → '비디오 저장',
   또는 Mac 에서 AirDrop). 커버.jpg 도 같이
2. Instagram → + → 릴스 → 방금 저장한 영상 선택 → 다음
   * '오디오(음악) 추가' 하지 않기 — BGM 이 이미 들어 있음 (추가하면 소리가 겹침)
   * '캡션(자동 자막)' 스티커/기능 쓰지 않기 — 영상 안에 자막이 이미 있음 (겹침)
   * 필터·텍스트 추가 없이 그대로
3. 다음 화면에서 '커버 수정' → '카메라 롤에서 추가' → '커버.jpg'
   (프로필 그리드에서는 가운데 3:4 만 보이는데, 커버는 그 안에 제목이 들어가게 만들어져 있음)
4. 캡션 칸에 '인스타_캡션.txt' 내용 전체 붙여넣기 (해시태그 5개 이하 그대로)
5. '피드에도 공유' 켜기 · 고급 설정 → 'AI 정보' 라벨 끔 (실제 촬영본)
6. 공유 후(선택): ./br published {episode} {target} <릴스 링크>
"""

TIKTOK_GUIDE = """[틱톡 올리는 법]
1. '틱톡영상.mp4' 를 휴대폰 사진 앱으로 (SSD 를 휴대폰에 연결 → '파일' 앱 → 이 폴더 → 길게 누르기 → 공유 → '비디오 저장',
   또는 Mac 에서 AirDrop). 커버.jpg 도 같이
2. TikTok → + → 업로드 → 영상 선택 → 다음
{music_step}   * '자막(자동 캡션)' 켜지 않기 — 영상 안에 자막이 이미 있음 (겹침)
3. '커버 선택' → 가능하면 이미지 추가로 '커버.jpg', 안 되면 제목이 잘 보이는 프레임 선택
4. 설명 칸에 '틱톡_캡션.txt' 내용 붙여넣기
5. 더 많은 옵션: 'AI 생성 콘텐츠' 끔 (실제 촬영본) · 댓글 허용 여부 선택
6. 게시 후(선택): ./br published {episode} {target} <틱톡 링크>
"""

# 공식: https://support.google.com/youtube/answer/13861714 (2026-10-21 확인) — 데스크톱 Studio, 고급 기능 필요,
# 비공개·아동용·쇼츠 불가, 최대 3장, 시청 시간이 가장 긴 썸네일이 자동으로 모두에게 (최대 2주)
AB_STEP = """   (선택) 썸네일 A/B 테스트 — 영상을 '공개'로 바꾼 뒤, 컴퓨터의 YouTube 스튜디오에서:
     콘텐츠 → 이 영상 → 썸네일 아래 'A/B 테스트' → '썸네일만' → 썸네일.jpg, 썸네일_B.jpg, 썸네일_C.jpg 순서로 올리고 완료
     (시청 시간이 가장 긴 썸네일이 2주 안에 자동으로 정해짐. '고급 기능'(전화 인증)이 켜져 있어야 버튼이 보임)
"""

YOUTUBE_GUIDE = """[유튜브 올리는 법 - API 미사용 시]
1. YouTube Studio → 만들기 → 동영상 업로드 → '{video}'
2. 제목: '유튜브_제목.txt' / 설명: '유튜브_설명.txt' / 태그: '유튜브_태그.txt'
3. 시청자층: '아니요, 아동용이 아닙니다' (부모 대상 육아 콘텐츠, docs/POLICY.md)
4. 속성 → AI 사용(변경된 콘텐츠): '아니요' (아빠 목소리 복제 내레이션도 공개 대상 아님, support.google.com/youtube/answer/14328491)
5. 공개 범위: '예약' → 첫 줄의 날짜·시각 (매일 같은 시각이 좋음: 구독자 알림·추천이 그 시간대에 맞춰짐)
   예약 전에 미리보기로 한 번 확인. 예약 영상은 공개 전까지 비공개 상태
   ⚠ 비공개·일부공개(링크 공유)로 남겨 두면 수익 창출 '유효 시청 시간'에 0으로 잡힙니다 (공개 롱폼만 인정)
   ⚠ 이미 올린 영상을 지우고 다시 올리면 그 영상의 시청 시간도 사라집니다 → 교체 대신 새 편으로
{extra}{related}{playlist}6. 공개된 직후: '유튜브_고정댓글.txt' 내용을 채널 계정으로 댓글 달고 ⋮ → '고정' (첫 1시간 댓글·답글이 추천에 도움.
   '고정'은 고급 기능이 열린 채널만 보임 → 안 보이면 댓글만 달아 두기)
   달린 댓글에는 첫날 답글 몇 개 (하트만 눌러도 좋음)
7. 업로드 후(선택): ./br published {episode} {target} <링크>  (공개되면 ./br schedule sync 가 자동으로 기록)
"""


RELATED_STEP = """   쇼츠 '관련 동영상': YouTube 앱 업로드 화면(또는 Studio > 동영상 > 세부정보) → 관련 동영상 → 같은 회차 롱폼 선택
   ('관련 동영상' 칸은 고급 기능이 열린 채널만 보임: Studio > 설정 > 채널 > 기능 사용 자격요건, docs/ALGORITHM.md 1-1.
    쇼츠 조회수는 '시청 시간 4,000시간'에 안 들어감 → 쇼츠에서 롱폼으로 넘어오게 하는 게 핵심.
    롱폼은 2부와 같은 날 예약 공개 → 롱폼이 공개된 뒤 1부·2부 모두에 추가 — 업로드일정.txt 에 할 일로 나옴)
"""


class ManualPublisher(Publisher):
    name = "manual"

    def publish(self, target: str, meta: dict) -> PublishResult:
        from . import base_target, video_of

        plan = Plan.load(self.episode_dir)
        kind, video = base_target(target), video_of(target)
        day = meta.get("upload_date", "")
        when = deliver.day_line(day) + "\n" if day else ""
        d = deliver.folder(plan, kind.split("_")[0], video)  # 숏폼1·숏폼2·롱폼: 플랫폼이 달라도 같은 폴더
        if kind in ("instagram_reels", "youtube_shorts"):
            # 쇼츠 = 릴스 (같은 short.mp4) → 한 파일. 혹시 다른 파일이면 따로 둔다
            video_name = "숏폼영상.mp4" if Path(meta["file"]).name in ("short.mp4", "short2.mp4") else (
                "릴스영상.mp4" if kind == "instagram_reels" else "쇼츠영상.mp4")
        else:
            video_name = "틱톡영상.mp4" if kind == "tiktok" else "롱폼영상.mp4"
        deliver.place(ROOT / meta["file"], d / video_name)
        if kind == "instagram_reels":
            if meta.get("cover"):
                deliver.place(ROOT / meta["cover"], d / "커버.jpg")
            (d / "인스타_캡션.txt").write_text(meta["caption"] + "\n", encoding="utf-8")
            (d / "인스타_업로드방법.txt").write_text(
                when + INSTAGRAM_GUIDE.format(episode=plan.id, target=target, video=video_name), encoding="utf-8")
        elif kind == "tiktok":
            if meta.get("cover"):
                deliver.place(ROOT / meta["cover"], d / "커버.jpg")
            (d / "틱톡_캡션.txt").write_text(meta["caption"] + "\n", encoding="utf-8")
            if meta.get("nobgm"):
                music_step = (
                    "   * 이 영상은 배경음악을 뺀 버전입니다 (외부 음악은 TikTok 이 자동으로 음소거함)\n"
                    "   * '사운드 추가' → 검색: " + (meta.get("music_search") or "귀여운 브금") + "\n"
                    "     → 마음에 드는 곡 선택 → '볼륨'에서 원본 소리 100%, 추가한 사운드 15~25%\n")
            else:
                music_step = "   * '사운드 추가' 하지 않기 — BGM 이미 들어 있음\n"
            (d / "틱톡_업로드방법.txt").write_text(
                when + TIKTOK_GUIDE.format(episode=plan.id, music_step=music_step, target=target), encoding="utf-8")
        else:
            short = kind == "youtube_shorts"
            extra = ""
            if meta.get("thumbnail") and short:
                deliver.place(ROOT / meta["thumbnail"], d / "커버.jpg")
                extra = "   쇼츠 썸네일: YouTube 앱 업로드 화면 → 썸네일 수정 → '커버.jpg'\n"
            if meta.get("thumbnail") and not short:
                deliver.place(ROOT / meta["thumbnail"], d / "썸네일.jpg")
                extra = f"   미리보기 이미지: '썸네일.jpg' · 최종 화면: 마지막 {config.load()['long']['outro_seconds']}초에 구독/영상 요소\n"
                # YouTube A/B 테스트용 다른 썸네일 2장 (thumbs.render_long: B = 좌우 바꾼 구도, C = 다른 순간)
                variants = [(ROOT / meta["thumbnail"]).with_name(n) for n in ("thumbnail_b.jpg", "thumbnail_c.jpg")]
                if all(v.exists() for v in variants):
                    deliver.place(variants[0], d / "썸네일_B.jpg")
                    deliver.place(variants[1], d / "썸네일_C.jpg")
                    extra += AB_STEP
            (d / "유튜브_제목.txt").write_text(meta["title"] + "\n", encoding="utf-8")
            (d / "유튜브_설명.txt").write_text(meta["description"] + "\n", encoding="utf-8")
            (d / "유튜브_태그.txt").write_text(", ".join(meta["tags"]) + "\n", encoding="utf-8")
            if meta.get("pinned_comment"):
                (d / "유튜브_고정댓글.txt").write_text(meta["pinned_comment"] + "\n", encoding="utf-8")
            playlist = f"   재생목록: '{meta['playlist']}' 에 추가 (없으면 새로 만들기, 공개)\n" if meta.get("playlist") else ""
            (d / "유튜브_업로드방법.txt").write_text(
                when + YOUTUBE_GUIDE.format(video=video_name, extra=extra, episode=plan.id, target=target,
                                            related=RELATED_STEP if short else "", playlist=playlist),
                encoding="utf-8")
        return PublishResult(target=target, status="packaged", detail=rel(d))
