"""plan.json → FilmCraft 프로젝트 → 업로드용 영상.

출력 (episodes/<id>/out/):
  short.mp4        1080x1920  YouTube Shorts + Instagram Reels 공용 파일
  short_nobgm.mp4  1080x1920  BGM 만 뺀 버전 (TikTok 등 외부 음원이 음소거되는 플랫폼용, catalog 의 blocked_on)
  long.mp4         1920x1080  YouTube 일반 영상
  thumbnail.jpg    1280x720   롱폼 썸네일
  cover.jpg        1080x1920  쇼츠 썸네일 (YouTube 쇼츠·릴스·틱톡 커버 공용)
  short2.mp4 · short2_nobgm.mp4 · cover2.jpg   쇼츠 2부 (plan.short2. 한 번에 쇼츠 2개 + 롱폼 1개 = 이틀치)
  preview_*.png    결과 확인용 컨택트시트
episodes/<id>/project.fcproj  FilmCraft 앱에서 열어 손으로 다듬을 수 있는 프로젝트
episodes/<id>/build.json      빌드 결과(길이, 구간, 챕터, 음악 크레딧) → metadata.py 가 사용

구성 (편집 문법은 docs/STYLE.md):
  1. 미디어 시퀀스: 영상 박스 크기. 클립 크롭/초점/블러배경/속도/정지/펀치줌/색보정
  2. 최종 시퀀스: frame 레이아웃이면 캔버스 + 미디어 시퀀스(중첩) + 상단 제목 + 워터마크
                full/롱폼이면 미디어 시퀀스가 곧 최종 시퀀스
  3. 그 위에 자막(sub/talk/big/chip) · 화자 아이콘 · 스티커 · 효과음 · BGM
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import config, faces, music, sfx, stabilize, stickers, voice
from .fc import FilmCraft, TICKS_PER_SECOND, rel, seconds_to_ticks as T
from .media import audio_level, clip_motion, loud_parts, probe, sheet
from .plan import SHORT_TARGETS, TARGET_NAMES, Caption, Clip, Music, Plan, validate

COVER = {"short": "cover.jpg", "short2": "cover2.jpg"}
QUIET_ORIGINAL_DB = -42.0  # 원본 소리가 이보다 작은 순간엔 BGM 을 '음악만' 크기로 (0.25초 단위)  # 쇼츠 썸네일 파일
OVERLAY_TRACKS = 16  # 자막·스티커가 겹칠 때 쓸 여유 트랙 수
SFX_TRACKS = ("A3", "A4", "A5")  # A6 는 오프닝·아웃트로 전용
MUSIC_TRACKS = ("A2", "A7")  # BGM. 장면마다 곡을 바꿀 때 번갈아 놓아 겹쳐(크로스페이드) 넘어간다
MUSIC_XFADE = 1.0  # 곡이 바뀔 때 겹치는 시간(초)


@dataclass
class Box:
    """영상이 보이는 영역(최종 시퀀스 좌표)."""

    x: float
    y: float
    w: float
    h: float

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class Segment:
    clip_index: int
    start: float  # 타임라인 초
    end: float
    chapter: str | None = None
    face: tuple[float, float, float, float] | None = None  # 보호한 얼굴(머리) 영역, 미디어 시퀀스 좌표 (big 자막 위치용)
    audio: int | None = None  # 원본 소리 클립 id (컷마다 오디오 크로스페이드)
    covers: list = field(default_factory=list)  # 가림 스티커가 지나가는 영역 [(x0, y0, x1, y1)] (미디어 좌표, 자막·스티커가 피함)


@dataclass
class Geometry:
    """자막 스타일별 크기·위치. 레이아웃/화면비마다 다르다."""

    box: Box
    text_bottom: float  # sub 자막 마지막 줄 baseline 이 넘지 말아야 할 y
    sizes: dict[str, float]
    chip_y: float
    title_y: float = 0  # full 레이아웃 훅 문구 첫 줄 baseline
    safe: tuple[float, float, float, float] | None = None  # 쇼츠 세이프존 (x0, y0, x1, y1). 롱폼은 None


@dataclass
class Tracks:
    """겹치지 않게 트랙을 배정하는 간단한 스케줄러."""

    names: list[str]
    busy: dict[str, list[tuple[float, float]]] = field(default_factory=dict)

    def take(self, t0: float, t1: float) -> str:
        for name in self.names:
            spans = self.busy.setdefault(name, [])
            if all(t1 <= a or t0 >= b for a, b in spans):
                spans.append((t0, t1))
                return name
        raise RuntimeError(f"겹치는 요소가 너무 많습니다 ({t0:.2f}~{t1:.2f}s)")


def _ticks_to_s(ticks: int) -> float:
    return ticks / TICKS_PER_SECOND


def voice_name(cap: Caption, cfg: dict) -> str:
    name = cap.voice if isinstance(cap.voice, str) else (cap.speaker or "narrator")
    return name if name in cfg.get("voices", {}) else "narrator"


def voice_names(plan: Plan) -> set[str]:
    cfg = config.load()
    return {voice_name(cap, cfg) for c in plan.clips for cap in c.captions if cap.voice}


class Builder:
    def __init__(self, fc: FilmCraft, plan: Plan, episode_dir: Path, name: str = "short"):
        self.fc = fc
        self.plan = plan
        self.name = name  # 쇼츠 시퀀스 이름: short(1부) | short2(2부)
        self.dir = episode_dir
        self.cfg = config.load()
        self.style = self.cfg["style"]
        self._items: dict[str, int] = {}
        self.hdr = False
        self.voice_spans: list[tuple[float, float]] = []  # AI 음성이 나오는 구간 (더킹용)
        self.music_used: list[dict] = []  # 이 영상에 실제로 깐 곡 (크레딧용)
        self.segments: list[Segment] = []  # 마지막으로 만든 미디어 시퀀스의 클립 구간
        self.frame_size: tuple[int, int] | None = None  # 내보내기 해상도 덮어쓰기 (render.py: 1:1·4:5 등)
        self._last_main: int | None = None  # 마지막으로 놓은 클립의 영상 clip id (전환 효과용)

    # --- 공통 헬퍼 ---------------------------------------------------------------------

    def item(self, path: Path) -> int:
        key = str(path)
        if key not in self._items:
            res = self.fc.run("file.import", paths=[key])
            if res.get("errors"):
                raise RuntimeError(f"import 실패 {path}: {res['errors']}")
            self._items[key] = res["items"][0]
        return self._items[key]

    def find_clip(self, clip_id: int) -> dict:
        seq = self.fc.sequence()
        for kind in ("video", "audio"):
            for track in seq[kind]:
                for it in track["items"]:
                    if it.get("clip") == clip_id:
                        return it
        raise KeyError(clip_id)

    def mask_time(self, clip_id: int, t_src: float) -> int:
        """마스크 키프레임 시각(ticks). masks.set 의 time 은 원본 기준이 아니라 **타임라인** 기준이다
        (2026-11-03 실험: 원본 초를 넘기면 클립 밖이라 모두 첫 프레임 한 키로 뭉침). 클립 밖은 FilmCraft 가 끝으로 붙인다."""
        it = self.find_clip(clip_id)
        return int(it["start"] + (T(t_src) - it["sourceIn"]) / (it.get("speed") or 1.0))

    def new_sequence(self, name: str, w: int, h: int, fps: float, video: int, audio: int) -> int:
        seq = self.fc.run("file.newSequence", name=name, width=w, height=h, fps=fps, video=video, audio=audio)
        if self.hdr:
            self.fc.run("sequence.settings", autoToneMap=True)
        return seq["sequence"]

    def text(self, text: str, t0: float, dur: float, x: float, y: float, size: float, track: str,
             color: str = "#ffffff", stroke: str | None = "#000000", stroke_ratio: float = 0.13,
             align: str = "center", background: bool = False, fun: bool = False, shadow: bool = False,
             max_w: float | None = None) -> int:
        """폰트를 항상 명시한다 (지정 안 하면 한글이 NO GLYPH 로 나옴).
        fun=True 면 귀여운 서체(style.fun_font, 기본 Jua)를 쓴다: 펀치라인·칩·썸네일용."""
        font = self.style.get("fun_font", "Jua") if fun else self.style["font"]
        font_style = self.style.get("fun_font_style", "Regular") if fun else self.style["font_style"]
        clip = self.fc.run(
            "graphics.newText", text=text, position=[x, y], size=size, seconds=dur,
            track=int(track[1:]) - 1, time=T(t0), font=font, fontStyle=font_style,
        )["clip"]
        props = {"font": font, "font_style": font_style, "align": align, "fill_color": color, "leading": 0}
        if shadow:
            props |= {"shadow": True}
        if stroke:
            props |= {"stroke": True, "stroke_color": stroke, "stroke_width": max(3, round(size * stroke_ratio))}
        if background:
            props |= {"background": True}
        self.fc.run("graphics.set", clip=clip, layer=0, props=props)
        if max_w:
            # 화면 폭을 넘으면 글자 크기를 줄여 잘림을 막는다
            q = self.text_bounds(clip)
            width = max(p[0] for p in q) - min(p[0] for p in q)
            if width > max_w:
                self.fc.run("graphics.set", clip=clip, layer=0, props={"size": size * max_w / width * 0.98})
        return clip

    def text_bounds(self, clip: int) -> list[list[float]]:
        return self.fc.run("graphics.list", clip=clip)["layers"][0]["quad"]

    def image(self, path: Path, t0: float, dur: float, cx: float, cy: float, px: float, track: str,
              pop: bool = True) -> int:
        """PNG 를 (cx, cy) 중심에 가로 px 크기로 얹는다. pop=True 면 톡 튀어나오는 애니메이션."""
        clip = self.fc.run("timeline.place", item=self.item(path), track=track, seconds=t0,
                           duration=T(dur), audio=False)["clips"][0]
        scale = px / 256 * 100  # 스티커 PNG 는 256px
        self.fc.run("effects.setParam", clip=clip, effect="motion", param="position", value=[cx, cy])
        if pop and dur > 0.4:
            for dt, k in ((0, 0.7), (0.12, 1.04), (0.22, 1.0)):  # 살짝 나타남 (튀는 팝 X)
                self.fc.run("effects.setParam", clip=clip, effect="motion", param="scale",
                            value=scale * k, time=T(t0 + dt), keyframe=True)
        else:
            self.fc.run("effects.setParam", clip=clip, effect="motion", param="scale", value=scale)
        return clip

    def sound(self, path: Path, t0: float, gain_db: float, tracks: Tracks) -> None:
        dur = probe(path)["duration_s"]
        track = tracks.take(t0, t0 + dur)
        placed = self.fc.run("timeline.place", item=self.item(path), track=track, seconds=t0, duration=T(dur))
        if gain_db:
            self.fc.run("clip.audioGain", clips=placed["clips"], mode="set", db=gain_db)

    # --- 1. 미디어 시퀀스 -------------------------------------------------------------------

    def media_sequence(self, target: str, w: int, h: int, fps: float, extra_video: int,
                       offset: float = 0.0, name: str | None = None, order: list[int] | None = None,
                       gap_after: int | None = None, gap: float = 0.0) -> tuple[int, list[Segment]]:
        """order: 놓을 plan.clips 인덱스 순서 (기본 = target 클립 순서).
        gap_after: order 의 몇 번째 뒤에 gap 초를 비울지 (롱폼: 미리보기 뒤 오프닝 카드 자리)."""
        clips = self.plan.clips_for(target)
        if order is None:
            order = [i for i, c in enumerate(self.plan.clips) if c in clips]
        name = name or target
        # 맨 위 트랙 하나는 가림 스티커(clips[].covers) 전용: 영상 바로 위, 자막·스티커와 섞이지 않게
        self.new_sequence(f"{name}_media" if extra_video == 0 else name, w, h, fps, video=3 + extra_video, audio=7)
        self.cover_track = f"V{3 + extra_video}"
        cursor = offset
        segments = []
        mains: list[int | None] = []
        for n, idx in enumerate(order):
            seg = self.place_clip(self.plan.clips[idx], cursor, target, w, h)
            seg.clip_index = idx
            segments.append(seg)
            mains.append(self._last_main)
            cursor = seg.end + (gap if gap_after is not None and n == gap_after else 0.0)
        self.segments = segments  # BGM 볼륨(원본 소리 구간) 계산용
        self.audio_crossfades(segments)
        self.video_transitions(mains)
        return self.fc.inspect()["project"]["activeSequence"], segments

    def audio_crossfades(self, segments: list[Segment]) -> None:
        """컷마다 원본 소리를 짧게(약 0.1초) 교차·페이드 → 컷에서 '딱' 튀는 소리 방지 (STYLE 10번)."""
        frames = int(self.style.get("audio_crossfade_frames", 3))
        if frames <= 0:
            return
        for seg in segments:
            if seg.audio is None:
                continue
            try:
                self.fc.run("sequence.applyAudioTransition", clip=seg.audio, effect="constant_power",
                            frames=frames, edge="out")
            except Exception:  # noqa: BLE001  (다음 클립이 없거나 너무 짧으면 건너뜀)
                pass

    def video_transitions(self, mains: list[int | None]) -> None:
        """컷 사이 화면 전환 (style.video_transition: cross_dissolve·whip·cross_zoom·flash …). 기본은 없음(하드 컷).
        마지막 클립에는 넣지 않는다 (다음 클립이 없으면 검은 화면으로 사라짐)."""
        effect = self.style.get("video_transition", "")
        frames = int(self.style.get("video_transition_frames", 0))
        if not effect or frames <= 0:
            return
        for clip in mains[:-1]:
            if clip is None:
                continue
            try:
                self.fc.run("sequence.applyVideoTransition", clip=clip, effect=effect, frames=frames, edge="out")
            except Exception:  # noqa: BLE001  (클립이 너무 짧으면 건너뜀)
                pass

    def place_clip(self, clip: Clip, t0: float, target: str, W: int, H: int) -> Segment:
        src = config.resolve(clip.src)
        info = probe(src)
        item = self.item(src)
        assert clip.end is not None
        src_len = clip.end - clip.start
        length = clip.duration
        with_audio = info["has_audio"] and not clip.mute and not clip.hold and not info["still"]
        if info["still"]:
            src_len = length = clip.end - clip.start

        placed = self.fc.run(
            "timeline.place", item=item, track="V2", audioTrack="A1", seconds=t0,
            sourceIn=T(clip.start), duration=T(length if clip.hold else src_len), audio=with_audio,
        )["clips"]
        main, aud = placed[0], (placed[1] if with_audio and len(placed) > 1 else None)
        self._last_main = main
        if clip.hold:
            self.hold(main, t0)
        elif clip.speed != 1 and not info["still"]:
            ids = [main] + ([aud] if aud else [])
            self.fc.run("clip.speedDuration", clips=ids, speed=clip.speed * 100, ripple=False)

        placed_info = self.find_clip(main)
        start, end = _ticks_to_s(placed_info["start"]), _ticks_to_s(placed_info["end"])

        portrait_in_wide = info["height"] >= info["width"] and W > H * 1.3  # 16:9 롱폼만. 쇼츠 영상 박스는 항상 꽉 채움
        fit = clip.fit if clip.fit != "auto" else ("blur" if portrait_in_wide else "fill")
        if fit == "blur":
            bg = self.fc.run("timeline.place", item=item, track="V1", seconds=t0, sourceIn=T(clip.start),
                             duration=T(length if clip.hold else src_len), audio=False)["clips"][0]
            if clip.hold:
                self.hold(bg, t0)
            elif clip.speed != 1:
                self.fc.run("clip.speedDuration", clips=[bg], speed=clip.speed * 100, ripple=False)
            self.fc.run("clip.fillFrame", clips=[bg])
            self.fc.run("effects.apply", clips=[bg], effect="gaussian_blur")
            self.fc.run("effects.setParam", clip=bg, effect="gaussian_blur", param="blurriness",
                        value=self.style["blur_background"])
            self.fc.run("effects.setParam", clip=bg, effect="gaussian_blur", param="repeat_edge", value=True)
            self.fc.run("effects.apply", clips=[bg], effect="brightness_contrast")
            self.fc.run("effects.setParam", clip=bg, effect="brightness_contrast", param="brightness", value=-15)
            self.fc.run("clip.fitToFrame", clips=[main])
            m = clip_motion(self.fc, main)
            min_scale = m["scale"]
            base_scale = min_scale * clip.zoom
            iw, ih = info["width"], info["height"]
            if iw * min_scale / 100 > W + 1 or ih * min_scale / 100 > H + 1:
                iw, ih = ih, iw  # 회전 메타데이터: fitToFrame 결과로 실제 방향을 판단
            # 확대하면 화면을 넘는 쪽(보통 세로)이 생긴다 → focus/focus_y 로 보여 줄 위치를 고른다
            fw, fh = iw * base_scale / 100, ih * base_scale / 100
            px = W / 2 if fw <= W else min(fw / 2, max(W - fw / 2, W / 2 + (0.5 - clip.focus) * fw))
            py = H / 2 if fh <= H else min(fh / 2, max(H - fh / 2, H / 2 + (0.5 - clip.focus_y) * fh))
            cover = False
        else:
            self.fc.run("clip.fillFrame", clips=[main])
            s0 = clip_motion(self.fc, main)["scale"]
            iw, ih = info["width"], info["height"]
            if iw * s0 / 100 < W - 1 or ih * s0 / 100 < H - 1:
                iw, ih = ih, iw  # 회전 메타데이터: fillFrame 결과로 실제 방향을 판단
            min_scale = s0
            base_scale = s0 * clip.zoom
            fw, fh = iw * base_scale / 100, ih * base_scale / 100
            px = min(fw / 2, max(W - fw / 2, W / 2 + (0.5 - clip.focus) * fw))
            py = min(fh / 2, max(H - fh / 2, H / 2 + (0.5 - clip.focus_y) * fh))
            cover = True

        # 얼굴 보호: 계획한 구도에서 보이는 얼굴이 잘리면 위치를 옮기거나 확대를 줄인다
        # (보정이 가려 둔 글자·타인 얼굴을 드러내면 확대는 유지하고 위치만, 그래도 드러나면 보정하지 않음)
        guard, hidden = None, []
        found: dict = {"faces": [], "texts": [], "frames": []}
        if faces.enabled() and (clip.face_guard or clip.blur_faces or self.style.get("auto_exposure", True)):
            n = 1 if (info["still"] or clip.hold) else max(6, min(16, int(src_len * 2)))
            found = faces.scan(src, clip.start, clip.end if not info["still"] else clip.start, samples=n)
        if clip.blur_faces:
            # 흐리게 할 얼굴은 '지킬 얼굴'에서 뺀다 (구도·자막 위치가 그 얼굴을 기준으로 잡히지 않게)
            found_all, found = found, faces.without(found, [(h[0], h[1]) for h in clip.blur_faces])
        if clip.face_guard and faces.enabled():
            before = (base_scale, px, py)
            guard, hidden, (base_scale, px, py), note = faces.guarded(
                found, iw, ih, W, H, base_scale, min_scale, px, py, cover, clip.crop, clip.zoom)
            if before != (base_scale, px, py) or note:
                print(f"    얼굴 보호: {Path(clip.src).name} 확대 {before[0]:.0f}→{base_scale:.0f}%"
                      + (f" · {note}" if note else ""))
        self.fc.run("effects.setParam", clip=main, effect="motion", param="scale", value=base_scale)
        self.fc.run("effects.setParam", clip=main, effect="motion", param="position", value=[px, py])

        if stabilize.wanted(clip, info):
            stabilize.apply(self.fc, main)  # 손떨림만 살짝 (최대 110%, 잘라내기·가림 클립은 제외)
        if clip.crop:
            # 원본 기준으로 잘라낸다 (블러 배경은 형체를 알아볼 수 없으므로 그대로 둔다)
            self.fc.run("effects.apply", clips=[main], effect="crop")
            for name, v in zip(("left", "top", "right", "bottom"), clip.crop):
                if v:
                    self.fc.run("effects.setParam", clip=main, effect="crop", param=name, value=v)
        if clip.blur_faces:
            self.blur_faces(main, clip, found_all, iw, ih)  # 흐리게 할 얼굴은 빼기 전 결과에서 따라간다
        if clip.blur_areas:
            self.blur_areas(main, clip, iw, ih)  # 창밖 풍경 등 위치가 드러나는 곳
        # 노출 맞춤: 클립마다 밝기가 달라 튀지 않게 (어두운 밤 영상은 밝게, 너무 밝으면 살짝 내림)
        ev = 0.0
        luma = faces.mean_luma(found) if self.style.get("auto_exposure", True) else None
        if luma:
            import math
            ev = math.log2(self.style.get("exposure_target", 0.45) / max(0.03, luma))
            ev = max(-0.6, min(1.2, ev)) if abs(ev) >= 0.15 else 0.0
        self.grade(main, extra_exposure=ev)
        if aud:
            # 원본 소리 크기 맞춤: 들리는 부분의 평균을 목표치로 (조용한 폰 녹음은 키우고 큰 울음은 줄임)
            gain = clip.gain_db
            if self.style.get("auto_dialogue_level", True):
                parts = loud_parts(src, clip.start, clip.end, -42.0)
                if parts:
                    lv = sum(audio_level(src, a, b) * (b - a) for a, b in parts) / sum(b - a for a, b in parts)
                    gain += max(-6.0, min(9.0, self.style.get("dialogue_target_db", -24.0) - lv))
            if abs(gain) >= 0.5:
                self.fc.run("clip.audioGain", clips=[aud], mode="set", db=round(gain, 1))
            for m0, m1 in clip.mute_ranges:
                # 이 구간만 소리 끔 (이름·타인 대화). 앞뒤 0.05초 페이드로 '뚝' 끊기지 않게
                k = clip.speed if not clip.hold else 1.0
                a, b = start + (m0 - clip.start) / k, start + (m1 - clip.start) / k
                for at, lv in ((a - 0.05, 0.0), (a, -80.0), (b, -80.0), (b + 0.05, 0.0)):
                    if start <= at <= end:
                        self.fc.run("effects.setParam", clip=aud, effect="volume", param="level", value=lv,
                                    time=T(at), keyframe=True)

        keys: list[float] = []
        if info["still"] and not clip.punch:
            # 켄번즈: 사진이 천천히 확대(짝수 클립)·축소(홀수 클립). 5% 이내 (과하지 않게)
            ks = (1.0, 1.05) if int(t0 * 10) % 2 == 0 else (1.05, 1.0)
            for t, k in ((start, ks[0]), (end, ks[1])):
                self.zoom_key(main, t, k, base_scale, px, py, guard, iw, ih, W, H, cover, hidden, clip.crop)
                keys.append(t)
        elif (not clip.punch and not clip.hold and not clip.covers and end - start >= 2.5
              and self.style.get("push_in", 0.035) > 0):  # 가림 스티커가 있으면 푸시인 없음 (확대되며 가림이 어긋나지 않게)
            # 정적인 영상에 아주 느린 푸시인 (멈춘 느낌 방지, 얼굴 중심)
            for t, k in ((start, 1.0), (end, 1.0 + self.style.get("push_in", 0.035))):
                self.zoom_key(main, t, k, base_scale, px, py, guard, iw, ih, W, H, cover, hidden, clip.crop)
                keys.append(t)

        punch_max = self.style.get("punch_max", 1.25)
        for p in clip.punch:
            a = start + p.at
            k_max = min(p.scale, punch_max)
            ramp = min(0.2, p.dur / 3)  # 6프레임 남짓 부드럽게 (튀는 줌 대신)
            for t, k in ((a, 1.0), (a + ramp, k_max), (a + p.dur - ramp, k_max), (a + p.dur, 1.0)):
                self.zoom_key(main, t, k, base_scale, px, py, guard, iw, ih, W, H, cover, hidden, clip.crop)
                keys.append(t)
        if keys and not clip.hold:
            self.ease(main, keys, start, clip.start, clip.speed if not info["still"] else 1.0, bool(guard))

        face = None
        if guard:
            fb = faces._screen(guard, iw * base_scale / 100, ih * base_scale / 100, px, py)
            face = (max(0.0, fb[0]), max(0.0, fb[1]), min(float(W), fb[2]), min(float(H), fb[3]))
        cover_rects = []
        if clip.covers:
            cover_rects = self.place_covers(clip, start, end, iw * base_scale / 100, ih * base_scale / 100, px, py,
                                            clip.speed if not (clip.hold or info["still"]) else 1.0, W, H, face)
        return Segment(clip_index=-1, start=start, end=end, chapter=clip.chapter, face=face, audio=aud,
                       covers=cover_rects)

    def place_covers(self, clip: Clip, start: float, end: float, fw: float, fh: float, px: float, py: float,
                     k: float, W: float = 1e9, H: float = 1e9,
                     face: tuple[float, float, float, float] | None = None) -> list[tuple[float, float, float, float]]:
        """가림 스티커: 원본 화면 좌표(0~1)를 이 클립의 화면 위치로 바꿔 고정하고, 점이 여럿이면 시간에 따라 따라간다.
        (얼굴·자막을 피해 옮기는 일반 스티커와 달리 그 자리를 덮는 게 목적. 2026-10-22 목욕 장면)"""
        track = getattr(self, "cover_track", "V3")
        rects = []
        for cv in clip.covers:
            pts = sorted(cv.points)
            if not pts:
                continue

            size = cv.size * fw

            def screen(x: float, y: float) -> tuple[float, float]:
                # 기획한 자리 그대로 (가리는 게 목적이라 자동으로 옮기지 않는다). 화면 끝에 걸치면 잘린 채로 둔다
                return px - fw / 2 + x * fw, py - fh / 2 + y * fh

            def when(t_src: float) -> float:
                return min(end, max(start, start + (t_src - clip.start) / k))

            for _, x, y in pts:  # 지나가는 영역 (자막·스티커가 피할 곳)
                cx, cy = screen(x, y)
                rects.append((cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2))
            x0, y0 = screen(*pts[0][1:])
            img = self.image(stickers.png_for(cv.emoji), start, end - start, x0, y0, size, track, pop=False)
            if len(pts) > 1:
                for t_src, x, y in pts:
                    self.fc.run("effects.setParam", clip=img, effect="motion", param="position",
                                value=list(screen(x, y)), time=T(when(t_src)), keyframe=True)
        if not rects:
            return []
        return [(min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))]

    def ease(self, clip: int, times: list[float], tl_start: float, src_start: float, speed: float,
             position: bool) -> None:
        """줌 키프레임을 부드럽게 (가속·감속). 실패해도 편집에는 지장 없음."""
        for t in times:
            media = T(src_start + (t - tl_start) * speed)
            for param in ("scale",) + (("position",) if position else ()):
                try:
                    self.fc.run("effects.setInterpolation", clip=clip, effect="motion", param=param, mediaTime=media,
                                interpolation="autoBezier")
                except Exception:  # noqa: BLE001
                    pass

    def blur_faces(self, clip_id: int, clip: Clip, found: dict, iw: float, ih: float) -> None:
        """부모 외 인물 얼굴을 흐리게 (Vision 으로 따라가는 타원 마스크). 못 찾으면 지정 위치 고정 블러."""
        k = 0.5523  # 타원 베지어 핸들 비율
        self.fc.run("effects.apply", clips=[clip_id], effect="gaussian_blur")
        idx = len(self.find_clip(clip_id).get("effects", [])) - 1
        idx = next((i for i, e in enumerate(self.find_clip(clip_id)["effects"]) if e["effect"] == "gaussian_blur"),
                   idx)
        self.fc.run("effects.setParam", clip=clip_id, effect=idx, param="blurriness", value=90)

        def path(b):
            h = faces.head(b)
            cx, cy = (h[0] + h[2]) / 2 * iw, (h[1] + h[3]) / 2 * ih
            rx, ry = (h[2] - h[0]) / 2 * iw * 1.35, (h[3] - h[1]) / 2 * ih * 1.35
            return {"closed": True, "vertices": [
                {"p": [cx, cy - ry], "in": [-k * rx, 0.0], "out": [k * rx, 0.0]},
                {"p": [cx + rx, cy], "in": [0.0, -k * ry], "out": [0.0, k * ry]},
                {"p": [cx, cy + ry], "in": [k * rx, 0.0], "out": [-k * rx, 0.0]},
                {"p": [cx - rx, cy], "in": [0.0, k * ry], "out": [0.0, -k * ry]}]}

        for m, hint in enumerate(clip.blur_faces):
            track = faces.track_near(found, (hint[0], hint[1]))
            if not track:  # 못 찾음 → 지정 위치에 넉넉한 고정 블러
                w, h = 0.14, 0.14 * iw / ih
                track = [(clip.start, (hint[0] - w / 2, hint[1] - h / 2, hint[0] + w / 2, hint[1] + h / 2))]
            first = path(track[0][1])
            c0 = first["vertices"][1]["p"][0] - first["vertices"][3]["p"][0]
            self.fc.run("masks.add", clip=clip_id, effect=idx, shape="ellipse",
                        center=[(first["vertices"][1]["p"][0] + first["vertices"][3]["p"][0]) / 2,
                                (first["vertices"][0]["p"][1] + first["vertices"][2]["p"][1]) / 2],
                        size=[c0, first["vertices"][2]["p"][1] - first["vertices"][0]["p"][1]])
            self.fc.run("masks.set", clip=clip_id, effect=idx, mask=m, feather=40, expansion=10)
            if len(track) > 1:
                self.fc.run("effects.toggleAnimation", clip=clip_id, effect=idx, param="path", mask=m)
                # 시작 전·끝 뒤도 덮이도록 첫·끝 위치를 클립 양 끝에 둔다
                points = [(clip.start, track[0][1])] + track + [(clip.end, track[-1][1])]
                for t_src, b in points:
                    self.fc.run("masks.set", clip=clip_id, effect=idx, mask=m, path=path(b),
                                time=self.mask_time(clip_id, t_src))
        print(f"    얼굴 블러: {Path(clip.src).name} {len(clip.blur_faces)}명")

    def blur_areas(self, clip_id: int, clip: Clip, iw: float, ih: float) -> None:
        """위치가 드러나는 곳(창밖 풍경 등, clips[].blur_areas)을 알아볼 수 없게: 강한 가우시안 블러 + 사각 마스크.
        points 가 여럿이면 마스크가 그 사이를 따라간다 (2026-11-03 사용자 요청)."""
        self.fc.run("effects.apply", clips=[clip_id], effect="gaussian_blur")
        # 얼굴 블러(blur_faces)와 따로: 방금 넣은 = 마지막 가우시안 블러
        idx = [i for i, e in enumerate(self.find_clip(clip_id)["effects"]) if e["effect"] == "gaussian_blur"][-1]
        self.fc.run("effects.setParam", clip=clip_id, effect=idx, param="blurriness",
                    value=self.style.get("area_blur", 150))
        self.fc.run("effects.setParam", clip=clip_id, effect=idx, param="repeat_edge", value=True)

        def path(b):
            x0, y0, x1, y1 = b[0] * iw, b[1] * ih, b[2] * iw, b[3] * ih
            return {"closed": True, "vertices": [{"p": [x, y], "in": [0.0, 0.0], "out": [0.0, 0.0]}
                                                 for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]}

        for m, area in enumerate(clip.blur_areas):
            keys = area.keys()
            b = keys[0][1]
            self.fc.run("masks.add", clip=clip_id, effect=idx, shape="rectangle",
                        center=[(b[0] + b[2]) / 2 * iw, (b[1] + b[3]) / 2 * ih], size=[(b[2] - b[0]) * iw, (b[3] - b[1]) * ih])
            self.fc.run("masks.set", clip=clip_id, effect=idx, mask=m, feather=24, expansion=0)
            if len(keys) > 1:
                self.fc.run("effects.toggleAnimation", clip=clip_id, effect=idx, param="path", mask=m)
                # 시작 전·끝 뒤도 덮이도록 첫·끝 영역을 클립 양 끝에 둔다
                points = [(clip.start, keys[0][1])] + keys + [(clip.end, keys[-1][1])]
                for t_src, box in points:
                    self.fc.run("masks.set", clip=clip_id, effect=idx, mask=m, path=path(box),
                                time=self.mask_time(clip_id, t_src))
        print(f"    위치 블러: {Path(clip.src).name} {len(clip.blur_areas)}곳 ({', '.join(a.what for a in clip.blur_areas)})")

    def zoom_key(self, clip: int, t: float, k: float, scale: float, px: float, py: float, guard, iw: float,
                 ih: float, W: int, H: int, cover: bool, hidden: list | None = None, crop: list | None = None) -> None:
        """t 시점에 k 배 확대 키프레임. 얼굴이 있으면 얼굴 중심 기준으로 커지고 얼굴이 잘리지 않게 위치도 함께
        (그 위치가 가려 둔 글자·얼굴을 드러내면 화면 가운데 기준 확대로)."""
        s, x, y = scale * k, px, py
        if guard and k != 1:
            cand = faces.zoom_about(guard, iw, ih, W, H, scale, px, py, k, cover)
            if not faces.reveals(hidden or [], iw, ih, W, H, (scale, px, py), cand, crop):
                s, x, y = cand
        self.fc.run("effects.setParam", clip=clip, effect="motion", param="scale", value=s, time=T(t), keyframe=True)
        if guard:
            self.fc.run("effects.setParam", clip=clip, effect="motion", param="position", value=[x, y],
                        time=T(t), keyframe=True)

    def hold(self, clip_id: int, t0: float) -> None:
        """클립 첫 프레임으로 끝까지 정지 (Add Frame Hold 는 플레이헤드부터 적용된다)."""
        self.fc.run("playhead.set", seconds=t0)
        self.fc.run("clip.frameHold", clips=[clip_id])

    def grade(self, clip_id: int, extra_exposure: float = 0.0) -> None:
        params = {
            "temperature": self.style.get("grade_temperature", 0),
            "vibrance": self.style.get("grade_vibrance", 0),
            "exposure": round(self.style.get("grade_exposure", 0.0) + extra_exposure, 2),
            # 프리셋 색보정 (외부 프로필의 무드 프리셋). 0 이면 넣지 않는다
            "tint": self.style.get("grade_tint", 0),
            "contrast": self.style.get("grade_contrast", 0),
            "highlights": self.style.get("grade_highlights", 0),
            "shadows": self.style.get("grade_shadows", 0),
            "faded_film": self.style.get("grade_fade", 0),
        }
        sat = self.style.get("grade_saturation", 0)  # Lumetri 채도는 100 이 원래 값
        if not any(params.values()) and not sat:
            return
        self.fc.run("effects.apply", clips=[clip_id], effect="lumetri")
        for k, v in params.items():
            if v:
                self.fc.run("effects.setParam", clip=clip_id, effect="lumetri", param=k, value=v)
        if sat:
            self.fc.run("effects.setParam", clip=clip_id, effect="lumetri", param="saturation", value=100 + sat)

    # --- 2. 최종 시퀀스 ---------------------------------------------------------------------

    def build_short(self) -> dict:
        s = self.cfg["short"]
        W, H, fps = s["width"], s["height"], s["fps"]
        if self.plan.layout == "frame":
            f = self.cfg["frame"]
            box = Box(0, f["media_top"], W, f["media_height"])
            media_seq, segments = self.media_sequence("short", W, int(box.h), fps, extra_video=0, name=self.name)
            total = segments[-1].end
            seq = self.new_sequence(self.name, W, H, fps, video=8 + OVERLAY_TRACKS, audio=7)
            if f["canvas"] == "blur":
                bg = self.fc.run("timeline.place", item=media_seq, track="V1", seconds=0, duration=T(total),
                                 audio=False)["clips"][0]
                self.fc.run("clip.fillFrame", clips=[bg])
                self.fc.run("effects.apply", clips=[bg], effect="gaussian_blur")
                self.fc.run("effects.setParam", clip=bg, effect="gaussian_blur", param="blurriness", value=120)
                self.fc.run("effects.setParam", clip=bg, effect="gaussian_blur", param="repeat_edge", value=True)
            else:
                matte = self.fc.run("file.newColorMatte", color=f["canvas"], seconds=total)["item"]
                self.fc.run("timeline.place", item=matte, track="V1", seconds=0, duration=T(total))
            mclip = self.fc.run("timeline.place", item=media_seq, track="V2", audioTrack="A1", seconds=0,
                                duration=T(total))["clips"][0]
            self.fc.run("effects.setParam", clip=mclip, effect="motion", param="scale", value=100)
            self.fc.run("effects.setParam", clip=mclip, effect="motion", param="position", value=[box.cx, box.cy])
            # 상단 고정 제목 (레퍼런스의 핵심 훅: 영상 내내 떠 있음)
            size = f["title_size"]
            for i, line in enumerate(self.plan.title_lines):
                color = f["title_colors"][min(i, len(f["title_colors"]) - 1)]
                self.text(line, 0, total, W / 2, f["title_top"] + size * (0.85 + i * 1.22), size,
                          track=f"V{3 + i}", color=color, stroke=None)
            # 브랜드: 제목 아래 "육아일지 #12 · 남매 대전", 영상 아래 "첫째 4살 · 둘째 8개월" + 핸들
            # 마지막 몇 초는 태그 자리에 구독 부탁 한 줄 (아주 살짝, [cta]. 영상 칸은 안 가림 → 루프 방해 없음)
            cta = self.cfg.get("cta", {})
            cta_s = min(cta.get("short_end_seconds", 2.5), total / 3) if cta.get("short_end") else 0.0
            if self.episode_tag():
                self.text(self.episode_tag(), 0, total - cta_s, W / 2, box.y - 22, 32, track="V5",
                          color=f.get("tag_color", "#999999"), stroke=None)
            if cta_s:
                self.text(cta["short_end"], total - cta_s, cta_s, W / 2, box.y - 24, 36, track="V5",
                          color=self.style.get("accent_color", "#ff7a3d"), stroke=None, fun=True)
            under = [x for x in (self.ages_line(), f.get("watermark", "")) if x]
            for i, line in enumerate(under):
                self.text(line, 0, total, W / 2, box.bottom + 62 + i * 48, 34 if i == 0 else 30,
                          track=f"V{6 + i}", color=f["watermark_color"], stroke=None)
            geo = Geometry(
                box=box, text_bottom=min(box.bottom - 64, H - s["safe_bottom"] - 30),
                sizes={"sub": 48, "talk": 60, "big": 120, "chip": 62}, chip_y=box.y + 110,
                safe=(s["safe_side"], s["safe_top"], W - s["safe_side"], H - s["safe_bottom"]),
            )
            first_overlay = 8
        else:
            box = Box(0, 0, W, H)
            seq, segments = self.media_sequence("short", W, H, fps, extra_video=4 + OVERLAY_TRACKS, name=self.name)
            total = segments[-1].end
            geo = Geometry(
                box=box, text_bottom=H - s["safe_bottom"] - 30,
                sizes={"sub": 54, "talk": 66, "big": 130, "chip": 66}, chip_y=s["safe_top"] + 110,
                title_y=s["safe_top"] + 90,
                safe=(s["safe_side"], s["safe_top"], W - s["safe_side"], H - s["safe_bottom"]),
            )
            hook = min(s["hook_seconds"], total)
            for i, line in enumerate(self.plan.title_lines):
                color = self.style["hook_color"] if i == 0 else "#ffffff"
                self.text(line, 0, hook, W / 2, geo.title_y + i * 100, 84, track=f"V{3 + i}", color=color)
            tag = " · ".join(x for x in (self.episode_tag(), self.ages_line()) if x)
            if tag:
                self.text(tag, 0, hook, W / 2, geo.title_y + len(self.plan.title_lines) * 100, 36, track="V5")
            first_overlay = 6
        self.overlays(segments, geo, first_overlay)
        self.add_music(total)
        return {"sequence": seq, "segments": segments, "duration": total, "geometry": geo}

    def teaser_clips(self) -> list[Clip]:
        """롱폼 맨 앞 '미리보기'(콜드 오픈): 이번 편 펀치라인 순간 2~3개를 짧게 (방송 예고처럼).
        길이를 억지로 늘리는 게 아니라, 첫 10초에 '이 영상 재밌다'를 보여 줘 끝까지 보게 하는 장치."""
        lc = self.cfg["long"]
        if not lc.get("teaser", True):
            return []
        secs, picks, used = float(lc.get("teaser_clip_seconds", 1.8)), [], set()
        flagged = [c for c in self.plan.clips if c.highlight]
        pool = flagged + [c for c in self.plan.clips if {"short", "short2"} & set(c.targets) and c not in flagged]
        for c in pool:
            if c.hold or c.src in used:
                continue
            if c.covers or (len(c.crop) == 4 and c.crop[3] >= 15):
                continue  # 목욕처럼 구도를 좁혀 가린 장면은 미리보기에 쓰지 않는다 (구도가 달라져 가림이 어긋날 수 있음)
            caps = [cap for cap in c.captions if cap.style == "big"] or ([] if not c.highlight else c.captions[:1])
            if not caps:
                continue
            cap = caps[0]
            k = c.speed or 1.0
            t_src = c.start + cap.at * k
            a = max(c.start, t_src - 0.4)
            b = min((c.end or a + secs), a + secs * k)
            if b - a < 0.8:
                continue
            from dataclasses import replace

            picks.append(replace(c, start=a, end=b, targets=["long"], chapter=None, punch=[], sfx=[], stickers=[], music=None,
                                 captions=[Caption(at=0.1, text=cap.text, style="big", dur=(b - a) / k - 0.2)]))
            used.add(c.src)
            if len(picks) >= int(lc.get("teaser_max", 3)):
                break
        return picks if len(picks) >= 2 else []

    def build_long(self) -> dict:
        lc = self.cfg["long"]
        W, H, fps = lc["width"], lc["height"], lc["fps"]
        intro = float(lc.get("intro_seconds", 0) or 0)
        from dataclasses import replace as _replace

        self.plan = _replace(self.plan, clips=list(self.plan.clips))  # 미리보기 클립을 붙여도 원본 plan 은 그대로
        teaser = self.teaser_clips()
        main = [i for i, c in enumerate(self.plan.clips) if "long" in c.targets]
        order = list(range(len(self.plan.clips), len(self.plan.clips) + len(teaser))) + main
        self.plan.clips.extend(teaser)  # 인덱스를 지키려고 맨 뒤에 붙이고 order 로 맨 앞에 놓는다
        seq, segments = self.media_sequence(
            "long", W, H, fps, extra_video=4 + OVERLAY_TRACKS, offset=0.0 if teaser else intro, order=order,
            gap_after=len(teaser) - 1 if teaser else None, gap=intro if teaser else 0.0)
        total = segments[-1].end
        start_main = segments[len(teaser)].start if teaser else intro
        if intro:
            self.intro(W, H, intro, t0=start_main - intro)
        geo = Geometry(box=Box(0, 0, W, H), text_bottom=H - 70,
                       sizes={"sub": 50, "talk": 58, "big": 120, "chip": 58}, chip_y=110)
        # 챕터 시작마다 좌상단 라벨
        for seg in segments:
            if seg.chapter:
                self.text(seg.chapter, seg.start, min(3.5, seg.end - seg.start), 70, 120, 58, track="V3",
                          color=self.style["hook_color"], align="left")
        handle = self.cfg["channel"].get("handle", "")
        if handle:
            self.text(handle, start_main, total - start_main, W - 60, 80, 30, track="V4", color="#ffffff",
                      align="right", stroke_ratio=0.1)
        if lc["outro_seconds"]:
            total = self.outro(total, W, H, lc["outro_seconds"])
        self.overlays(segments, geo, first_overlay=6)
        self.add_music(total, use_cuts=False)  # music.cuts 는 쇼츠 타임라인 기준
        return {"sequence": seq, "segments": segments, "duration": total, "geometry": geo}

    def drift(self, clip: int, t0: float, secs: float, k: float = 1.06) -> None:
        """정적인 카드·글씨가 '멈춘 화면'처럼 보이지 않게 천천히 커지게 한다."""
        for t, v in ((t0, 100.0), (t0 + secs, 100.0 * k)):
            self.fc.run("effects.setParam", clip=clip, effect="motion", param="scale", value=v, time=T(t), keyframe=True)

    def pops(self, emojis: list[str], spots: list[tuple[float, float]], t0: float, until: float, px: float,
             gap: float = 0.6) -> None:
        """스티커를 차례로 톡톡 띄운다 (효과음 포함)."""
        audio = Tracks(["A6"])
        for i, (e, (x, y)) in enumerate(zip(emojis, spots)):
            t = t0 + i * gap
            if t >= until - 0.3:
                break
            self.image(stickers.png_for(e), t, until - t, x, y, px, track=f"V{8 + i}")
            self.sound(sfx.path_for("pop"), t, -8, audio)

    def moving_backdrop(self, clip: Clip, t0: float, secs: float, track: str) -> None:
        """오프닝·아웃트로 배경: 실제 영상을 흐리게 깔아 화면이 멈춰 보이지 않게 한다."""
        src = config.resolve(clip.src)
        info = probe(src)
        avail = info["duration_s"] - clip.start
        start = clip.start if avail >= secs else max(0.0, info["duration_s"] - secs)
        if info["still"] or info["duration_s"] < secs:
            matte = self.fc.run("file.newColorMatte", color=self.cfg["thumbnail"].get("bg", "#fff1e6"),
                                seconds=secs)["item"]
            self.fc.run("timeline.place", item=matte, track=track, seconds=t0, duration=T(secs))
            return
        bg = self.fc.run("timeline.place", item=self.item(src), track=track, seconds=t0, sourceIn=T(start),
                         duration=T(secs), audio=False)["clips"][0]
        self.fc.run("clip.fillFrame", clips=[bg])
        self.fc.run("effects.apply", clips=[bg], effect="gaussian_blur")
        self.fc.run("effects.setParam", clip=bg, effect="gaussian_blur", param="blurriness", value=45)
        self.fc.run("effects.setParam", clip=bg, effect="gaussian_blur", param="repeat_edge", value=True)
        self.fc.run("effects.apply", clips=[bg], effect="brightness_contrast")
        self.fc.run("effects.setParam", clip=bg, effect="brightness_contrast", param="brightness", value=-28)
        self.grade(bg)
        m = clip_motion(self.fc, bg)
        for t, k in ((t0, 1.0), (t0 + secs, 1.08)):  # 천천히 다가가는 줌
            self.fc.run("effects.setParam", clip=bg, effect="motion", param="scale", value=m["scale"] * k,
                        time=T(t), keyframe=True)

    def intro(self, W: int, H: int, secs: float, t0: float = 0.0) -> None:
        """롱폼 고정 오프닝 (매 편 같은 모양): 썸네일 장면을 흐리게 깔고 '<채널 이름> / 육아일지 #N · 시리즈' + 제목."""
        ch = self.cfg["channel"]
        accent = self.style.get("accent_color", "#ff7a3d")
        th = self.plan.thumbnail
        base = self.plan.clips[th.clip]
        self.moving_backdrop(Clip(src=base.src, start=th.at), t0, secs, "V2")
        a = self.text(ch["name"], t0, secs, W / 2, H * 0.38, 124, track="V3", color="#ffffff", stroke=accent,
                      stroke_ratio=0.14, fun=True, shadow=True)
        b = self.text(self.episode_tag(), t0 + 0.15, secs - 0.15, W / 2, H * 0.38 + 112, 54, track="V4",
                      color="#fff36b", stroke="#5c3d2e", stroke_ratio=0.12, fun=True)
        title = self.plan.youtube_long_title.split("|")[0].strip() or self.plan.youtube_title
        c = self.text(title, t0 + 0.4, secs - 0.4, W / 2, H * 0.38 + 212, 46, track="V5", color="#ffffff",
                      stroke="#000000", stroke_ratio=0.1)
        for clip in (a, b, c):
            self.drift(clip, t0, secs, 1.05)
        self.pops(["🔍", "✨"], [(W * 0.2, H * 0.3), (W * 0.82, H * 0.28)], t0 + 0.3, t0 + secs, 130)

    def outro(self, t0: float, W: int, H: int, secs: float) -> float:
        """롱폼 고정 아웃트로 (최종 화면 요소 자리): 마지막 장면을 흐리게 이어서 깔고 글씨·스티커."""
        ch = self.cfg["channel"]
        accent = self.style.get("accent_color", "#ff7a3d")
        last = self.plan.clips_for("long")[-1]
        self.moving_backdrop(Clip(src=last.src, start=last.start), t0, secs, "V2")
        a = self.text(f"{ch.get('tagline', ch['name'])}\n다음 편도 함께 봐요", t0, secs, W / 2, H * 0.22, 74,
                      track="V3", color="#ffffff", stroke=accent, stroke_ratio=0.12, fun=True, shadow=True)
        b = self.text(f"{ch['name']}  {ch.get('handle', '')}", t0, secs, W / 2, H * 0.22 + 200, 46,
                      track="V4", color="#fff36b", stroke="#5c3d2e", stroke_ratio=0.1, fun=True)
        clips = [a, b]
        y = H * 0.22 + 290
        if self.plan.next_hook:
            clips.append(self.text(f"다음 기록: {self.plan.next_hook}", t0 + 0.8, secs - 0.8, W / 2, y,
                                   40, track="V5", color="#ffffff", stroke="#000000", stroke_ratio=0.1))
            y += 70
        # 구독·좋아요(·하이프) 부탁 한 줄, 아주 살짝 ([cta])
        cta = self.cfg.get("cta", {})
        ask = cta.get("hype_outro") if cta.get("hype") else cta.get("long_outro")
        if ask:
            clips.append(self.text(ask, t0 + 1.4, secs - 1.4, W / 2, y, 38, track="V6",
                                   color="#fff36b", stroke="#5c3d2e", stroke_ratio=0.1, fun=True))
        for clip in clips:
            self.drift(clip, t0, secs, 1.06)
        self.pops(["💕", "👀", "✨"], [(W * 0.18, H * 0.2), (W * 0.84, H * 0.24), (W * 0.2, H * 0.62)],
                  t0 + 0.5, t0 + secs, 140, gap=0.9)
        return t0 + secs

    def episode_tag(self) -> str:
        parts = []
        if self.plan.episode_no:
            # 쇼츠를 1·2부로 나눈 회차는 "육아일지 #3-1", "#3-2" (롱폼은 "#3")
            part = f"-{self.plan.part_no}" if self.plan.part_no else ""
            parts.append(f"육아일지 #{self.plan.episode_no}{part}")
        label = config.series_label(self.plan.series)
        if label:
            parts.append(label)
        return " · ".join(parts)

    def ages_line(self) -> str:
        """이 영상에 담긴 촬영 기간의 나이 ('첫째 D+57~63'). 끝은 클립 중 마지막 촬영일(없으면 plan.date)."""
        on = date.fromisoformat(self.plan.date)
        days = [d for c in self.plan.clips_for("long" if self.name == "long" else "short")
                if (d := config.shot_date(c.src)) and d <= on]
        if not days:
            return config.ages(on, self.plan.cast or None)
        return config.ages(max(days), self.plan.cast or None, since=min(days))

    # --- 3. 자막 · 스티커 · 효과음 · 음악 ----------------------------------------------------------

    def overlays(self, segments: list[Segment], geo: Geometry, first_overlay: int) -> None:
        video = Tracks([f"V{i}" for i in range(first_overlay, first_overlay + OVERLAY_TRACKS)])
        audio = Tracks(list(SFX_TRACKS))
        self.voice_spans = []
        self.text_rects: list[tuple[float, float, tuple[float, float, float, float]]] = []
        for item in self.layout_captions(segments, geo):
            self.caption(item, geo, video, audio)
        for seg in segments:
            clip = self.plan.clips[seg.clip_index]
            for st in clip.stickers:
                t = seg.start + st.at
                dur = min(st.dur, seg.end - t)
                px = geo.box.w * st.size
                busy = [r for a, b, r in self.text_rects if a < t + dur and t < b]  # 같은 시간 자막
                busy += [(geo.box.x + c[0], geo.box.y + c[1], geo.box.x + c[2], geo.box.y + c[3]) for c in seg.covers]
                sx, sy = sticker_spot(st.x, st.y, px, geo.box, seg.face, busy)
                if geo.safe:  # 쇼츠: 오른쪽 버튼·하단 캡션 영역을 피한다
                    x0, y0, x1, y1 = geo.safe
                    sx = min(max(sx, x0 + px / 2), x1 - px / 2)
                    sy = min(max(sy, y0 + px / 2), y1 - px / 2)
                self.image(stickers.png_for(st.emoji), t, dur, sx, sy, px, video.take(t, t + dur))
                if st.sfx:
                    self.sound(sfx.path_for(st.sfx), t, -6, audio)
            for s in clip.sfx:
                self.sound(sfx.path_for(s.name), seg.start + s.at, -6 + s.gain_db, audio)

    def layout_captions(self, segments: list[Segment], geo: Geometry) -> list[dict]:
        """자막을 그리기 전에 최종 시간·위치를 정해 겹침을 없앤다.

        1. AI 음성이 있으면 음성 길이만큼 자막을 늘린다
        2. 같은 자리(같은 style)의 자막이 시간상 겹치면 앞 자막을 다음 자막 직전에 끝낸다
        3. talk(대사)는 sub(내레이션) 바로 위에 뜨므로, 같은 시간에 보이는 sub 가 여러 줄이면 그만큼 올린다
        """
        items: list[dict] = []
        for seg in segments:
            for cap in self.plan.clips[seg.clip_index].captions:
                t = seg.start + cap.at
                dur = min(cap.dur, seg.end - t)
                if cap.voice:
                    wav, _ = voice.speak(cap.text, self.voice_name(cap))
                    s0, s1 = voice.speech_bounds(wav)
                    vdur = s1 - s0
                    dur = max(dur, vdur + 0.2)
                    if t + vdur > seg.end + 0.1:
                        print(f"    ! 음성 '{cap.text[:12]}…' 이 클립 끝을 {t + vdur - seg.end:.1f}s 넘깁니다 "
                              "(at 을 앞당기거나 문장을 줄이세요)")
                items.append({"cap": cap, "t": t, "dur": dur, "lift": 0.0, "face": seg.face, "covers": seg.covers})
        for style in ("sub", "talk", "big", "chip"):
            group = sorted((i for i in items if i["cap"].style == style), key=lambda i: i["t"])
            for a, b in zip(group, group[1:]):
                if a["t"] + a["dur"] > b["t"] - 0.05:
                    a["dur"] = max(0.4, b["t"] - 0.05 - a["t"])
        sub_line = geo.sizes["sub"] * 1.25
        for it in (i for i in items if i["cap"].style == "talk"):
            t0, t1 = it["t"], it["t"] + it["dur"]
            lines = [s["cap"].text.count("\n") + 1 for s in items
                     if s["cap"].style == "sub" and s["t"] < t1 and s["t"] + s["dur"] > t0]
            if lines:
                it["lift"] = (max(lines) - 1) * sub_line
            it["sub_busy"] = bool(lines)  # 같은 시간에 아래 내레이션 줄이 쓰이는지 (가림 스티커 피할 때)
        return items

    def voice_name(self, cap: Caption) -> str:
        return voice_name(cap, self.cfg)

    def caption(self, item: dict, geo: Geometry, video: Tracks, audio: Tracks) -> None:
        cap, t, dur = item["cap"], item["t"], item["dur"]
        if cap.voice:
            self.speak(cap, t, audio)
        size = geo.sizes[cap.style]
        lines = cap.text.count("\n") + 1
        fit_w = (geo.safe[2] - geo.safe[0]) if geo.safe else geo.box.w - 90
        line_h = size * 1.25
        box = geo.box
        made: list[int] = []
        # 아래 자막 줄이 얼굴(눈·입)을 덮으면 영상 칸 위쪽으로 올린다 (2026-10-19: 자막이 아기 눈을 가린 사례)
        face = item.get("face")
        up_y = geo.chip_y + geo.sizes["chip"] * 1.5 + size  # 칩(시간 라벨) 바로 아래
        # 가림 스티커(목욕 오리 등) 위로도 자막을 얹지 않는다: 얼굴과 같은 '피할 띠'로 본다
        avoid = ([face] if face else []) + list(item.get("covers") or [])
        band = lambda y0, y1: any(y0 < box.y + a[3] and box.y + a[1] < y1 for a in avoid)  # noqa: E731
        if cap.style == "sub":
            y = geo.text_bottom - (lines - 1) * line_h
            if band(y - size, y + size * 0.3) and not band(up_y - size, up_y + (lines - 1) * line_h + size * 0.3):
                y = up_y
            made.append(self.text(cap.text, t, dur, box.cx, y, size, video.take(t, t + dur), max_w=fit_w,
                                  color=self.style.get("caption_color", "#ffffff"),
                                  stroke=self.style.get("caption_stroke", "#000000") or None))
        elif cap.style == "talk":
            y = geo.text_bottom - geo.sizes["sub"] * 2.1 - (lines - 1) * line_h - item["lift"]
            if band(y - size, y + size * 0.3):
                alt = up_y + geo.sizes["sub"] * 1.6
                low = geo.text_bottom - (lines - 1) * line_h  # 아래 자막 줄 (그 시간에 내레이션이 없을 때만)
                if not band(alt - size, alt + (lines - 1) * line_h + size * 0.3):
                    y = alt
                elif not item.get("sub_busy") and not band(low - size, low + size * 0.3):
                    y = low
            clip = self.text(cap.text, t, dur, box.cx, y, size, video.take(t, t + dur), max_w=fit_w - size * 1.6)
            made.append(clip)
            speaker = self.cfg.get("speakers", {}).get(cap.speaker or "")
            if speaker:
                # 텍스트 왼쪽에 화자 아이콘 (메신저 대화 스타일). 텍스트를 오른쪽으로 비켜 준다.
                icon = size * 1.5
                quad = self.text_bounds(clip)
                left, top = quad[0][0], quad[0][1]
                shift = (icon + 14) / 2
                self.fc.run("graphics.set", clip=clip, layer=0, props={"position": [box.cx + shift, y]})
                self.image(stickers.png_for(speaker["emoji"]), t, dur, left + shift - icon / 2 - 14,
                           top + size * 0.62, icon, video.take(t, t + dur), pop=False)
        elif cap.style == "big":
            # 기본은 화면 가운데. 얼굴과 겹치면 얼굴 위·아래 중 넓은 쪽으로 (펀치라인이 아기 얼굴을 가리지 않게)
            cy = box.cy
            block = lines * size * 1.15
            face = item.get("face")
            if face:
                f0, f1 = box.y + face[1], box.y + face[3]
                if cy - block / 2 < f1 and f0 < cy + block / 2:
                    top_lo, top_hi = geo.chip_y + size * 0.6, f0 - 16
                    bot_lo, bot_hi = f1 + 16, geo.text_bottom - geo.sizes["sub"] * 2.3
                    up, down = top_hi - top_lo, bot_hi - bot_lo
                    if max(up, down) >= block * 0.8:
                        cy = (top_lo + top_hi) / 2 if up > down else (bot_lo + bot_hi) / 2
                    else:  # 얼굴이 화면을 거의 채우면 이마 쪽에 (입·턱을 가리면 표정이 죽는다)
                        cy = top_lo + block / 2
            y = cy - (lines - 1) * size * 0.6 + size * 0.35
            made.append(self.text(cap.text, t, dur, box.cx, y, size, video.take(t, t + dur),
                                  color=self.style["hook_color"], stroke=self.style.get("hook_stroke", "#000000") or None,
                                  stroke_ratio=0.16, fun=True, max_w=fit_w))
        elif cap.style == "chip":
            made.append(self.text(cap.text, t, dur, box.cx, geo.chip_y, size, video.take(t, t + dur),
                                  color=self.style.get("chip_color", "#ffffff"),
                                  stroke=self.style.get("chip_stroke", "#000000") or None,
                                  stroke_ratio=0.15, fun=True, max_w=fit_w))
        for c in made:
            self.fade(c, t, dur)
            q = self.text_bounds(c)
            xs, ys = [v[0] for v in q], [v[1] for v in q]
            self.text_rects.append((t, t + dur, (min(xs), min(ys), max(xs), max(ys))))
        if cap.sfx:
            # 음성이 있으면 효과음을 살짝 먼저 내서 첫 음절이 묻히지 않게 한다
            self.sound(sfx.path_for(cap.sfx), max(0.0, t - 0.15) if cap.voice else t, -6, audio)

    def fade(self, clip: int, t: float, dur: float) -> None:
        """자막 부드럽게 나타나고 사라지기 (0.1초). 툭 튀어나오는 느낌 대신 깔끔하게 (STYLE 10번)."""
        f = self.style.get("caption_fade", 0.1)
        if f <= 0 or dur < 4 * f:
            return
        for at, v in ((t, 0), (t + f, 100), (t + dur - f * 0.8, 100), (t + dur, 0)):
            self.fc.run("effects.setParam", clip=clip, effect="opacity", param="opacity", value=v, time=T(at),
                        keyframe=True)

    def speak(self, cap: Caption, t: float, audio: Tracks) -> float:
        wav, vc = voice.speak(cap.text, self.voice_name(cap))
        s0, s1 = voice.speech_bounds(wav)  # 앞뒤 무음을 잘라 자막과 동시에 말이 시작되게
        vdur = s1 - s0
        track = audio.take(t, t + vdur)
        clips = self.fc.run("timeline.place", item=self.item(wav), track=track, seconds=t, sourceIn=T(s0),
                            duration=T(vdur))["clips"]
        if vc.get("pitch"):
            self.fc.run("effects.apply", clips=clips, effect="pitch_shifter")
            self.fc.run("effects.setParam", clip=clips[0], effect="pitch_shifter", param="semitones", value=vc["pitch"])
        if vc.get("gain_db"):
            self.fc.run("clip.audioGain", clips=clips, mode="set", db=vc["gain_db"])
        self.voice_spans.append((t, t + vdur))
        return vdur

    def duck_spans(self) -> list[tuple[float, float]]:
        merged: list[list[float]] = []
        for a, b in sorted(self.voice_spans):
            if merged and a - merged[-1][1] < 0.5:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return [(a, b) for a, b in merged]

    def duck_original(self) -> None:
        """AI 음성 동안 원본 소리(A1)를 줄인다."""
        spans = self.duck_spans()
        if not spans:
            return
        db = self.cfg["voices"].get("duck_original_db", -9.0)
        for track in self.fc.sequence()["audio"]:
            if track["name"] not in ("Audio 1", "A1"):
                continue
            for it in track["items"]:
                c0, c1 = _ticks_to_s(it["start"]), _ticks_to_s(it["end"])
                for a, b in spans:
                    for at, level in ((a - 0.15, 0.0), (a, db), (b, db), (b + 0.25, 0.0)):
                        if c0 <= at <= c1:
                            self.fc.run("effects.setParam", clip=it["clip"], effect="volume", param="level",
                                        value=level, time=T(at), keyframe=True)

    def original_spans(self) -> list[tuple[float, float]]:
        """원본 소리가 살아 있는 구간 (음소거·정지 화면·사진·소리 없는 클립 제외). BGM 볼륨 기준."""
        spans = []
        for seg in self.segments:
            c = self.plan.clips[seg.clip_index]
            info = probe(config.resolve(c.src))
            if info["has_audio"] and not (c.mute or c.hold or info["still"]):
                # 원본 소리가 실제로 들리는 부분에서만 BGM 을 낮춘다 (조용한 순간엔 음악만 크기 → 소리 끊김 방지)
                k = c.speed or 1.0
                for a, b in loud_parts(config.resolve(c.src), c.start, c.end or c.start, QUIET_ORIGINAL_DB):
                    if any(m0 <= a and b <= m1 for m0, m1 in c.mute_ranges):
                        continue  # 소리를 끈 구간은 음악만 크기로
                    spans.append((seg.start + (a - c.start) / k, seg.start + (b - c.start) / k))
        return spans

    def music_sections(self, total: float) -> list[tuple[float, float, Music | None]]:
        """[(시작, 끝, 곡)]: plan.music 으로 시작해서, clips[].music 이 있는 클립부터 그 곡으로 바꾼다 ("off" = 음악 없음).
        같은 곡이 이어지면 한 구간. 오프닝 카드·아웃트로는 앞뒤 구간에 붙는다."""
        base = self.plan.music
        marks: list[tuple[float, Music | None]] = [(0.0, base)]
        key = lambda m: (m.track or m.file) if m else None  # noqa: E731
        for seg in self.segments:
            c = self.plan.clips[seg.clip_index]
            if c.music is None:
                continue
            m = None if c.music == "off" else Music(track=c.music, gain_db=base.gain_db if base else None)
            if key(m) != key(marks[-1][1]):
                marks.append((seg.start, m))
        out = []
        for i, (t0, m) in enumerate(marks):
            t1 = marks[i + 1][0] if i + 1 < len(marks) else total
            if t1 - t0 > 0.05:
                out.append((t0, t1, m))
        return out

    def add_music(self, total: float, use_cuts: bool = True) -> None:
        """BGM: 원본 소리가 있는 구간은 낮게(music_gain_db), 음소거 구간은 크게(music_only_gain_db),
        AI 음성 동안 더 낮게, music.cuts 는 무음, 끝 1.2초 페이드아웃.
        (음소거 장면에서 BGM 이 낮게만 깔리면 소리가 비고 끊겨 들린다: 2026-10-17 쇼츠 2부 QA)
        장면마다 곡을 바꾸면(clips[].music) A2·A7 에 번갈아 놓고 MUSIC_XFADE 초 겹쳐 넘어간다.
        곡마다 녹음 크기가 달라 music.trim_db 로 기준 음량에 맞춘다 (곡이 바뀔 때 소리가 튀지 않게)."""
        self.duck_original()
        self.music_used = []
        if not self.plan.music:
            return
        a = self.cfg["audio"]
        base_under = self.plan.music.gain_db if self.plan.music.gain_db is not None else a["music_gain_db"]
        orig = self.original_spans()
        voice = self.duck_spans()
        cuts = [tuple(c) for c in self.plan.music.cuts] if use_cuts else []
        duck = self.cfg.get("voices", {}).get("duck_music_db", -12.0)
        sections = self.music_sections(total)
        multi = len(sections) > 1
        alone = max(base_under, a["music_only_gain_db"])

        def level(x: float) -> float:
            if any(t0 <= x < t1 for t0, t1 in cuts):
                return -60.0
            v = (base_under - alone) if any(s0 <= x < s1 for s0, s1 in orig) else 0.0
            if any(v0 <= x < v1 for v0, v1 in voice):
                v += duck
            return v

        edges = sorted({e for span in [*cuts, *orig, *voice] for e in span if 0 < e < total - 1.2})
        for i, (t0, t1, m) in enumerate(sections):
            if m is None:
                continue  # "off": 원본 소리만
            info = music.resolve(m)
            path = info["path"]
            if not any(u["id"] == info["id"] for u in self.music_used):
                self.music_used.append({"id": info["id"], "title": info["title"], "attribution": info["attribution"]})
            first, last = i == 0, i == len(sections) - 1
            s0 = 0.0 if first else max(0.0, t0 - MUSIC_XFADE / 2)
            e1 = total if last else min(total, t1 + MUSIC_XFADE / 2)
            track = MUSIC_TRACKS[i % 2]
            # 곡 안의 쉬는 구간(0.3초 이상)은 건너뛰고, 짧으면 처음부터 반복 (music.playable)
            t, clips = s0, []
            for a_in, a_out in music.playable(path, m.start, e1 - s0):
                clips += self.fc.run("timeline.place", item=self.item(path), track=track, seconds=t,
                                     sourceIn=T(a_in), duration=T(a_out - a_in))["clips"]
                t += a_out - a_in
            trim = music.trim_db(path) if multi or self.style.get("music_normalize", True) else 0.0
            self.fc.run("clip.audioGain", clips=clips, mode="set", db=alone + trim)

            def env(x: float, i=i, s0=s0, e1=e1, first=first, last=last) -> float:
                # 크로스페이드는 진폭 기준(가운데에서 각 곡 -6dB): dB 를 직선으로 줄이면 가운데가 -30dB 까지 꺼져서
                # 원본 소리가 없는 장면에서는 '소리 끊김'이 된다 (2026-10-23 번외편 QA)
                import math

                v = level(x)
                r = 1.0
                if not first and x < s0 + MUSIC_XFADE:  # 앞 곡과 겹치며 커진다
                    r = min(r, max(0.0, (x - s0) / MUSIC_XFADE))
                if not last and x > e1 - MUSIC_XFADE:  # 다음 곡과 겹치며 작아진다
                    r = min(r, max(0.0, (e1 - x) / MUSIC_XFADE))
                return v if r >= 1.0 else max(-60.0, v + 20 * math.log10(max(r, 0.001)))

            pts = [s0, e1] + [e for e in edges if s0 < e < e1]
            events: list[tuple[float, float]] = []
            for e in sorted(set(pts)):  # 경계마다 0.15초에 걸쳐 바뀐다
                events += [(max(s0, e - 0.15), env(e - 0.16)), (e, env(e + 0.001))]
            if not first:
                events += [(s0, -60.0), (min(e1, s0 + MUSIC_XFADE), env(s0 + MUSIC_XFADE))]
            if not last:
                events += [(max(s0, e1 - MUSIC_XFADE), env(e1 - MUSIC_XFADE)), (e1, -60.0)]
            else:
                events += [(max(s0, total - 1.2), env(total - 1.21)), (total, -60.0)]
            events = sorted(dict(sorted(events)).items())
            for clip in clips:
                ci = self.find_clip(clip)
                c0, c1 = _ticks_to_s(ci["start"]), _ticks_to_s(ci["end"])
                inside = [(at, lv) for at, lv in events if c0 <= at <= c1]
                # 조각 시작점에도 그 시점의 레벨을 둔다 (앞 조각의 마지막 키프레임이 이어지지 않으므로)
                if not inside or inside[0][0] > c0 + 0.01:
                    inside.insert(0, (c0, env(c0)))
                for at, lv in inside:
                    self.fc.run("effects.setParam", clip=clip, effect="volume", param="level",
                                value=lv, time=T(at), keyframe=True)

    # --- 썸네일 · 내보내기 -------------------------------------------------------------------

    def thumbnail(self, out: Path) -> None:
        """롱폼 썸네일 1280x720: thumbnail.jpg(A) + thumbnail_b/c.jpg (YouTube 테스트 및 비교). babyreel/thumbs.py"""
        from . import thumbs

        thumbs.render_long(self.plan, out.parent)

    def cover(self, out: Path) -> None:
        """쇼츠 썸네일 1080x1920: YouTube 쇼츠 썸네일·인스타 릴스 커버·틱톡 커버 공용. babyreel/thumbs.py"""
        from . import thumbs

        thumbs.render(self.plan, "cover", out)

    def master(self, seq: int, ceiling: float = -3.0) -> None:
        """믹스 마스터에 하드 리미터를 걸고 목표 라우드니스 근처까지 부스트한다.

        내보내기의 라우드니스 정규화는 게인만 조절해서, 아기 비명·효과음처럼 순간 피크가 큰 믹스는
        트루피크 한도에 막혀 목표(-14 LUFS)까지 못 올라간다. 리미터로 피크를 눌러 두고 올린다.
        """
        import tempfile

        from .qa import loudness

        target = self.cfg["audio"]["target_lufs"]
        self.fc.run("sequence.open", item=seq)
        strips = self.fc.run("mixer.inspect")["strips"]
        mix = next(s for s in strips if s["ref"] == "Mix")
        slot = next((i["slot"] for i in mix["inserts"] if i["effect"] == "hard_limiter"), None)
        if slot is None:
            slot = self.fc.run("mixer.addInsert", strip="Mix", effect="hard_limiter")["slot"]
        boost = 0.0
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(4):
                self.fc.run("mixer.setInsert", strip="Mix", slot=slot, params={"max": ceiling, "boost": boost})
                wav = Path(tmp) / f"m{boost:.1f}.wav"
                self.fc.run("file.exportMedia", path=str(wav), format="wav", sequence=seq, wait=True)
                lufs = loudness(wav)["integrated_lufs"]
                if lufs < -60 or abs(lufs - target) < 0.7 or i == 3:
                    break  # 마지막으로 측정한 부스트를 그대로 둔다 (설정값과 측정값이 어긋나지 않게)
                boost = max(0.0, min(24.0, boost + (target - lufs) * 1.1))
        print(f"    마스터: 리미터 부스트 {boost:.1f}dB → {lufs} LUFS")

    def export_without_music(self, seq: int, out: Path, preset: str, video_kbps: int, audio_kbps: int,
                             fps: float) -> None:
        """BGM 트랙(A2·A7)만 음소거하고 내보낸 뒤 되돌린다. 마스터링(라우드니스)은 다시 맞춘다."""
        self.fc.run("sequence.open", item=seq)
        for strip in MUSIC_TRACKS:
            self.fc.run("mixer.setStrip", strip=strip, muted=True)
        try:
            self.export(seq, out, preset, video_kbps, audio_kbps, fps)
        finally:
            for strip in MUSIC_TRACKS:
                self.fc.run("mixer.setStrip", strip=strip, muted=False)

    def export(self, seq: int, out: Path, preset: str, video_kbps: int, audio_kbps: int, fps: float) -> None:
        """내보낸 뒤 피크를 재서 -1dBFS 를 넘으면 리미터 한도를 낮춰 한 번 더 (클리핑 안전장치)."""
        # 아기 비명처럼 아주 짧은 큰 소리는 AAC 인코딩에서 리미터 한도를 넘기도 한다 → 한도를 단계적으로 더 낮춘다
        for ceiling in (-3.0, -4.5, -6.5, -8.5):
            self._export_once(seq, out, preset, video_kbps, audio_kbps, fps, ceiling)
            peak = _file_peak(out)
            if peak is None or peak <= -1.0:
                return
            print(f"    피크 {peak:.1f} dBFS → 리미터 한도를 낮춰 다시 내보냄")

    def _export_once(self, seq: int, out: Path, preset: str, video_kbps: int, audio_kbps: int, fps: float,
                     ceiling: float) -> None:
        self.master(seq, ceiling)
        settings = self.fc.run("export.presets.get", name=preset)["settings"]
        if self.frame_size:
            settings["frameSize"] = list(self.frame_size)
        settings["bitrateKbps"] = video_kbps
        settings["maxBitrateKbps"] = round(video_kbps * 1.3)
        settings["frameRate"] = {"num": round(fps * 1000), "den": 1000} if fps % 1 else {"num": int(fps), "den": 1}
        settings["audio"]["bitrate_kbps"] = audio_kbps
        settings["audio"]["sample_rate"] = 48000
        settings["effects"]["loudness"] = {
            "enabled": True, "target_lufs": self.cfg["audio"]["target_lufs"], "true_peak_dbtp": -1.5,
        }
        settings["metadata"]["title"] = self.plan.youtube_title or self.plan.concept
        out.parent.mkdir(parents=True, exist_ok=True)
        self.fc.run("sequence.open", item=seq)
        self.fc.run("file.exportMedia", path=str(out), settings=settings, sequence=seq, wait=True)


# --- 진입점 ---------------------------------------------------------------------------------


def build(episode_dir: Path, targets: list[str] | None = None, export: bool = True, verbose: bool = False) -> dict:
    episode_dir = episode_dir.resolve()
    plan = Plan.load(episode_dir)
    report = validate(plan)
    report.print()
    if not report.ok:
        raise SystemExit("plan.json 오류를 고친 뒤 다시 실행하세요")
    plan.fill_ends()
    targets = targets or [t for t, on in plan.outputs.items() if on]
    out_dir = episode_dir / "out"
    manifest: dict = {"episode": plan.id, "built_at": datetime.now().isoformat(timespec="seconds"), "outputs": {}}
    old_manifest = episode_dir / "build.json"
    if old_manifest.exists() and set(targets) != {t for t, on in plan.outputs.items() if on}:
        prev = json.loads(old_manifest.read_text(encoding="utf-8"))
        manifest["outputs"] = {k: v for k, v in prev.get("outputs", {}).items() if k not in targets}
        for k in ("cover", "thumbnail"):
            if k in prev:
                manifest[k] = prev[k]
    started = time.time()

    # 일부만 다시 만들 때(--only)는 기존 프로젝트를 열고 해당 시퀀스만 교체한다 (다른 편집본 보존)
    project_path = episode_dir / "project.fcproj"
    all_targets = [t for t, on in plan.outputs.items() if on]
    partial = set(targets) != set(all_targets) and project_path.exists()
    with FilmCraft(project=project_path if partial else None, verbose=verbose) as fc:
        if partial:
            drop = set()
            for t in targets:
                cover = "cover" if t == "short" else f"cover_{t}"
                drop |= {t, f"{t}_media"} | ({"thumbnail", "thumbnail_photo"} if t == "long" else {cover, f"{cover}_photo"})
            stale = [c["item"] for c in fc.inspect()["project"]["root"]["children"]
                     if c["type"] == "Sequence" and c["name"] in drop]
            if stale:
                fc.run("project.delete", items=stale)
        b = Builder(fc, plan, episode_dir, name="long")  # 롱폼·롱폼 썸네일: "육아일지 #3" (1·2부 표시 없음)
        b.hdr = any(probe(config.resolve(c.src))["hdr"] for c in plan.clips)
        builders = {"long": b}
        results = {}
        for target in SHORT_TARGETS:
            if target in targets:
                print(f"▶ {TARGET_NAMES[target]}(9:16) 조립")
                sb = Builder(fc, plan.part(target), episode_dir, name=target)
                sb._items, sb.hdr = b._items, b.hdr
                builders[target] = sb
                results[target] = sb.build_short()
        if "long" in targets:
            print("▶ 롱폼(16:9) 조립")
            results["long"] = b.build_long()
            print("▶ 썸네일")
            b.thumbnail(out_dir / "thumbnail.jpg")

        for target, res in results.items():
            seq = res["sequence"]
            tb = builders[target]
            if target in SHORT_TARGETS:
                cfg = b.cfg["short"]
                tb.cover(out_dir / COVER[target])  # 쇼츠 썸네일 (릴스·틱톡 커버 공용)
                preset, size = "Social Vertical 1080×1920", (cfg["width"], cfg["height"])
            else:
                cfg = b.cfg["long"]
                preset, size = "YouTube 1080p Full HD", (cfg["width"], cfg["height"])
            preview = out_dir / f"preview_{target}.png"
            sheet(fc, seq, res["duration"], size[0], size[1], preview, times=_preview_times(tb.plan, res))
            path = out_dir / f"{target}.mp4"
            # 업로드 해상도: 편집은 1080 기준 좌표로, 내보낼 때만 1440 으로 (원본 4K 를 그 해상도로 다시 그림 → 실제로 더 선명).
            # YouTube 는 1440 이상 업로드에 고화질 코덱(VP9/AV1)을 줘서 1080 으로 보는 사람에게도 덜 뭉개진다 (docs/STYLE.md 14번)
            # (쇼츠·롱폼이 같은 Builder 를 쓸 수 있으므로 매번 정한다. render.py 는 이 루프를 거치지 않음)
            tb.frame_size = (cfg["upload_width"], cfg["upload_height"]) if cfg.get("upload_width") else None
            kbps = cfg.get("upload_kbps", cfg["video_kbps"]) if tb.frame_size else cfg["video_kbps"]
            if export:
                print(f"▶ 내보내기 {rel(path)} ({res['duration']:.1f}s"
                      + (f", {tb.frame_size[0]}x{tb.frame_size[1]}" if tb.frame_size else "") + ")")
                tb.export(seq, path, preset, kbps, cfg["audio_kbps"], cfg["fps"])
            manifest["outputs"][target] = {
                "file": rel(path) if export else None,
                "preview": rel(preview),
                "duration": round(res["duration"], 2),
                "segments": [asdict(s) for s in res["segments"]],
            }
            if target in SHORT_TARGETS:
                manifest["outputs"][target]["cover"] = rel(out_dir / COVER[target])
                if tb.plan.music:
                    info = music.resolve(tb.plan.music)
                    manifest["outputs"][target]["music"] = {"id": info["id"], "title": info["title"],
                                                            "attribution": info["attribution"]}
            if tb.music_used:
                manifest["outputs"][target]["music_all"] = tb.music_used
            blocked = music.blocked_platforms(tb.plan.music)
            if target in SHORT_TARGETS and blocked and export:
                # TikTok 처럼 이 BGM 이 음소거되는 플랫폼용: 배경음악만 뺀 버전 (아기 소리·효과음·내레이션은 그대로)
                nb = out_dir / f"{target}_nobgm.mp4"
                print(f"▶ 내보내기 {rel(nb)} (BGM 없음 → {', '.join(blocked)} 용)")
                tb.export_without_music(seq, nb, preset, kbps, cfg["audio_kbps"], cfg["fps"])
                manifest["outputs"][target]["file_nobgm"] = rel(nb)
                manifest["outputs"][target]["nobgm_for"] = blocked
        fc.run("file.saveAs", path=str(episode_dir / "project.fcproj"))

    manifest["ai_voice"] = any(cap.voice for c in plan.clips for cap in c.captions)
    if manifest["ai_voice"]:
        manifest["tts_engine"] = voice.engine()
        engines = sorted({voice.resolve_engine(voice.voice_config(n)) for n in voice_names(plan)})
        manifest["tts_engines"] = engines  # 'clone' = 아빠 본인 목소리 (Qwen3-TTS)
    if plan.music:
        info = music.resolve(plan.music)
        manifest["music"] = {"id": info["id"], "title": info["title"], "attribution": info["attribution"]}
    if "long" in targets or "thumbnail" not in manifest:
        manifest["thumbnail"] = rel(out_dir / "thumbnail.jpg")
    if "short" in results:
        manifest["cover"] = rel(out_dir / "cover.jpg")
    for t in SHORT_TARGETS:  # 다시 만들지 않은 쇼츠의 기존 결과(cover·music)는 보존
        if t in manifest["outputs"] and t not in results and "cover" not in manifest["outputs"][t]:
            manifest["outputs"][t]["cover"] = rel(out_dir / COVER[t])
    from .qa import layout_problems

    for target in results:
        errs, warns = layout_problems(episode_dir / "project.fcproj", target)
        for w in warns:
            print(f"    ! 검수 {w}")
        for e in errs:
            print(f"    ✗ 검수 {e}")
        manifest["outputs"][target]["layout_errors"] = errs
    manifest["build_seconds"] = round(time.time() - started, 1)
    _mark_used(plan)
    from . import series

    series.write()
    (episode_dir / "build.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _mark_used(plan: Plan) -> None:
    """library/index.json 에 어떤 에피소드에서 쓴 클립인지 기록 (자동 기획 시 중복 방지)."""
    from .media import load_index, save_index

    index = load_index()
    changed = False
    for c in plan.clips:
        entry = index.get(c.src)
        if entry is not None and plan.id not in entry.setdefault("used_in", []):
            entry["used_in"].append(plan.id)
            changed = True
    if changed:
        save_index(index)


def _file_peak(path: Path) -> float | None:
    """완성 파일의 샘플 피크(dBFS). macOS afconvert 로 풀어서 잰다. 실패하면 None."""
    import array
    import math
    import subprocess
    import tempfile
    import wave

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "p.wav"
        try:
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@48000", str(path), str(wav)], check=True,
                           capture_output=True)
            with wave.open(str(wav)) as w:
                a = array.array("h")
                a.frombytes(w.readframes(w.getnframes()))
        except (OSError, subprocess.CalledProcessError, wave.Error):
            return None
    m = max((abs(x) for x in a), default=0)
    return 20 * math.log10(m / 32768) if m else -120.0


def sticker_spot(x: float, y: float, px: float, box: Box, face, busy: list | None = None) -> tuple[float, float]:
    """스티커 중심. 얼굴(머리)이나 같은 시간 자막과 겹치면 좌우 반대편 → 위 → 아래 순으로 옮긴다."""
    def at(xx: float, yy: float) -> tuple[float, float]:
        return box.x + xx * box.w, box.y + yy * box.h

    avoid = list(busy or [])
    if face:
        avoid.append((box.x + face[0], box.y + face[1], box.x + face[2], box.y + face[3]))
    if not avoid:
        return at(x, y)

    def hits(cx: float, cy: float) -> bool:
        h = px / 2 * 0.8 + 16  # 여유 16px (QA '스티커가 자막을 가림' 기준보다 넉넉히)
        return any(cx - h < f[2] and f[0] < cx + h and cy - h < f[3] and f[1] < cy + h for f in avoid)

    for xx, yy in ((x, y), (1 - x, y), (x, 0.1), (1 - x, 0.1), (x, 0.85), (1 - x, 0.85)):
        cx, cy = at(xx, yy)
        if not hits(cx, cy):
            return cx, cy
    return at(x, y)


def _preview_times(plan: Plan, res: dict) -> list[float]:
    """자막이 뜬 순간 위주로 미리보기 시점을 고른다 (자막 위치·잘림 확인용)."""
    times = []
    for seg in res["segments"]:
        clip = plan.clips[seg.clip_index]
        for cap in clip.captions:
            times.append(seg.start + cap.at + min(0.6, cap.dur / 2))
        times.append((seg.start + seg.end) / 2)
    times = sorted({round(t, 2) for t in times if t < res["duration"]})
    if len(times) > 12:
        step = len(times) / 12
        times = [times[int(i * step)] for i in range(12)]
    return times or [res["duration"] / 2]
