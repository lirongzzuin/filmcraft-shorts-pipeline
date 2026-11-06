"""단위 테스트 (FilmCraft 불필요, 몇 초). 실행: ./br-test 또는 .venv/bin/python -m unittest tests.test_unit"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
import types
import unittest
import wave
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from babyreel import config, deliver, metadata, qa  # noqa: E402
from babyreel.plan import Plan, has_emoji, validate  # noqa: E402

TEST_PLAN = ROOT / "episodes" / "_test"
os.environ.pop("BABYREEL_ORDER_DIR", None)  # selftest 의 날짜순 폴더 위치가 테스트에 섞이지 않게 (필요한 테스트만 직접 지정)


def load_test_plan() -> Plan:
    return Plan.load(TEST_PLAN)


class TempEpisode:
    """테스트 plan 을 임시 에피소드 폴더로 복사 (publish.json 이 실제 폴더에 안 남게)."""

    def __enter__(self) -> Path:
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name) / "_unit"
        d.mkdir()
        (d / "plan.json").write_text((TEST_PLAN / "plan.json").read_text(encoding="utf-8"), encoding="utf-8")
        return d

    def __exit__(self, *exc: object) -> None:
        self.tmp.cleanup()


class ConfigTest(unittest.TestCase):
    def test_channel(self):
        c = config.load()
        self.assertEqual(c["channel"]["name"], "<채널 이름>")
        self.assertEqual(c["channel"]["handle"], "@example_channel")
        self.assertEqual([x["nick"] for x in config.children()], ["첫째", "둘째"])
        self.assertEqual(config.series_label("siblings"), "남매 대전")

    def test_age_format(self):
        on = date(2016, 10, 17)
        self.assertEqual(config.age_of("2016-09-01", on), "D+46")
        self.assertEqual(config.age_of("2016-02-10", on), "8개월")
        self.assertEqual(config.age_of("2013-06-21", on), "3살")
        self.assertIsNone(config.age_of("", on))
        self.assertIsNone(config.age_of("2017-01-01", on))

    def test_ages_line_filters_cast(self):
        kids = [{"nick": "첫째", "birth_date": "2013-06-21"}, {"nick": "둘째", "birth_date": "2016-02-10"}]
        with mock.patch.object(config, "children", return_value=kids):
            self.assertEqual(config.ages(date(2016, 10, 17)), "첫째 3살 · 둘째 8개월")
            self.assertEqual(config.ages(date(2016, 10, 17), ["둘째"]), "둘째 8개월")


class PlanValidationTest(unittest.TestCase):
    def test_test_plan_is_valid(self):
        r = validate(load_test_plan())
        self.assertTrue(r.ok, r.errors)

    def mutate(self, fn) -> list[str]:
        raw = asdict(load_test_plan())
        fn(raw)
        return validate(Plan.from_dict(raw)).errors

    def test_emoji_in_caption_rejected(self):
        errs = self.mutate(lambda p: p["clips"][0]["captions"][0].update(text="웃김🤣"))
        self.assertTrue(any("이모지" in e for e in errs))

    def test_unknown_series_and_cast(self):
        errs = self.mutate(lambda p: p.update(series="nope", cast=["철수"]))
        self.assertTrue(any("series" in e for e in errs))
        self.assertTrue(any("cast" in e for e in errs))

    def test_unknown_speaker_voice_sfx(self):
        def f(p):
            p["clips"][0]["captions"][2].update(speaker="철수", voice=True, sfx="nope")
        errs = self.mutate(f)
        self.assertTrue(any("speaker" in e for e in errs))
        self.assertTrue(any("효과음" in e for e in errs))

    def test_caption_outside_clip(self):
        errs = self.mutate(lambda p: p["clips"][0]["captions"][0].update(at=99))
        self.assertTrue(any("밖" in e for e in errs))

    def test_full_name_warns(self):
        raw = asdict(load_test_plan())
        raw["description"] = "이훤의 하루"
        r = validate(Plan.from_dict(raw))
        self.assertTrue(any("성+이름" in w for w in r.warnings))

    def test_unknown_field_rejected(self):
        raw = asdict(load_test_plan())
        raw["typo_field"] = 1
        with self.assertRaises(ValueError):
            Plan.from_dict(raw)

    def test_youtube_only_music_blocked_for_shorts(self):
        from babyreel import music
        fake = {"id": "x", "title": "x", "path": Path("/x"), "license": "yt", "attribution": "yt",
                "platforms": ["youtube"]}
        with mock.patch.object(music, "resolve", return_value=fake):
            errs = validate(load_test_plan()).errors
        self.assertTrue(any("Instagram" in e for e in errs))

    def test_safety_gate(self):
        p = load_test_plan()
        p.safety_reviewed = False
        self.assertTrue(any("safety" in w for w in validate(p, for_package=True).warnings))
        self.assertTrue(any("safety" in e for e in validate(p, for_package=True, require_review=True).errors))

    def test_fun_checklist(self):
        from babyreel.plan import fun_report
        p = load_test_plan()
        self.assertEqual(fun_report(p.clips_for("short"), p.music), [])  # 테스트 plan 은 기준 충족
        for c in p.clips:
            c.sfx, c.stickers, c.punch, c.hold = [], [], [], False
            for cap in c.captions:
                cap.sfx, cap.style = None, "sub"
        p.music.track = "wallpaper"
        self.assertEqual(len(fun_report(p.clips_for("short"), p.music)), 5)

    def test_pacing_report(self):
        from babyreel.plan import pacing_report
        p = load_test_plan()
        p.fill_ends()
        self.assertFalse(any("첫 0.6초" in w for w in pacing_report(p.clips_for("short"))))
        self.assertTrue(any("첫 컷" in w for w in pacing_report(p.clips_for("short"))))  # 첫 컷 5.5s
        for c in p.clips[0].captions:
            c.at = max(c.at, 1.0)
        self.assertTrue(any("첫 0.6초" in w for w in pacing_report(p.clips_for("short"))))

    def test_long_hold_warns(self):
        raw = asdict(load_test_plan())
        raw["clips"][1]["end"] = raw["clips"][1]["start"] + 3.0
        r = validate(Plan.from_dict(raw))
        self.assertTrue(any("정지 화면" in w for w in r.warnings))

    def test_emoji_detector(self):
        self.assertTrue(has_emoji("👶"))
        self.assertFalse(has_emoji("기록 결과, 공을 발견함"))


def fake_manifest() -> dict:
    # _test plan 의 clips[3] 은 챕터 없는 사진 클립이라 0,1,2,4 번 클립으로 구성
    segs = [{"clip_index": i, "start": s, "end": e, "chapter": c}
            for i, s, e, c in [(0, 0, 12, "첫 만남"), (1, 12, 25, "정적"), (2, 25, 40, "도망"), (4, 40, 52, "다시 보기")]]
    return {
        "outputs": {"short": {"file": "episodes/_test/out/short.mp4", "segments": segs, "duration": 21},
                    "long": {"file": "episodes/_test/out/long.mp4", "segments": segs, "duration": 60}},
        "cover": "episodes/_test/out/cover.jpg", "thumbnail": "episodes/_test/out/thumbnail.jpg",
        "music": {"attribution": '"Fluffing a Duck" Kevin MacLeod'}, "ai_voice": True,
    }


class MetadataTest(unittest.TestCase):
    def setUp(self):
        self.plan = load_test_plan()
        self.meta = metadata.build_metadata(self.plan, fake_manifest())

    def test_signature_and_credits(self):
        d = self.meta["youtube_shorts"]["description"]
        self.assertIn("📔 <채널 이름>", d)
        self.assertIn("육아일지 No.7-1 · 남매 대전", d)  # 쇼츠를 1·2부로 나눈 회차
        self.assertNotIn("#7", d)  # 숫자 해시태그가 생기면 안 됨
        self.assertIn("Kevin MacLeod", d)
        self.assertIn("AI 음성", d)
        self.assertIn("#shorts", d)

    def test_instagram_hashtag_limit(self):
        tags = [w for w in self.meta["instagram_reels"]["caption"].split() if w.startswith("#")]
        self.assertLessEqual(len(tags), config.load()["metadata"]["instagram_max_hashtags"])
        self.assertIn("#채널해시태그", tags)

    def test_youtube_fields(self):
        y = self.meta["youtube_shorts"]
        self.assertFalse(y["made_for_kids"])
        self.assertIn("첫째", y["tags"])
        self.assertLessEqual(len(y["title"]), 100)

    def test_chapters(self):
        d = self.meta["youtube_long"]["description"]
        self.assertIn("00:00 첫 만남", d)
        self.assertIn("00:40 다시 보기", d)


class ChapterMergeTest(unittest.TestCase):
    def test_short_chapter_merges_into_next(self):
        plan = load_test_plan()
        segs = [{"clip_index": 0, "start": 0, "end": 5}, {"clip_index": 1, "start": 5, "end": 20},
                {"clip_index": 2, "start": 20, "end": 40}, {"clip_index": 4, "start": 40, "end": 60}]
        chap = metadata.chapters(plan, segs)
        self.assertEqual(chap[0], "00:00 첫 만남 · 정적")
        self.assertEqual(len(chap), 3)


class CaptionLayoutTest(unittest.TestCase):
    def test_overlap_resolved_and_talk_lifted(self):
        from babyreel.build import Box, Builder, Geometry, Segment
        from babyreel.plan import Caption
        p = load_test_plan()
        clip = p.clips[0]
        clip.captions = [
            Caption(at=0.0, text="첫 내레이션", dur=3.0, style="sub"),
            Caption(at=2.0, text="둘째 내레이션\n두 줄", dur=2.0, style="sub"),
            Caption(at=2.2, text="대사", dur=1.5, style="talk", speaker="둘째"),
        ]
        b = Builder(None, p, TEST_PLAN)
        geo = Geometry(box=Box(0, 0, 1080, 1200), text_bottom=1100, sizes={"sub": 46, "talk": 60, "big": 120, "chip": 62},
                       chip_y=110)
        items = b.layout_captions([Segment(clip_index=0, start=10.0, end=15.5)], geo)
        first, second, talk = items
        self.assertLessEqual(first["t"] + first["dur"], second["t"] - 0.04)  # 앞 자막이 다음 자막 전에 끝남
        self.assertAlmostEqual(talk["lift"], 46 * 1.25)  # 두 줄 내레이션 위로 한 줄만큼 올라감


class DeliverTest(unittest.TestCase):
    def test_place_is_independent_copy(self):
        """다시 빌드해 out/ 파일이 제자리에서 바뀌어도 전달 폴더 파일은 그대로 (하드링크 금지)."""
        with tempfile.TemporaryDirectory() as tmp:
            src, dest = Path(tmp) / "short.mp4", Path(tmp) / "d" / "쇼츠영상.mp4"
            src.write_bytes(b"v1")
            deliver.place(src, dest)
            with open(src, "r+b") as f:  # 내보내기가 같은 파일에 덮어쓰는 상황
                f.write(b"XX")
            self.assertEqual(dest.read_bytes(), b"v1")
            self.assertNotEqual(src.stat().st_ino, dest.stat().st_ino)
            self.assertFalse(list(dest.parent.glob(".*.part")))

    def test_place_skips_same_file_and_recopies_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dest = Path(tmp) / "short.mp4", Path(tmp) / "d" / "숏폼영상.mp4"
            src.write_bytes(b"v1")
            deliver.place(src, dest)
            first = dest.stat().st_ino
            deliver.place(src, dest)  # 같은 파일 → 다시 복사하지 않음
            self.assertEqual(dest.stat().st_ino, first)
            src.write_bytes(b"v2-new")  # 다시 빌드 → 새 파일로
            deliver.place(src, dest)
            self.assertEqual(dest.read_bytes(), b"v2-new")

    def test_folder_is_episode_label(self):
        """업로드 폴더 = <회차>/숏폼1·숏폼2·롱폼. 플랫폼이 달라도 같은 영상이면 같은 폴더."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"BABYREEL_DELIVER_DIR": tmp}):
            a, b = load_test_plan(), load_test_plan()
            b.id = "other"
            self.assertEqual(deliver.folder(a, "instagram", "short"), Path(tmp) / a.id / "숏폼1")
            self.assertEqual(deliver.folder(a, "tiktok", "short"), deliver.folder(a, "youtube", "short"))
            self.assertEqual(deliver.folder(a, "youtube", "short2").name, "숏폼2")
            self.assertEqual(deliver.folder(a, "youtube", "long").name, "롱폼")
            self.assertNotEqual(deliver.folder(b, "instagram"), deliver.folder(a, "instagram"))


class OutsideBlurTest(unittest.TestCase):
    """창밖 풍경 흐리게 (2026-11-03): blur_areas 스키마·검증, 창밖 의심 칸 판정."""

    def test_blur_area_follows_points(self):
        from babyreel.plan import Blur
        b = Blur(points=[[1.0, 0.0, 0.0, 0.3, 0.4], [3.0, 0.2, 0.0, 0.5, 0.4]])
        self.assertEqual(b.at(0.0), [0.0, 0.0, 0.3, 0.4])  # 처음 전은 첫 영역
        self.assertEqual([round(v, 3) for v in b.at(2.0)], [0.1, 0.0, 0.4, 0.4])
        self.assertEqual(b.at(9.0), [0.2, 0.0, 0.5, 0.4])
        self.assertEqual(Blur(box=[0.5, 0.0, 1.0, 0.3]).at(5.0), [0.5, 0.0, 1.0, 0.3])

    def test_blur_area_validation(self):
        raw = json.loads((TEST_PLAN / "plan.json").read_text(encoding="utf-8"))
        raw["clips"][0]["blur_areas"] = [{"box": [0.6, 0.3, 0.2, 0.5]}, {"box": [0, 0, 1, 1], "points": [[0, 0, 0, 1, 1]]}]
        r = validate(Plan.from_dict(raw))
        self.assertTrue(any("blur_areas[0]" in e for e in r.errors), r.errors)
        self.assertTrue(any("blur_areas[1]" in e and "하나만" in e for e in r.errors), r.errors)

    def test_suspects(self):
        from babyreel import outside
        frames = [{"t": 0, "cells": [[2, 0, "building", 0.45], [0, 2, "window", 0.35], [1, 1, "sky", 0.45],
                                     [1, 1, "window", 0.4], [0, 1, "tree", 0.3]]}]
        found = outside.suspects(frames)
        self.assertEqual(set(found), {(2, 0), (1, 1)})  # 강한 label 하나, 또는 약한 label 둘 이상 (창틀만은 아님)
        self.assertIn("오른쪽 위(건물 0.45)", outside.describe(found))
        self.assertEqual(outside.suspects([{"t": 0, "cells": [[0, 0, "sky", 0.5], [0, 0, "blue_sky", 0.5]]}]), {})  # 하늘만은 아님
        left = outside.covered(found, [[0.6, 0.0, 1.0, 0.4]])  # 오른쪽 위를 가림
        self.assertEqual(set(left), {(1, 1)})


class LoudnessTest(unittest.TestCase):
    def test_sine_reference(self):
        """1kHz 스테레오 사인파 진폭 0.1 → 약 -20 LUFS (BS.1770 기준)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sine.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(2), w.setsampwidth(2), w.setframerate(48000)
                frames = bytearray()
                for i in range(48000 * 3):
                    v = int(0.1 * math.sin(2 * math.pi * 1000 * i / 48000) * 32767)
                    frames += struct.pack("<hh", v, v)
                w.writeframes(bytes(frames))
            lv = qa.loudness(path)
        self.assertAlmostEqual(lv["integrated_lufs"], -20.0, delta=0.7)
        self.assertAlmostEqual(lv["sample_peak_dbfs"], -20.0, delta=0.2)


class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BABYREEL_DELIVER_DIR": self.tmp.name})
        self.env.start()
        media = Path(self.tmp.name) / "media"
        media.mkdir()
        for name in ("short.mp4", "cover.jpg", "thumbnail.jpg"):
            (media / name).write_bytes(b"x")
        self.meta = metadata.build_metadata(load_test_plan(), fake_manifest())
        for m in self.meta.values():
            m["file"] = str(media / "short.mp4")
            if "cover" in m:
                m["cover"] = str(media / "cover.jpg")
            if "thumbnail" in m:
                m["thumbnail"] = str(media / "thumbnail.jpg")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_manual_instagram_folder(self):
        from babyreel.publish.manual import ManualPublisher
        with TempEpisode() as ep:
            r = ManualPublisher(ep, "_unit").publish("instagram_reels", self.meta["instagram_reels"])
            d = deliver.folder(Plan.load(ep), "instagram", "short")
        self.assertEqual(r.status, "packaged")
        self.assertEqual(d.name, "숏폼1")
        for f in ("숏폼영상.mp4", "커버.jpg", "인스타_캡션.txt", "인스타_업로드방법.txt"):
            self.assertTrue((d / f).exists(), f)
        self.assertIn("'숏폼영상.mp4'", (d / "인스타_업로드방법.txt").read_text(encoding="utf-8"))

    def test_manual_youtube_folder(self):
        from babyreel.publish.manual import ManualPublisher
        with TempEpisode() as ep:
            ManualPublisher(ep, "_unit").publish("youtube_shorts", self.meta["youtube_shorts"])
            ManualPublisher(ep, "_unit").publish("youtube_long", self.meta["youtube_long"])
            plan = Plan.load(ep)
            short, long = deliver.folder(plan, "youtube", "short"), deliver.folder(plan, "youtube", "long")
        guide = (short / "유튜브_업로드방법.txt").read_text(encoding="utf-8")
        self.assertIn("관련 동영상", guide)  # 쇼츠 → 롱폼 연결 안내
        self.assertIn("'유튜브_제목.txt'", guide)
        for f in ("숏폼영상.mp4", "커버.jpg", "유튜브_제목.txt", "유튜브_설명.txt", "유튜브_태그.txt"):
            self.assertTrue((short / f).exists(), f)
        self.assertIn("'예약'", (long / "유튜브_업로드방법.txt").read_text(encoding="utf-8"))
        self.assertTrue((long / "롱폼영상.mp4").exists() and (long / "썸네일.jpg").exists())

    def test_manual_all_platforms_share_short_folder(self):
        """숏폼1 폴더 하나에 YouTube·인스타·틱톡 파일이 겹치지 않고 모두 (쇼츠=릴스 영상은 한 파일)."""
        from babyreel.publish.manual import ManualPublisher
        with TempEpisode() as ep:
            for t in ("youtube_shorts", "instagram_reels", "tiktok"):
                ManualPublisher(ep, "_unit").publish(t, self.meta[t])
            d = deliver.folder(Plan.load(ep), "tiktok", "short")
        names = {f.name for f in d.iterdir()}
        self.assertTrue({"숏폼영상.mp4", "틱톡영상.mp4", "커버.jpg", "유튜브_업로드방법.txt", "인스타_업로드방법.txt",
                         "틱톡_업로드방법.txt", "인스타_캡션.txt", "틱톡_캡션.txt"} <= names, names)
        self.assertEqual([n for n in names if n.endswith(".mp4")].__len__(), 2)

    def test_manual_tiktok_folder(self):
        from babyreel.publish.manual import ManualPublisher
        with TempEpisode() as ep:
            ManualPublisher(ep, "_unit").publish("tiktok", self.meta["tiktok"])
            d = deliver.folder(Plan.load(ep), "tiktok", "short")
        for f in ("틱톡영상.mp4", "커버.jpg", "틱톡_캡션.txt", "틱톡_업로드방법.txt"):
            self.assertTrue((d / f).exists(), f)

    def test_youtube_api_uploads_private_draft(self):
        """google 라이브러리를 가짜로 바꿔 업로드 요청 본문과 결과 기록을 검증."""
        calls = {}

        class Req:
            def next_chunk(self):
                return None, {"id": "VID123"}

        class Videos:
            def insert(self, part, body, media_body):
                calls["body"] = body
                return Req()

        class Thumbs:
            def set(self, videoId, media_body):
                calls["thumb"] = videoId
                return types.SimpleNamespace(execute=lambda: None)

        service = types.SimpleNamespace(videos=lambda: Videos(), thumbnails=lambda: Thumbs())
        http = types.ModuleType("googleapiclient.http")
        http.MediaFileUpload = lambda *a, **k: object()
        from babyreel.publish import youtube_api
        with mock.patch.object(youtube_api, "_service", return_value=service), \
                mock.patch.dict(sys.modules, {"googleapiclient.http": http}), TempEpisode() as ep:
            pub = youtube_api.YouTubePublisher(ep, "_unit")
            r = pub.publish("youtube_long", self.meta["youtube_long"])
        self.assertEqual(calls["body"]["status"]["privacyStatus"], "private")
        self.assertFalse(calls["body"]["status"]["selfDeclaredMadeForKids"])
        self.assertEqual(calls["thumb"], "VID123")
        self.assertEqual(r.status, "draft")
        self.assertIn("studio.youtube.com/video/VID123", r.detail)
        note = Path(self.tmp.name) / load_test_plan().id / "롱폼" / "유튜브_업로드결과.txt"
        self.assertIn("VID123", note.read_text(encoding="utf-8"))

    def test_youtube_api_without_secrets_raises(self):
        from babyreel.publish import youtube_api
        with mock.patch.object(youtube_api, "CLIENT_SECRET", Path("/nonexistent")), \
                mock.patch.object(youtube_api, "TOKEN", Path("/nonexistent-token")):
            with self.assertRaises(RuntimeError):
                youtube_api._service()

    def test_instagram_api_flow(self):
        from babyreel.publish import instagram_api
        responses = iter([{"id": "C1"}, {"status_code": "FINISHED"}, {"id": "M1"}, {"permalink": "https://ig/p/1"}])
        cfg = {**config.load(), "instagram": {"public_base_url": "https://cdn.example"}}
        with mock.patch.object(instagram_api, "_call", side_effect=lambda *a, **k: next(responses)), \
                mock.patch.dict(os.environ, {"IG_USER_ID": "1", "IG_ACCESS_TOKEN": "t"}), \
                mock.patch.object(config, "load", return_value=cfg), TempEpisode() as ep:
            r = instagram_api.InstagramPublisher(ep, "_unit").publish("instagram_reels", self.meta["instagram_reels"])
        self.assertEqual((r.status, r.url), ("uploaded", "https://ig/p/1"))

    def test_registry_and_record(self):
        from babyreel.publish import PublishResult, base, publisher_for, record
        with TempEpisode() as ep, mock.patch.object(base, "LOG", Path(self.tmp.name) / "log.jsonl"), \
                mock.patch.object(base, "DATA", Path(self.tmp.name)):
            self.assertEqual(publisher_for("instagram_reels", ep, "_unit").name, "manual")
            self.assertEqual(publisher_for("youtube_shorts", ep, "_unit").name, "youtube_api")
            record(ep, "_unit", PublishResult(target="youtube_shorts", status="draft", url="u"))
            state = json.loads((ep / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual(state["youtube_shorts"]["status"], "draft")


class FaceGuardTest(unittest.TestCase):
    """얼굴 보호 (docs/FEEDBACK.md 2026-10-17 '크롭될 때 아기 얼굴이 잘림')."""

    def test_moves_frame_so_head_is_inside(self):
        from babyreel import faces
        # 720x1280 세로 원본을 1080x1000 박스에 꽉 채우고 아래쪽을 보도록(focus_y 0.85) 둔 상태
        iw, ih, W, H = 720, 1280, 1080, 1000
        scale = W / iw * 100 * 1.3
        fw, fh = iw * scale / 100, ih * scale / 100
        py = min(fh / 2, max(H - fh / 2, H / 2 + (0.5 - 0.85) * fh))
        face = (0.35, 0.52, 0.65, 0.66)  # 화면 위 가장자리에 걸쳐 이마가 잘리는 얼굴
        vis = faces.visible([face], iw, ih, W, H, scale, W / 2, py)
        self.assertEqual(vis, [face])
        u = faces.union(vis)
        s2, px2, py2 = faces.fit(u, iw, ih, W, H, scale, W / iw * 100, W / 2, py, cover=True)
        top = py2 - ih * s2 / 200 + u[1] * ih * s2 / 100
        bottom = py2 - ih * s2 / 200 + u[3] * ih * s2 / 100
        self.assertGreaterEqual(top, -0.5)
        self.assertLessEqual(bottom, H + 0.5)
        # 꽉 채움 유지 (빈 곳 없음)
        self.assertGreaterEqual(ih * s2 / 100 / 2 + py2, H - 0.5)
        self.assertLessEqual(py2 - ih * s2 / 200, 0.5)

    def test_hidden_faces_stay_hidden(self):
        from babyreel import faces
        iw, ih, W, H = 720, 1280, 1080, 1000
        scale = W / iw * 100
        fh = ih * scale / 100
        py = min(fh / 2, max(H - fh / 2, H / 2 + (0.5 - 0.9) * fh))  # 아래쪽만 보는 구도
        stranger = (0.4, 0.02, 0.6, 0.12)  # 화면 밖(위) 타인 얼굴
        self.assertEqual(faces.visible([stranger], iw, ih, W, H, scale, W / 2, py), [])
        # crop 으로 가린 영역의 얼굴도 제외
        self.assertEqual(faces.visible([(0.8, 0.5, 0.95, 0.6)], iw, ih, W, H, scale, W / 2, H / 2,
                                       crop=[0, 0, 30, 0]), [])

    def test_guard_never_reveals_hidden_text(self):
        from babyreel import faces
        iw, ih, W, H = 720, 1280, 1080, 1000
        min_scale = W / iw * 100
        scale = min_scale * 1.75  # 일부러 크게 확대해 오른쪽 담요 글씨를 뺀 장면
        fw, fh = iw * scale / 100, ih * scale / 100
        px, py = W / 2, min(fh / 2, max(H - fh / 2, H / 2 + (0.5 - 0.3) * fh))
        found = {"faces": [(0.3, 0.16, 0.62, 0.47)], "texts": [(0.82, 0.15, 0.95, 0.4)]}
        u, hidden, frame, note = faces.guarded(found, iw, ih, W, H, scale, min_scale, px, py, True, None, zoom=1.75)
        self.assertAlmostEqual(frame[0], scale)  # zoom 1.5 이상 → 확대는 줄이지 않음
        self.assertLess(faces.shown(found["texts"][0], iw, ih, W, H, *frame), 0.15)
        # zoom 이 작아도(1.2) 보정이 글씨를 드러내면 그 보정은 쓰지 않는다
        scale = min_scale * 1.2
        fw, fh = iw * scale / 100, ih * scale / 100
        px = min(fw / 2, max(W - fw / 2, W / 2 + (0.5 - 0.25) * fw))
        py = min(fh / 2, max(H - fh / 2, H / 2 + (0.5 - 0.3) * fh))
        before = faces.shown(found["texts"][0], iw, ih, W, H, scale, px, py)
        _, _, frame, _ = faces.guarded(found, iw, ih, W, H, scale, min_scale, px, py, True, None, zoom=1.2)
        if before < 0.15:
            self.assertLess(faces.shown(found["texts"][0], iw, ih, W, H, *frame), 0.15)

    def test_guard_keeps_cropped_frame_full(self):
        from babyreel import faces
        iw, ih, W, H = 720, 1280, 1080, 1000
        min_scale = W / iw * 100
        scale = min_scale * 1.5
        crop = [0, 0, 20, 0]
        fw, fh = iw * scale / 100, ih * scale / 100
        px, py = W - fw / 2 + 0.2 * fw + 6, H / 2  # 오른쪽 20% crop 을 화면 밖으로 밀어 둔 구도
        self.assertTrue(faces.covers(iw, ih, W, H, (scale, px, py), crop))
        found = {"faces": [(0.05, 0.4, 0.3, 0.55)], "texts": []}
        _, _, frame, _ = faces.guarded(found, iw, ih, W, H, scale, min_scale, px, py, True, crop, zoom=1.5)
        self.assertTrue(faces.covers(iw, ih, W, H, frame, crop))  # crop 한 빈 띠가 화면에 생기지 않음

    def test_punch_zoom_keeps_face(self):
        from babyreel import faces
        iw, ih, W, H = 1080, 1920, 1080, 1000
        scale = 100.0
        u = faces.head((0.1, 0.3, 0.35, 0.42))  # 왼쪽 가장자리 근처 얼굴
        py = H / 2
        s2, px2, py2 = faces.zoom_about(u, iw, ih, W, H, scale, W / 2, py, 1.3, cover=True)
        left = px2 - iw * s2 / 200 + u[0] * iw * s2 / 100
        self.assertGreaterEqual(left, -0.5)  # 가운데 기준 확대였다면 화면 밖으로 밀려났을 얼굴


class MusicGapTest(unittest.TestCase):
    def test_playable_skips_inner_rest(self):
        from babyreel import music
        with mock.patch.object(music, "gaps", return_value=[(84.46, 84.81), (89.61, 89.96), (210.8, 219.95)]), \
                mock.patch("babyreel.media.probe", return_value={"duration_s": 220.0}):
            pieces = music.playable(Path("x.mp3"), 0.0, 152.6)
        self.assertEqual(pieces[0], (0.0, 84.46))
        self.assertEqual(pieces[1][0], 84.81)
        self.assertAlmostEqual(sum(b - a for a, b in pieces), 152.6, places=2)
        for a, b in pieces:  # 어떤 조각도 쉬는 구간을 지나지 않는다
            for g0, g1 in [(84.46, 84.81), (89.61, 89.96)]:
                self.assertFalse(a < g1 and g0 < b, (a, b))

    def test_short_track_loops_before_tail_silence(self):
        from babyreel import music
        with mock.patch.object(music, "gaps", return_value=[(62.0, 67.3)]), \
                mock.patch("babyreel.media.probe", return_value={"duration_s": 67.37}):
            pieces = music.playable(Path("x.mp3"), 0.0, 100.0)
        self.assertEqual(pieces[0], (0.0, 62.0))  # 곡 끝 무음 전에 처음으로
        self.assertEqual(pieces[1][0], 0.0)


class ShortPartTest(unittest.TestCase):
    def test_part_view(self):
        p = load_test_plan()
        self.assertEqual(p.shorts(), ["short", "short2"])
        v = p.part("short2")
        self.assertEqual(v.title_lines, ["공 쟁탈전", "2라운드ㅋㅋ"])
        self.assertEqual(v.youtube_title, "공 쟁탈전 2라운드ㅋㅋ")
        self.assertEqual(v.music.track, "fluffing_a_duck")  # 비우면 1부 음악
        self.assertEqual([c.src for c in v.clips_for("short")], ["tests/footage/test_portrait_02.mp4"])
        self.assertEqual(v.thumbnail.clip, 5)  # 인덱스 보존
        self.assertEqual((p.part("short").part_no, v.part_no), (1, 2))

    def test_validate_part_rules(self):
        p = load_test_plan()
        self.assertTrue(validate(p).ok)
        p.short2.youtube_title = p.youtube_title
        self.assertTrue(any("1부와 같습니다" in e for e in validate(p).errors))
        p = load_test_plan()
        p.clips[5].targets = ["long"]  # 2부 클립이 없음
        self.assertTrue(any("쇼츠 2부" in e for e in validate(p).errors))
        p = load_test_plan()
        p.outputs["short2"], p.short2 = False, None
        self.assertTrue(any("short2" in w for w in validate(p).warnings))

    def test_family_on_screen_warning(self):
        """아기만 나오면 수익 창출에 불리 → 부모 얼굴 컷이 없으면 경고 (2026-10-17 사용자 요청)."""
        p = load_test_plan()
        for c in p.clips:
            c.people = ["둘째"]
        w = validate(p).warnings
        self.assertTrue(any("엄마·아빠 얼굴이 한 번도" in x for x in w), w)
        self.assertTrue(any("[쇼츠 2부]" in x and "엄마·아빠" in x for x in w), w)
        for c in p.clips:
            c.people = ["둘째", "아빠"]
        self.assertFalse(any("엄마·아빠" in x for x in validate(p).warnings))

    def test_metadata_has_part2_targets(self):
        m = fake_manifest()
        m["outputs"]["short2"] = {"file": "episodes/_test/out/short2.mp4", "cover": "episodes/_test/out/cover2.jpg",
                                  "segments": [], "duration": 3, "file_nobgm": "episodes/_test/out/short2_nobgm.mp4",
                                  "nobgm_for": ["tiktok"]}
        meta = metadata.build_metadata(load_test_plan(), m)
        for t in ("youtube_shorts_2", "instagram_reels_2", "tiktok_2"):
            self.assertIn(t, meta)
        self.assertEqual(meta["youtube_shorts_2"]["title"], "공 쟁탈전 2라운드ㅋㅋ")
        self.assertIn("No.7-2", meta["youtube_shorts_2"]["description"])
        self.assertEqual(meta["tiktok_2"]["file"], "episodes/_test/out/short2_nobgm.mp4")
        self.assertEqual(meta["instagram_reels_2"]["cover"], "episodes/_test/out/cover2.jpg")


class EngagementTest(unittest.TestCase):
    """구독 부탁 한 줄 · 고정 댓글 · 재생목록 (2026-10-29)."""

    def test_cta_and_pinned_comment_in_metadata(self):
        m = fake_manifest()
        plan = load_test_plan()
        meta = metadata.build_metadata(plan, m)
        cta = config.load()["cta"]["description"]
        self.assertIn(cta, meta["youtube_shorts"]["description"])
        self.assertIn(cta, meta["instagram_reels"]["caption"])
        self.assertTrue(meta["youtube_shorts"]["pinned_comment"])
        self.assertTrue(meta["youtube_shorts"]["playlist"])
        if "youtube_long" in meta:
            self.assertIn(cta, meta["youtube_long"]["description"])
            self.assertNotEqual(meta["youtube_long"]["playlist"], meta["youtube_shorts"]["playlist"])

    def test_pinned_comment_rotates_and_respects_plan(self):
        plan = load_test_plan()
        a = metadata.pinned_comment(plan, "short")
        plan.episode_no += 1
        self.assertNotEqual(a, metadata.pinned_comment(plan, "short"))  # 회차마다 다른 질문
        plan.pinned_comment = "직접 쓴 댓글"
        self.assertEqual(metadata.pinned_comment(plan, "short"), "직접 쓴 댓글")
        plan.next_hook = "첫 이유식"
        self.assertIn("첫 이유식", metadata.pinned_comment(plan, "long"))

    def test_hype_only_when_enabled(self):
        cfg = config.load()
        self.assertFalse(cfg["cta"]["hype"])  # 파트너 프로그램 가입 전에는 하이프 부탁 안 함
        meta = metadata.build_metadata(load_test_plan(), fake_manifest())
        for v in meta.values():
            self.assertNotIn("하이프", v.get("description", "") + v.get("caption", ""))


class ScheduleTest(unittest.TestCase):
    def test_short_daily_long_every_two_days(self):
        from babyreel import schedule
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "BABYREEL_SCHEDULE": str(Path(tmp) / "s.json"), "BABYREEL_DELIVER_DIR": tmp}):
            d0 = date(2026, 10, 18)
            a = load_test_plan()
            got = schedule.assign(a, today=d0)
            # 롱폼은 쇼츠 2부와 같은 날 (long_day = "short2", 2026-10-29 사용자 실제 방식)
            self.assertEqual(got, {"short": d0, "short2": date(2026, 10, 19), "long": date(2026, 10, 19)})
            self.assertEqual(schedule.assign(a, today=date(2026, 10, 25)), got)  # 한 번 정하면 그대로
            b = load_test_plan(); b.id = "next"
            got_b = schedule.assign(b, today=d0)  # 다음 회차는 이어서
            self.assertEqual(got_b, {"short": date(2026, 10, 20), "short2": date(2026, 10, 21),
                                     "long": date(2026, 10, 21)})
            c = load_test_plan(); c.id = "late"
            got_c = schedule.assign(c, today=date(2026, 11, 1))  # 공백 뒤엔 오늘부터
            self.assertEqual(got_c["short"], date(2026, 11, 1))
            self.assertTrue((Path(tmp) / "업로드일정.txt").exists())

    def test_rebase_follows_published_then_reflows(self):
        """채널에서 실제 공개된 날짜로 고정하고, 남은 회차는 다음 날부터 쇼츠 매일·롱폼은 2부 날로 다시 잡는다."""
        from babyreel import schedule
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "BABYREEL_SCHEDULE": str(Path(tmp) / "s.json"), "BABYREEL_DELIVER_DIR": tmp}):
            schedule.save({
                "a": {"short": "2026-10-27", "short2": "2026-10-28", "long": "2026-10-29"},
                "b": {"short": "2026-10-31", "short2": "2026-11-01", "long": "2026-10-31"},
                "c": {"short": "2026-11-02", "short2": "2026-11-03", "long": "2026-11-02"},
            })
            pub = {("a", "short"): date(2026, 10, 27), ("a", "short2"): date(2026, 10, 28),
                   ("a", "long"): date(2026, 10, 28)}
            changes = schedule.rebase(date(2026, 10, 29), pub)
            s = schedule.load()
            self.assertEqual(s["a"]["long"], "2026-10-28")  # 실제 공개일로 고정
            self.assertEqual(s["b"], {"short": "2026-10-29", "short2": "2026-10-30", "long": "2026-10-30"})
            self.assertEqual(s["c"], {"short": "2026-10-31", "short2": "2026-11-01", "long": "2026-11-01"})
            self.assertIn(("a", "long", "2026-10-29", "2026-10-28"), changes)

    def test_channel_title_match_tolerates_small_edits(self):
        from babyreel import channel
        rows = [("ep1", "short2", "샘플 쇼츠 제목 하나ㅋㅋ (설명 괄호)"),
                ("ep1", "long", "샘플 롱폼 제목, 일주일 기록 | 육아일지 No.4"),
                ("ep2", "short", "샘플 쇼츠 제목 둘ㅋㅋ (아빠 메모)")]
        vids = [{"id": "x1", "kind": "short", "title": "샘플 쇼츠 제목 하나 (설명 괄호)"},
                {"id": "x2", "kind": "long", "title": "샘플 롱폼 제목, 일주일 기록 | 육아일지 No.4"},
                {"id": "x3", "kind": "short", "title": "샘플 쇼츠 제목 둘  (아빠 메모)"},
                {"id": "x4", "kind": "short", "title": "전혀 다른 영상"}]
        with mock.patch.object(channel, "episode_titles", return_value=rows):
            got = channel.match(vids)
        self.assertEqual(got[("ep1", "short2")]["id"], "x1")
        self.assertEqual(got[("ep1", "long")]["id"], "x2")
        self.assertEqual(got[("ep2", "short")]["id"], "x3")
        self.assertEqual(len(got), 3)

    def test_channel_match_keeps_recorded_id_after_title_edit(self):
        from babyreel import channel
        rows = [("ep9", "short2", "샘플 제목 셋, 올린 뒤 고치기 전 이름ㅋㅋ")]
        vids = [{"id": "x9", "kind": "short", "title": "완전히 바뀐 다른 제목!!"}]
        with mock.patch.object(channel, "episode_titles", return_value=rows), \
                mock.patch.object(channel, "known_ids", return_value={}):
            self.assertEqual(channel.match(vids), {})  # 제목만으로는 못 찾음
        with mock.patch.object(channel, "episode_titles", return_value=rows), \
                mock.patch.object(channel, "known_ids", return_value={"x9": ("ep9", "short2")}):
            self.assertEqual(channel.match(vids)[("ep9", "short2")]["id"], "x9")

    def test_stats_history_stays_small_and_flags_unlinked_long(self):
        from datetime import datetime, timedelta
        from babyreel import stats
        pub = datetime(2026, 10, 1, 18, tzinfo=stats.KST)
        vids = [{"id": "s1", "kind": "short", "title": "쇼츠 1", "published": pub, "views": 1000, "likes": 10},
                {"id": "s2", "kind": "short", "title": "쇼츠 2", "published": pub + timedelta(days=1), "views": 1200,
                 "likes": 5},
                {"id": "l1", "kind": "long", "title": "롱폼 | 육아일지 No.9", "published": pub + timedelta(days=1),
                 "views": 3, "likes": 1}]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(stats, "HISTORY", Path(tmp) / "v.json"):
            for day in range(40):  # 40일 매일 기록해도 정해진 날짜만 남는다
                stats.record(vids, now=pub + timedelta(days=day, hours=1))
            h = stats.load()["s1"]["h"]
            self.assertEqual([p[0] for p in h], [0, 1, 2, 3, 7, 14, 30, 39])
            self.assertEqual(stats.at_day(stats.load()["s1"], 10), 1000)
            found = {("ep", "short"): vids[0], ("ep", "short2"): vids[1], ("ep", "long"): vids[2]}
            r = stats.analyze(vids, found, now=pub + timedelta(days=5))
        self.assertEqual(len(r["link_check"]), 1)  # 롱폼 3회 / 쇼츠 2,200회 → 연결 확인
        self.assertIn("육아일지 No.9", stats.alerts(r)[0])
        self.assertEqual(r["crowded"], set())  # 하루 간격은 겹침 아님

    def test_short_opening_rule(self):
        from babyreel.plan import Caption, Clip, _opening_warnings
        hit = [Clip(src="a.mov", start=3.5, end=6.5, captions=[Caption(text="(불만 가득)", at=1.8, style="big")]),
               Clip(src="a.mov", start=10.8, end=14.0)]
        montage = [Clip(src="a.mov", start=0, end=3.3, captions=[Caption(text="유모차 탑승 완료", at=0.2, style="sub")]),
                   Clip(src="b.mov", start=0, end=4.2)]
        self.assertEqual(_opening_warnings(hit), [])
        out = " ".join(_opening_warnings(montage))
        self.assertIn("장면이 2개", out)
        self.assertIn("4초 안", out)

    def test_faces_cache_compacts(self):
        from babyreel import faces
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(faces, "CACHE", Path(tmp) / "f.json"):
            faces._save_cache({"v2|old": {"faces": []}, "v3|a": {"faces": [[0.123456789, 0.5, 0.75, 1.0]]}})
            self.assertEqual(faces._load_cache(), {"v3|a": {"faces": [[0.1235, 0.5, 0.75, 1.0]]}})

    def test_long_gap_respected_when_one_short(self):
        from babyreel import schedule
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "BABYREEL_SCHEDULE": str(Path(tmp) / "s.json"), "BABYREEL_DELIVER_DIR": tmp}):
            a = load_test_plan(); a.outputs["short2"] = False
            schedule.assign(a, today=date(2026, 10, 17))  # 쇼츠 10/17, 롱폼 10/17
            b = load_test_plan(); b.id = "b"
            got = schedule.assign(b, today=date(2026, 10, 17))
            self.assertEqual(got["short"], date(2026, 10, 18))
            self.assertEqual(got["long"], date(2026, 10, 19))  # 롱폼은 이틀 간격

    def test_resync_fixes_upload_day_and_board_shows_folder(self):
        """일정이 바뀌면 업로드방법 txt 첫 줄의 올릴 날만 고친다 (폴더 이름은 그대로). 일정판에 폴더 위치."""
        from babyreel import schedule
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "BABYREEL_SCHEDULE": str(Path(tmp) / "s.json"), "BABYREEL_DELIVER_DIR": tmp}):
            p = load_test_plan()
            schedule.assign(p, today=date(2026, 10, 18))
            for video in ("short", "short2", "long"):
                d = deliver.folder(p, "youtube", video)
                (d / "유튜브_업로드방법.txt").write_text("[올릴 날: 옛날]\n안내", encoding="utf-8")
            (deliver.folder(p, "tiktok", "short") / "틱톡_업로드방법.txt").write_text("[올릴 날: 옛날]\n", encoding="utf-8")
            fixed = deliver.resync()
            self.assertEqual(len(fixed), 4)
            got = (Path(tmp) / p.id / "숏폼2" / "유튜브_업로드방법.txt").read_text(encoding="utf-8")
            self.assertTrue(got.startswith(deliver.day_line(schedule.day_of(p, "short2").isoformat())), got)
            self.assertTrue(got.endswith("\n안내"))
            self.assertEqual(deliver.resync(), [])  # 다시 해도 바뀌는 것 없음
            board = schedule.write_board(date(2026, 10, 18)).read_text(encoding="utf-8")
            for label in ("숏폼1", "숏폼2", "롱폼"):
                self.assertIn(f"→ {p.id}/{label}", board)


    def test_order_folders_follow_schedule(self):
        """SSD 업로드/<회차 폴더>/<숏폼1 (날짜)>: 회차 폴더 이름순 = 올릴 순서, 안에 숏폼1·숏폼2·롱폼 구분.
        일정이 바뀌면 이름 바꾸기, 올리면 '올림', 다 올리면 '올린 영상' 안으로. episodes/<회차>/<구분> 은 링크라 파일이 따라간다."""
        from babyreel import schedule
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "BABYREEL_SCHEDULE": str(Path(tmp) / "s.json"), "BABYREEL_DELIVER_DIR": str(Path(tmp) / "eps"),
                "BABYREEL_ORDER_DIR": str(Path(tmp) / "업로드")}), \
                mock.patch.object(schedule, "titles", lambda ep: {"short": "굿모닝: 웃음?ㅋㅋ", "short2": "둘째 편", "long": "행복한 일상 | 육아일지 No.1"}):
            od = Path(tmp) / "업로드"
            ep_dir = od / "2026-11-04~05 행복한 일상"
            p = load_test_plan(); p.id = "2025-02-01_happy"
            schedule.assign(p, today=date(2026, 11, 4))
            d = deliver.folder(p, "youtube", "short")
            self.assertEqual(d, Path(tmp) / "eps" / p.id / "숏폼1")
            self.assertTrue(d.is_symlink())
            (d / "숏폼영상.mp4").write_bytes(b"v")
            for v in ("short2", "long"):
                deliver.folder(p, "youtube", v)
            self.assertEqual([x.name for x in od.iterdir() if x.is_dir()], [ep_dir.name])
            self.assertEqual(sorted(x.name for x in ep_dir.iterdir()), ["롱폼 (11-05 목)", "숏폼1 (11-04 수)", "숏폼2 (11-05 목)"])
            self.assertEqual(deliver.resync(), [])  # 이미 맞음
            self.assertEqual(deliver._safe("아주 " * 30), "아주 " * 19 + "아주…")  # 길면 단어 단위로
            self.assertEqual(deliver._span(["2026-10-31", "2026-11-01"]), "2026-10-31~11-01")
            s = schedule.load(); s[p.id]["short"] = "2026-11-03"; schedule.save(s)  # 일정이 바뀜
            new_dir = od / "2026-11-03~05 행복한 일상"
            self.assertEqual(deliver.resync(dry_run=True)[0],
                             f"폴더: {ep_dir.name}/숏폼1 (11-04 수) → {new_dir.name}/숏폼1 (11-03 화)")
            self.assertTrue((ep_dir / "숏폼1 (11-04 수)").is_dir())  # dry-run 은 그대로
            deliver.resync()
            self.assertEqual((new_dir / "숏폼1 (11-03 화)" / "숏폼영상.mp4").read_bytes(), b"v")
            self.assertFalse(ep_dir.exists())  # 비어 버린 예전 회차 폴더는 정리
            self.assertEqual((d / "숏폼영상.mp4").read_bytes(), b"v")  # 링크가 새 이름을 가리킴
            with mock.patch.object(schedule, "uploaded", lambda ep, v: v == "short"):  # 1부만 올림 → '올림'
                deliver.resync()
                self.assertTrue((new_dir / "숏폼1 (11-03 화) 올림" / "숏폼영상.mp4").exists())
                d.unlink()  # 링크가 사라져도 표시 파일로 다시 찾는다
                deliver.resync()
                self.assertEqual((d / "숏폼영상.mp4").read_bytes(), b"v")
            with mock.patch.object(schedule, "uploaded", lambda ep, v: True):  # 다 올림 → '올린 영상' 안으로
                deliver.resync()
                self.assertTrue((od / deliver.UPLOADED / new_dir.name / "롱폼 (11-05 목) 올림").is_dir())
                self.assertFalse(new_dir.exists())
            board = schedule.write_board(date(2026, 11, 4))
            self.assertTrue((od / deliver.BOARD).exists())
            self.assertNotIn(f"→ {p.id}/", board.read_text(encoding="utf-8"))

    def test_order_folders_migrate_flat_layout(self):
        """2026-11-03 한 줄 모양(업로드/'2026-11-04(수) 숏폼1 제목') → 회차 폴더 안으로 옮긴다 (링크·표시 파일로 찾음)."""
        from babyreel import schedule
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "BABYREEL_SCHEDULE": str(Path(tmp) / "s.json"), "BABYREEL_DELIVER_DIR": str(Path(tmp) / "eps"),
                "BABYREEL_ORDER_DIR": str(Path(tmp) / "업로드")}), \
                mock.patch.object(schedule, "titles", lambda ep: {"short": "1부", "short2": "2부", "long": "롱폼 제목"}):
            od = Path(tmp) / "업로드"
            p = load_test_plan(); p.id = "2025-02-01_happy"
            schedule.assign(p, today=date(2026, 11, 4))
            flat = od / "2026-11-04(수) 숏폼1 1부"
            flat.mkdir(parents=True)
            (flat / deliver.MARK).write_text(f"{p.id}/short\n", encoding="utf-8")
            (flat / "숏폼영상.mp4").write_bytes(b"v")
            link = Path(tmp) / "eps" / p.id / "숏폼1"
            link.parent.mkdir(parents=True)
            os.symlink(flat, link)
            self.assertEqual(deliver.resync(), ["폴더: 2026-11-04(수) 숏폼1 1부 → 2026-11-04~05 롱폼 제목/숏폼1 (11-04 수)"])
            self.assertFalse(flat.exists())
            self.assertEqual((link / "숏폼영상.mp4").read_bytes(), b"v")


class TelegramTest(unittest.TestCase):
    """텔레그램 요청 → 메모 저장 → auto.sh 실행 → 결과 회신 (실제 텔레그램·Claude 없이)."""

    def test_parse(self):
        from babyreel.telegram_bot import parse
        self.assertEqual(parse("영상 만들어줘: 둘째 첫 뒤집기, 첫째가 박수"), ("make", "둘째 첫 뒤집기, 첫째가 박수"))
        self.assertEqual(parse("/make 첫째 첫 걸음"), ("make", "첫째 첫 걸음"))
        self.assertEqual(parse("영상좀 만들어 주세요~ 첫째 생일파티"), ("make", "첫째 생일파티"))
        self.assertEqual(parse("오늘")[0], "today")
        self.assertEqual(parse("일정")[0], "schedule")
        self.assertEqual(parse("/start")[0], "help")
        self.assertEqual(parse("안녕")[0], "unknown")

    def _env(self, tmp: Path):
        from babyreel import telegram_bot as tg
        inbox, episodes, data = tmp / "inbox", tmp / "episodes", tmp / "data"
        for d in (inbox, episodes, data, tmp / "logs"):
            d.mkdir()
        return [mock.patch.object(tg, "INBOX", inbox), mock.patch.object(tg, "EPISODES", episodes),
                mock.patch.object(tg, "DATA", data), mock.patch.object(tg, "LOCK", data / "auto.lock"),
                mock.patch.object(tg, "ROOT", tmp), mock.patch.object(tg.time, "sleep", lambda s: None),
                mock.patch.dict(os.environ, {"BABYREEL_SCHEDULE": str(tmp / "s.json"), "BABYREEL_DELIVER_DIR": str(tmp)})]

    def test_make_writes_memo_runs_auto_and_reports(self):
        from contextlib import ExitStack
        from babyreel import telegram_bot as tg
        with tempfile.TemporaryDirectory() as t, ExitStack() as st:
            tmp = Path(t)
            for m in self._env(tmp):
                st.enter_context(m)
            (tmp / "inbox" / "IMG_1.MOV").write_bytes(b"x")
            replies, photos, calls = [], [], []

            def fake_auto(cmd, **kw):
                calls.append(kw["env"])
                memo = next((tmp / "inbox").glob("텔레그램요청_*.txt")).read_text(encoding="utf-8")
                self.assertIn("둘째 첫 뒤집기", memo)
                ep = tmp / "episodes" / "2026-10-17_flip"
                (ep / "out").mkdir(parents=True)
                plan = json.loads((TEST_PLAN / "plan.json").read_text(encoding="utf-8"))
                (ep / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
                (ep / "build.json").write_text(json.dumps({"built_at": datetime.now().isoformat()}), encoding="utf-8")
                for f in ("cover.jpg", "cover2.jpg", "thumbnail.jpg"):
                    (ep / "out" / f).write_bytes(b"jpg")
                (tmp / "logs" / "auto_1.log").write_text("육아일지 No.8 준비: 첫 뒤집기\n", encoding="utf-8")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(tg.subprocess, "run", side_effect=fake_auto), \
                    mock.patch.object(tg, "send_photo", lambda cid, p, cap: photos.append((p.name, cap))):
                tg.run_make("둘째 첫 뒤집기", replies.append, 42)
        self.assertEqual(calls[0]["BABYREEL_REQUEST"], "둘째 첫 뒤집기")
        self.assertEqual(calls[0]["BABYREEL_SOURCE"], "telegram")
        self.assertTrue(any("완성" in r for r in replies), replies)
        self.assertEqual([p for p, _ in photos], ["cover.jpg", "cover2.jpg", "thumbnail.jpg"])
        self.assertIn("쇼츠 2부", photos[1][1])

    def test_make_without_media_does_not_run(self):
        from contextlib import ExitStack
        from babyreel import telegram_bot as tg
        with tempfile.TemporaryDirectory() as t, ExitStack() as st:
            tmp = Path(t)
            for m in self._env(tmp):
                st.enter_context(m)
            replies = []
            with mock.patch.object(tg.subprocess, "run") as run, mock.patch.object(tg, "unused_library", return_value=0):
                tg.run_make("아무거나", replies.append, 42)
            run.assert_not_called()
            self.assertTrue(any("새 영상이 없어요" in r for r in replies), replies)
            self.assertFalse(list((tmp / "inbox").glob("*.txt")))  # 메모도 남기지 않음

    def test_unregistered_chat_ignored(self):
        from babyreel import telegram_bot as tg
        updates = [[{"update_id": 1, "message": {"chat": {"id": 999, "type": "private"}, "text": "영상 만들어줘"}}]]

        def fake_call(method, **kw):
            if method == "getUpdates":
                if updates:
                    return updates.pop()
                raise KeyboardInterrupt
            return {}

        with tempfile.TemporaryDirectory() as t, mock.patch.object(tg, "OFFSET", Path(t) / "o"), \
                mock.patch.object(tg, "load_secret", return_value={"token": "x", "allowed_chat_ids": [42]}), \
                mock.patch.object(tg, "call", side_effect=fake_call), mock.patch.object(tg, "handle") as h:
            with self.assertRaises(KeyboardInterrupt):
                tg.run()
        h.assert_not_called()

    def test_send_without_config_is_noop(self):
        from babyreel import telegram_bot as tg
        with mock.patch.object(tg, "load_secret", return_value={}):
            self.assertFalse(tg.send("hi"))


class PublishDueTest(unittest.TestCase):
    def test_immediate_public_waits_for_date(self):
        """바로 공개되는 업로더(instagram_api)는 올릴 날이 된 영상만 게시."""
        import babyreel.__main__ as cli
        from babyreel.publish import PublishResult
        with tempfile.TemporaryDirectory() as tmp:
            ep = Path(tmp) / "2026-10-17_x"
            ep.mkdir()
            (ep / "plan.json").write_text((TEST_PLAN / "plan.json").read_text(encoding="utf-8"), encoding="utf-8")
            today = date.today()
            meta = {"instagram_reels": {"upload_date": today.isoformat()},
                    "instagram_reels_2": {"upload_date": date.fromordinal(today.toordinal() + 1).isoformat()}}
            (ep / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
            posted = []

            class Fake:
                name = "instagram_api"

                def publish(self, target, m):
                    posted.append(target)
                    return PublishResult(target=target, status="uploaded", url="u")

            with mock.patch.object(cli, "EPISODES", Path(tmp)), \
                    mock.patch("babyreel.publish.publisher_for", lambda t, d, i: Fake()), \
                    mock.patch("babyreel.publish.record"):
                cli.cmd_publish_due(None)
        self.assertEqual(posted, ["instagram_reels"])  # 내일 것은 기다림


class LoudPartsTest(unittest.TestCase):
    def test_quiet_moments_are_not_ducked(self):
        """원본 소리가 조용한 순간엔 BGM 을 낮추지 않는다 (조용한 원본 + BGM 쉼 = 소리 끊김, 2026-10-18)."""
        from babyreel import media
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000)
                frames = bytearray()
                for i in range(8000 * 4):  # 0~1초 소리, 1~3초 무음, 3~4초 소리
                    t = i / 8000
                    v = int(0.2 * math.sin(2 * math.pi * 440 * t) * 32767) if t < 1 or t >= 3 else 0
                    frames += struct.pack("<h", v)
                w.writeframes(bytes(frames))
            parts = media.loud_parts(path, 0, 4)
        self.assertEqual(len(parts), 2)
        self.assertAlmostEqual(parts[0][1], 1.0, delta=0.26)
        self.assertAlmostEqual(parts[1][0], 3.0, delta=0.26)


class DuplicateIngestTest(unittest.TestCase):
    def test_copies_are_set_aside(self):
        from babyreel import media
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(media, "LIBRARY", Path(tmp) / "lib"):
            a, b, c = Path(tmp) / "IMG_1.MOV", Path(tmp) / "IMG_1 2.MOV", Path(tmp) / "IMG_2.MOV"
            a.write_bytes(b"same" * 1000)
            b.write_bytes(b"same" * 1000)
            c.write_bytes(b"diff" * 1000)
            kept = media._drop_duplicates([b, a, c])
            self.assertEqual([k.name for k in kept], ["IMG_1.MOV", "IMG_2.MOV"])
            self.assertFalse(b.exists())
            self.assertEqual(len(list((Path(tmp) / "lib" / "_duplicates").iterdir())), 1)  # 지우지 않고 옮김


class RestraintTest(unittest.TestCase):
    """과하지 않게 재미있게 (docs/STYLE.md 11번)."""

    def _clips(self, **kw):
        from babyreel.plan import Caption, Clip, Punch, Sfx, Sticker
        c = Clip(src="x.mp4", start=0, end=10, captions=[Caption(at=0.2, text="기록 결과, 오늘도 평화로움", dur=3.0)])
        for k, v in kw.items():
            setattr(c, k, v)
        return [c], (Caption, Punch, Sfx, Sticker)

    def test_budget_and_stacking(self):
        from babyreel.plan import restraint_report
        clips, (Caption, Punch, Sfx, Sticker) = self._clips()
        c = clips[0]
        c.stickers = [Sticker(at=t, emoji="😳") for t in (1, 2, 3, 4)]
        c.sfx = [Sfx(at=t, name="pop") for t in (1, 2, 3, 4, 5)]
        c.punch = [Punch(at=1.0, dur=1.0, scale=1.4)]
        c.captions.append(Caption(at=1.0, text="(정적)", style="big", dur=1.0))
        out = " ".join(restraint_report(clips))
        self.assertIn("스티커 4개 > 2개", out)
        self.assertIn("효과음 5개 > 3개", out)
        self.assertIn("한꺼번에", out)
        self.assertIn("1.4배", out)

    def test_calm_clip_passes(self):
        from babyreel.plan import restraint_report
        clips, (Caption, Punch, Sfx, Sticker) = self._clips()
        clips[0].captions.append(Caption(at=5.0, text="(씨익)", style="big", dur=1.5))
        clips[0].punch = [Punch(at=5.0, dur=1.0, scale=1.2)]
        self.assertEqual(restraint_report(clips), [])

    def test_reading_time_and_punchline_hold(self):
        from babyreel.plan import restraint_report
        clips, (Caption, Punch, Sfx, Sticker) = self._clips()
        clips[0].captions = [Caption(at=0.0, text="기록 결과 자는 표정은 진지함 그 자체였음", dur=1.0),
                             Caption(at=9.5, text="(정적)", style="big", dur=0.5)]
        out = " ".join(restraint_report(clips))
        self.assertIn("빨리 사라짐", out)
        self.assertIn("여운", out)

    def test_gendered_word_is_error(self):
        p = load_test_plan()
        p.clips[0].captions[0].text = "우리 공주 일어났어요"
        self.assertTrue(any("성별 호칭" in e for e in validate(p).errors))
        p = load_test_plan()
        p.clips[0].captions[0].text = "오빠가 괴롭힌다"  # 엄마가 아빠를 부르는 말은 허용
        self.assertFalse(any("성별 호칭" in e for e in validate(p).errors))


class PlacementTest(unittest.TestCase):
    def test_sticker_avoids_face_and_captions(self):
        from babyreel.build import Box, sticker_spot
        box = Box(0, 490, 1080, 1180)
        face = (600, 100, 1000, 500)  # 오른쪽 위 얼굴 (미디어 칸 좌표)
        x, y = sticker_spot(0.82, 0.2, 150, box, face)
        self.assertLess(x, 540)  # 왼쪽으로 옮김
        x2, y2 = sticker_spot(0.82, 0.2, 150, box, face, busy=[(0, 600, 400, 800)])
        self.assertFalse(0 < x2 < 400 and 600 < y2 < 800)
        self.assertEqual(sticker_spot(0.82, 0.2, 150, box, None), (0.82 * 1080, 490 + 0.2 * 1180))

    def test_face_tracking_and_exclusion(self):
        from babyreel import faces
        found = {"faces": [(0.6, 0.0, 0.8, 0.2), (0.3, 0.4, 0.45, 0.5)], "texts": [], "frames": [
            {"t": 0.0, "luma": 0.2, "faces": [[0.6, 0.0, 0.8, 0.2], [0.3, 0.4, 0.45, 0.5]]},
            {"t": 1.0, "luma": 0.3, "faces": [[0.62, 0.02, 0.82, 0.22], [0.3, 0.4, 0.45, 0.5]]},
            {"t": 2.0, "luma": 0.4, "faces": [[0.3, 0.4, 0.45, 0.5]]}]}
        tr = faces.track_near(found, (0.7, 0.1))
        self.assertEqual(len(tr), 3)  # 못 찾은 프레임도 직전 위치로 계속 가림
        self.assertAlmostEqual(tr[1][1][0], 0.62)
        self.assertEqual(tr[2][1], tr[1][1])
        kept = faces.without(found, [(0.7, 0.1)])
        self.assertEqual(kept["faces"], [(0.3, 0.4, 0.45, 0.5)])  # 블러할 얼굴은 보호 대상에서 제외
        self.assertAlmostEqual(faces.mean_luma(found), 0.3)


class SpeechTest(unittest.TestCase):
    def test_speech_lines_flags_hallucinations(self):
        from babyreel import transcribe as stt
        segs = {"segments": [{"start": 0.0, "end": 1.0, "text": "힘줘 힘줘!"},
                             {"start": 1.0, "end": 1.5, "text": "이렇게"}, {"start": 1.5, "end": 2.0, "text": "이렇게"},
                             {"start": 2.0, "end": 2.5, "text": "이렇게"},
                             {"start": 3.0, "end": 3.5, "text": "employment local"},
                             {"start": 99.0, "end": 100.0, "text": "영상 밖"}]}
        levels = {0.0: -20.0}
        with mock.patch.object(stt, "transcribe", return_value=segs), \
                mock.patch("babyreel.media.probe", return_value={"duration_s": 10.0}), \
                mock.patch("babyreel.media.audio_level", side_effect=lambda v, a, b: levels.get(a, -30.0)):
            out = stt.speech_lines(Path("x.mov"))
        real = [x["text"] for x in out if x["real"]]
        self.assertEqual(real, ["힘줘 힘줘!"])
        why = {x["text"]: x["why"] for x in out}
        self.assertIn("반복", why["이렇게"])
        self.assertIn("한글 아님", why["employment local"])
        self.assertIn("영상 밖", why["영상 밖"])

    def test_mute_ranges_validated(self):
        p = load_test_plan()
        p.clips[0].mute_ranges = [[0.6, 1.2]]
        self.assertFalse(any("mute_ranges" in e for e in validate(p).errors))
        p.clips[0].mute_ranges = [[51.0, 50.0]]  # 시작 > 끝
        self.assertTrue(any("mute_ranges" in e for e in validate(p).errors))


class InsightsTest(unittest.TestCase):
    """YouTube 성과 수집 (가짜 API 응답으로)."""

    def _fake(self):
        class Call:
            def __init__(self, data): self.data = data
            def execute(self): return self.data

        class R:
            def __init__(self, fn): self.fn = fn
            def list(self, **kw): return Call(self.fn(**kw))
            def query(self, **kw): return Call(self.fn(**kw))

        vids = {
            "S1": ("샘플 쇼츠 반응ㅋㅋ", "PT20S", "public", 1000),
            "L1": ("공을 처음 본 둘째 몰아보기 | 육아일지 No.7", "PT3M", "private", 0),
            "S2": ("공 쟁탈전 2라운드ㅋㅋ", "PT15S", "public", 500),
        }

        class YT:
            def channels(self):
                return R(lambda **kw: {"items": [{"id": "C", "snippet": {"title": "<채널 이름>"},
                                                  "contentDetails": {"relatedPlaylists": {"uploads": "U"}},
                                                  "statistics": {"subscriberCount": "12"}}]})
            def playlistItems(self):
                return R(lambda **kw: {"items": [{"contentDetails": {"videoId": k}} for k in vids]})
            def videos(self):
                return R(lambda **kw: {"items": [{"id": k, "snippet": {"title": t, "publishedAt": "2026-10-10T00:00:00Z"},
                                                  "contentDetails": {"duration": d},
                                                  "status": {"privacyStatus": pv, "madeForKids": False},
                                                  "statistics": {"viewCount": str(n)}}
                                                 for k, (t, d, pv, n) in vids.items() if k in kw["id"].split(",")]})

        class YTA:
            def reports(self):
                def q(**kw):
                    if kw.get("dimensions") == "video":
                        return {"columnHeaders": [{"name": n} for n in ("video", "views", "estimatedMinutesWatched",
                                "averageViewDuration", "averageViewPercentage", "likes", "shares", "subscribersGained")],
                                "rows": [["S1", 1000, 300, 18, 90, 50, 5, 3], ["S2", 500, 100, 12, 80, 10, 1, 0],
                                         ["L1", 0, 0, 0, 0, 0, 0, 0]]}
                    return {"columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}],
                            "rows": [[0.0, 1.0], [0.25, 0.95], [0.5, 0.6], [0.75, 0.55], [1.0, 0.5]]}
                return R(q)
        return YT(), YTA()

    def test_collect_end_to_end(self):
        from babyreel import insights, schedule
        yt, yta = self._fake()
        ours = [{"episode": "_unit", "target": "youtube_shorts", "video": "short", "title": "샘플 쇼츠 반응ㅋㅋ",
                 "upload_date": "2026-10-10", "duration": 20.0,
                 "segments": [{"clip_index": 0, "start": 0, "end": 8}, {"clip_index": 1, "start": 8, "end": 20}]},
                {"episode": "_unit", "target": "youtube_long", "video": "long", "title": "샘플 롱폼 몰아보기",
                 "upload_date": "2026-10-10", "duration": 180.0, "segments": []}]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(insights, "services", return_value=(yt, yta)), \
                mock.patch.object(insights, "our_videos", return_value=ours), \
                mock.patch.object(insights, "record_links", return_value=0), \
                mock.patch.object(insights, "OUT_DIR", Path(tmp)), mock.patch.object(insights, "DOC", Path(tmp) / "I.md"), \
                mock.patch.object(insights, "_features", return_value={"talk": True, "cut": 8.0, "caption": "기록 시작"}):
            with mock.patch.object(insights, "mode", return_value="oauth"):
                r = insights.collect(today=date(2026, 10, 19), interactive=True)
            doc = (Path(tmp) / "I.md").read_text(encoding="utf-8")
        self.assertEqual(r["matched"], 2)
        self.assertTrue(any("private" in a and "공개" in a for a in r["alerts"]))  # 롱폼 비공개 → 시청 시간 0 경고
        self.assertEqual(r["ypp"]["long_public_hours"], 0)  # 공개 롱폼이 없으므로 0
        self.assertEqual(r["ypp"]["shorts_public_views_90d"], 1500)
        s1 = next(v for v in r["videos"] if v["target"] == "youtube_shorts")
        self.assertEqual(s1["views"], 1000)
        self.assertAlmostEqual(s1["drops"][0]["t"], 5.0)  # 0.25~0.5 구간(5초)에서 가장 많이 빠짐
        self.assertIn("수익 창출", doc)
        self.assertIn("집계", doc)

    def test_policy_alerts(self):
        from babyreel import insights
        vids = [{"title": "a", "upload_status": "rejected", "rejection": "copyright"},
                {"title": "b", "upload_status": "processed", "region_blocked": ["DE"]},
                {"title": "c", "upload_status": "processed", "age_restricted": True},
                {"title": "d", "upload_status": "processed"}]
        out = " ".join(insights.policy_alerts(vids))
        self.assertIn("저작권", out)
        self.assertIn("차단", out)
        self.assertIn("연령 제한", out)
        self.assertNotIn("'d'", out)

    def test_auto_run_never_opens_browser(self):
        from babyreel import insights
        with mock.patch.object(insights, "mode", return_value="oauth"), \
                mock.patch.object(insights, "TOKEN", Path("/nonexistent/token.json")), \
                mock.patch.object(insights, "API_KEY", Path("/nonexistent/key.txt")), \
                mock.patch.object(insights, "services") as svc:
            with self.assertRaises(RuntimeError):
                insights.collect(today=date(2026, 10, 19), interactive=False)
            svc.assert_not_called()

    def test_title_match_tolerates_small_edits(self):
        from babyreel import insights
        ours = [{"title": "샘플 쇼츠 웃음 터짐ㅋㅋ (첫 웃음)", "video": "short"},
                {"title": "샘플 롱폼 성장 기록 일주일 | 육아일지 No.5", "video": "long"}]
        videos = [{"id": "a", "title": "샘플 쇼츠 웃음 터짐ㅋㅋ", "duration": 20},
                  {"id": "b", "title": "샘플 롱폼 성장 기록 일주일", "duration": 253}]
        pairs = insights.match(ours, videos)
        self.assertEqual([(o["video"], v["id"]) for o, v in pairs], [("short", "a"), ("long", "b")])


class ComplianceTest(unittest.TestCase):
    def test_limits(self):
        from babyreel import compliance
        ok = {"youtube_shorts": {"title": "첫째", "description": "기록 #a #b", "tags": ["육아"], "made_for_kids": False}}
        self.assertEqual(compliance.check(ok, {"outputs": {"short": {"duration": 30}}}), ([], []))
        bad = {"youtube_shorts": {"title": "가" * 101, "description": "<b> " + " ".join(f"#t{i}" for i in range(61)),
                                  "tags": ["x" * 501], "made_for_kids": True}}
        e, w = compliance.check(bad, {"outputs": {"short": {"duration": 200}}})
        text = " ".join(e)
        for key in ("제목", "'<'", "해시태그", "태그 합계", "쇼츠"):
            self.assertIn(key, text)
        self.assertTrue(any("아동용" in x for x in w))

    def test_existing_episodes_pass(self):
        import json as _j
        from babyreel import compliance
        for d in sorted((ROOT / "episodes").glob("2024-*/metadata.json")):
            meta = _j.loads(d.read_text(encoding="utf-8"))
            man = _j.loads((d.parent / "build.json").read_text(encoding="utf-8"))
            self.assertEqual(compliance.check(meta, man)[0], [], d.parent.name)


class KoreanReadingTest(unittest.TestCase):
    """TTS 숫자 읽기: 고유어(하나·둘·셋)/한자어(일·이·삼) 구분 (docs/FEEDBACK.md 2026-10-20)."""

    def test_counters(self):
        from babyreel.kotext import spoken
        cases = {
            "3번 울고 2시간 잤음": "세 번 울고 두 시간 잤음",
            "육아일지 7번": "육아일지 칠 번",
            "No.7": "칠 번",
            "1번째 웃음, 2번째": "첫 번째 웃음, 두 번째",
            "오후 2시 30분": "오후 두 시 삼십 분",
            "14:02 기록 시작": "열네 시 이 분 기록 시작",
            "12월 14일": "십이월 십사 일",
            "6월, 10월": "유월, 시월",
            "5주째": "오 주째",
            "3개월 아기": "삼 개월 아기",
            "20살, 21개, 100명": "스무 살, 스물한 개, 백 명",
            "체중 4.9kg": "체중 사 점 구 킬로그램",
            "분유 160ml, 2병": "분유 백육십 밀리리터, 두 병",
            "D+61": "디 플러스 육십일",
            "50%": "오십 퍼센트",
            "120까지는": "백이십까지는",
            "2024년": "이천이십사 년",
            "아기 2명과 고양이 1마리": "아기 두 명과 고양이 한 마리",
            "재우는 사람이 먼저 잠듦.": "재우는 사람이 먼저 잠듬.",
            "특이사항 없음": "특이사항 없음",  # 뒤에 음절이 이어지면 그대로 ([업씀])
            "할 말이 없.": "할 말이 업.",
            "닭, 여덟 개": "닥, 여덜 개",
            "닭이 운다": "닭이 운다",
        }
        for src, want in cases.items():
            self.assertEqual(spoken(src), want, src)

    def test_symbols(self):
        from babyreel.kotext import spoken
        self.assertEqual(spoken("생일 축하해~ ㅋㅋㅋ"), "생일 축하해")
        self.assertEqual(spoken("그 웃음… 기록은\n계속된다"), "그 웃음, 기록은 계속된다")
        self.assertEqual(spoken("D+61 · 기상"), "디 플러스 육십일, 기상")
        self.assertEqual(spoken("엄마 아빠도… 아직"), "엄마 아빠도, 아직")

    def test_numbers(self):
        from babyreel.kotext import native, sino
        self.assertEqual([sino(n) for n in (0, 10, 11, 105, 1000, 10000, 23456)],
                         ["영", "십", "십일", "백오", "천", "만", "이만삼천사백오십육"])
        self.assertEqual([native(n) for n in (1, 2, 3, 4, 20, 21, 99)], ["한", "두", "세", "네", "스무", "스물한", "아흔아홉"])
        self.assertEqual(native(3, prenoun=False), "셋")


class VoiceCloneTest(unittest.TestCase):
    """아빠 목소리: 본인만 복제 허용, 녹음 없으면 기본 음성으로, 받아쓰기 확인."""

    def test_only_owner_can_be_cloned(self):
        from babyreel import voice
        with mock.patch.object(config, "load", return_value={"voices": {"엄마": {"voice": "F1", "clone": "엄마"}}}):
            with self.assertRaises(ValueError):
                voice.voice_config("엄마")
        with self.assertRaises(ValueError):
            voice.enroll(Path("/nonexistent.m4a"), "엄마")

    def test_fallback_without_recording(self):
        from babyreel import voice
        with tempfile.TemporaryDirectory() as d, mock.patch.object(voice, "VOICES", Path(d)):
            self.assertIsNone(voice.reference("아빠"))
            self.assertFalse(voice.clone_ready("아빠")[0])
            self.assertEqual(voice.resolve_engine({"voice": "M1", "clone": "아빠"}), voice.engine())
            (Path(d) / "아빠").mkdir()
            (Path(d) / "아빠" / "ref.wav").write_bytes(b"x")
            (Path(d) / "아빠" / "ref.txt").write_text("안녕하세요", encoding="utf-8")
            self.assertIsNotNone(voice.reference("아빠"))
            with mock.patch.object(voice, "VENV", Path(d) / "nope"):
                self.assertFalse(voice.clone_ready("아빠")[0])  # 가상환경이 없으면 기본 음성

    def test_cer_and_names(self):
        from babyreel import voice
        self.assertEqual(voice.cer("육아일지 7번", "육아일지 7번"), 0.0)
        self.assertEqual(voice.cer("세 번 울고", "3번 울고"), 0.0)  # 받아쓰기 숫자도 같은 규칙으로 비교
        self.assertGreater(voice.cer("샘플 문장, 길이가 꽤 긴 편", "샘플 문장"), 0.5)
        self.assertEqual(voice.fix_names("에이는 웃었다. 해 보니까"), "아이A는 웃었다. 해 보니까")

    def test_retry_until_clear(self):
        """받아쓰기 오류율이 높으면 다시 만들고, 가장 나은 것을 쓴다."""
        from babyreel import voice
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "x.wav"
            made = []

            def fake_once(text, vc, path, seed):
                made.append(seed)
                path.write_bytes(bytes([seed]))

            errs = {0: 0.5, 1: 0.1, 2: 0.0}
            with mock.patch.object(voice, "_clone_once", fake_once), mock.patch.object(voice, "tighten"), \
                    mock.patch.object(voice, "check", lambda p, t: errs[p.read_bytes()[0]]):
                voice._clone("문장", {"clone": "아빠"}, out)
            self.assertEqual(made, [0, 1])  # 두 번째에 통과하면 멈춘다
            self.assertEqual(out.read_bytes(), bytes([1]))
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["x.wav"])  # 시도 파일 정리


    def test_style_attempts(self):
        """육아일지 문체 끝('~함/~음')이면 문장 끝 부호를 바꾼 후보를 만든다."""
        from babyreel import voice
        att = [x for x, _ in voice.clone_attempts("기록 결과, 공을 발견함")]
        self.assertEqual(att[:3], ["기록 결과, 공을 발견함.", "기록 결과, 공을 발견함,", "기록 결과, 공을 발견함…"])
        self.assertEqual({x for x, _ in voice.clone_attempts("크리스마스를 맞이하는 첫째")}, {"크리스마스를 맞이하는 첫째."})
        self.assertTrue(voice.POLITE_END.search("잤습니다."))
        self.assertFalse(voice.POLITE_END.search("잤음."))


    def test_sentence_end(self):
        from babyreel import voice
        self.assertEqual(voice.sentence_end("기록은 계속된다"), "기록은 계속된다.")
        self.assertEqual(voice.sentence_end("아직 적응 중"), "아직 적응 중.")
        self.assertEqual(voice.sentence_end("잘 잤어?"), "잘 잤어?")
        self.assertEqual(voice.sentence_end("기록 완료."), "기록 완료.")

    def test_tighten_trims_tail_and_pauses(self):
        """끝음 잔향은 짧게, 문장 사이 긴 쉼은 0.25초로 (끝음을 끌지 않게, 2026-10-20)."""
        from babyreel import voice
        sr = 24000

        def tone(sec, amp):
            return [int(amp * math.sin(2 * math.pi * 180 * i / sr)) for i in range(int(sr * sec))]

        # 말 1초 · 쉼 0.8초 · 말 1초 · 잔향(작게) 0.6초 · 무음 0.5초
        samples = tone(1, 12000) + [0] * int(sr * 0.8) + tone(1, 12000) + tone(0.6, 120) + [0] * int(sr * 0.5)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v.wav"
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
                w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
            info = voice.tighten(p)
            self.assertAlmostEqual(info["pauses_cut"], 0.55, delta=0.05)
            # 2초 말 + 0.25초 쉼 + 끝 0.07초 + 여백 0.05초 ≈ 2.37초 (원래 3.9초)
            self.assertAlmostEqual(info["after"], 2.37, delta=0.08)
            with wave.open(str(p)) as w:
                a = struct.unpack(f"<{w.getnframes()}h", w.readframes(w.getnframes()))
            self.assertLess(max(abs(x) for x in a[-int(sr * 0.06):]), 200)  # 끝은 조용히 닫힘

    def test_own_clone_is_not_flagged(self):
        """아빠 대사를 아빠 본인 목소리 복제로 읽으면 '변경된 콘텐츠' 경고 대상이 아니다 (YouTube 예외)."""
        from babyreel import compliance, voice
        plan = load_test_plan()
        cap = next(c for clip in plan.clips for c in clip.captions)
        cap.style, cap.speaker, cap.voice = "talk", "아빠", True
        with mock.patch.object(voice, "resolve_engine", return_value="clone"):
            self.assertFalse(any("변경된 콘텐츠" in w for w in compliance.check({}, None, plan)[1]))
        with mock.patch.object(voice, "resolve_engine", return_value="supertonic"):
            self.assertTrue(any("변경된 콘텐츠" in w for w in compliance.check({}, None, plan)[1]))


class AgeRangeTest(unittest.TestCase):
    """영상 아래 나이: 여러 날 찍은 영상은 기간으로 (D+66 하나로 적으면 D+61 장면과 안 맞음)."""

    def test_range(self):
        kids = [{"nick": "첫째", "birth_date": "2014-05-02"}]
        with mock.patch.object(config, "children", return_value=kids):
            self.assertEqual(config.ages(date(2014, 7, 4), since=date(2014, 6, 28)), "첫째 D+57~63")
            self.assertEqual(config.ages(date(2014, 7, 7)), "첫째 D+66")
            self.assertEqual(config.ages(date(2014, 7, 7), since=date(2014, 7, 7)), "첫째 D+66")
            self.assertEqual(config.ages(date(2014, 10, 10), since=date(2014, 9, 7)), "첫째 4~5개월")
            self.assertEqual(config.ages(date(2014, 8, 22), since=date(2014, 8, 2)), "첫째 D+92~3개월")
        self.assertEqual(config.shot_date("library/2024-12/2024-12-10_IMG_7225.MOV"), date(2024, 12, 10))
        self.assertIsNone(config.shot_date("tests/footage/test_portrait_01.mp4"))


class TelegramVoiceTest(unittest.TestCase):
    """텔레그램 음성 메시지 → 아빠 목소리 등록 → 샘플 회신."""

    def test_voice_message_enrolls(self):
        from babyreel import telegram_bot as tb, voice
        sent, files = [], []
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "s.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\0\0" * 24000)
            with mock.patch.object(tb, "send", lambda t, ids=None: sent.append(t)), \
                    mock.patch.object(tb, "download", return_value=Path(d) / "v.ogg") as dl, \
                    mock.patch.object(tb, "send_file", lambda cid, path, **kw: files.append(path.suffix)), \
                    mock.patch.object(voice, "enroll", return_value={"seconds": 9.5, "text": "안녕하세요",
                                                                      "level_db": -22.0, "noise_db": -70.0}), \
                    mock.patch.object(voice, "clone_ready", return_value=(True, "ok")), \
                    mock.patch.object(voice, "speak", return_value=(wav, {"engine": "clone"})):
                tb.handle({"chat": {"id": 1}, "voice": {"file_id": "abc"}})
            dl.assert_called_once()
        self.assertTrue(any("등록 완료" in x for x in sent), sent)
        self.assertEqual(files, [".m4a"])  # 휴대폰에서 바로 듣는 샘플

    def test_help_has_script(self):
        from babyreel import telegram_bot as tb, voice
        self.assertIn(voice.SCRIPT, tb.help_text())
        self.assertNotIn("{script}", tb.help_text())


class MusicVarietyTest(unittest.TestCase):
    """BGM 다양화: 장면마다 곡 바꾸기·검증·추천·음량 맞춤 (2026-10-21)."""

    def clips(self, *specs):
        from babyreel.plan import Clip
        return [Clip(src="x.mp4", start=0, end=d, music=m) for d, m in specs]

    def test_sections(self):
        from babyreel.plan import music_sections
        secs = music_sections(self.clips((5, None), (4, "quirky_dog"), (6, None), (3, "off"), (2, "quirky_dog")), "merry_go")
        self.assertEqual(secs, [(0.0, 5.0, "merry_go"), (5.0, 15.0, "quirky_dog"), (15.0, 18.0, None),
                                (18.0, 20.0, "quirky_dog")])
        # 같은 곡을 다시 지정하면 전환 아님, 맨 앞 클립에서 바꾸면 처음부터 그 곡
        self.assertEqual(music_sections(self.clips((5, "merry_go"), (5, None)), "merry_go"), [(0.0, 10.0, "merry_go")])
        self.assertEqual(music_sections(self.clips((5, "jingle_bells"), (5, None)), "merry_go"),
                         [(0.0, 10.0, "jingle_bells")])

    def test_report(self):
        from babyreel.plan import Music, music_report
        base = Music(track="merry_go")
        err, warn = music_report(self.clips((10, None), (10, "no_such_track")), base, "short", [])
        self.assertTrue(any("카탈로그에 없습니다" in e for e in err))
        err, warn = music_report(self.clips((3, None), (2, "quirky_dog"), (2, "jingle_bells"), (10, None)), base, "short", [])
        self.assertTrue(any("전환 2번" in w for w in warn), warn)
        self.assertTrue(any("짧습니다" in w for w in warn), warn)
        _, warn = music_report(self.clips((60, None), (60, None)), base, "long", [])
        self.assertTrue(any("BGM 한 곡" in w for w in warn), warn)
        _, warn = music_report(self.clips((60, None), (60, "heartwarming")), base, "long", [])
        self.assertFalse(any("BGM 한 곡" in w for w in warn), warn)
        _, warn = music_report(self.clips((20, None)), base, "short", ["merry_go", "carefree"])
        self.assertTrue(any("최근 편" in w for w in warn), warn)

    def test_catalog_and_suggest(self):
        from babyreel import music
        cat = music.load_catalog()
        self.assertGreaterEqual(len(cat), 35)
        for e in cat:  # 모든 곡: CC BY + 크레딧 + 상황 태그 + 인스타 허용
            self.assertIn("CC BY 4.0", e["license"])
            self.assertIn("Kevin MacLeod", e["attribution"])
            self.assertTrue(e.get("situations"), e["id"])
            self.assertTrue(set(e["situations"]) <= set(music.SITUATIONS), e["id"])
            self.assertIn("instagram", e["platforms"])
        for tag in music.SITUATIONS:
            self.assertTrue(any(tag in e["situations"] for e in cat), tag)
        with mock.patch.object(music, "abs_path", lambda f: Path(__file__)):  # 파일이 있다고 가정
            picks = [e["id"] for e in music.suggest("크리스마스", avoid=["jingle_bells"], n=10)]
            self.assertIn("jingle_bells", picks)
            self.assertNotEqual(picks[0], "jingle_bells")  # 최근에 쓴 곡은 뒤로
            like = [e["id"] for e in music.suggest_like("merry_go", ["wholesome"])]
            self.assertNotIn("merry_go", like)
            self.assertNotIn("wholesome", like)

    def test_trim_bounds(self):
        from babyreel import music
        with mock.patch.object(music, "loudness", return_value=-33.0):
            self.assertEqual(music.trim_db(Path("a.mp3")), music.MAX_TRIM_DB)
        with mock.patch.object(music, "loudness", return_value=-9.0):
            self.assertEqual(music.trim_db(Path("a.mp3")), -music.MAX_TRIM_DB)
        with mock.patch.object(music, "loudness", return_value=-22.0):
            self.assertAlmostEqual(music.trim_db(Path("a.mp3")), 2.0)

    def test_recent_tracks(self):
        from babyreel import music
        with tempfile.TemporaryDirectory() as d:
            for name, date_, track, sw in (("a", "2024-12-01", "merry_go", "jingle_bells"), ("b", "2024-12-10", "carefree", None),
                                           ("_test", "2099-01-01", "hyperfun", None)):
                (Path(d) / name).mkdir()
                clips = [{"src": "x", "music": sw}] if sw else []
                (Path(d) / name / "plan.json").write_text(json.dumps({"date": date_, "music": {"track": track}, "clips": clips}))
            with mock.patch("babyreel.config.EPISODES", Path(d)):
                self.assertEqual(music.recent_tracks(), ["carefree", "merry_go", "jingle_bells"])
                self.assertEqual(music.recent_tracks(before="2024-12-05"), ["merry_go", "jingle_bells"])

    def test_credits_list_all_tracks(self):
        from babyreel import metadata
        plan = load_test_plan()
        o = {"file": "out/x.mp4", "duration": 20.0, "segments": [], "cover": "out/cover.jpg"}
        man = {"outputs": {"short": {**o, "music_all": [{"attribution": "A"}, {"attribution": "B"}]},
                           "long": dict(o)}, "music": {"attribution": "A"}, "thumbnail": "out/t.jpg"}
        meta = metadata.build_metadata(plan, man)
        self.assertIn("🎵 Music: A\n🎵 Music: B", meta["youtube_shorts"]["description"])


class ThumbnailTest(unittest.TestCase):
    """썸네일 (2026-10-21): 기획 점검·QA 검사·실제 렌더링 (얼굴 위 글자 금지, 글자 맞춤, 규격)."""

    def test_plan_checks(self):
        from babyreel.plan import thumbnail_report
        plan = load_test_plan()
        plan.thumbnail.text = []
        self.assertTrue(any("비어 제목" in w for w in thumbnail_report(plan)))
        plan.thumbnail.text = ["아주아주아주긴글자입니다", "둘"]
        self.assertTrue(any("줄당" in w for w in thumbnail_report(plan)))
        plan.thumbnail.text = list(plan.title_lines)
        self.assertTrue(any("제목과 똑같" in w for w in thumbnail_report(plan)))
        plan.thumbnail.text = ["모델이", "잠듦ㅋㅋ"]
        plan.clips[plan.thumbnail.clip].people = ["첫째", "아빠"]
        self.assertTrue(any("thumbnail.faces" in w for w in thumbnail_report(plan)))
        plan.thumbnail.faces = [[0.5, 0.4]]
        self.assertFalse(any("thumbnail.faces" in w for w in thumbnail_report(plan)))

    def write_report(self, d, **kw):
        from babyreel import thumbs
        img = Path(d) / "thumbnail.jpg"
        subprocess_ok = os.system(f"sips -s format jpeg -z 720 1280 /System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/GenericDocumentIcon.icns --out {img} >/dev/null 2>&1")
        if subprocess_ok != 0 or not img.exists():
            self.skipTest("sips 없음")
        rep = {"width": 1280, "height": 720, "fit": True, "face_found": True, "face": [700, 150, 1050, 560],
               "face_quality": 0.6, "eyes": 0.25, "text": [[60, 250, 600, 360]], "emoji": [0, 0, 0, 0], "font_size": 120}
        rep.update(kw)
        thumbs.report_path(img).write_text(json.dumps(rep))
        return img

    def test_problems(self):
        from babyreel import thumbs
        with tempfile.TemporaryDirectory() as d:
            e, w = thumbs.problems(self.write_report(d))
            self.assertEqual(e, [], e)
            e, _ = thumbs.problems(self.write_report(d, text=[[650, 200, 1000, 300]]))
            self.assertTrue(any("얼굴을 가립니다" in x for x in e), e)
            e, _ = thumbs.problems(self.write_report(d, fit=False))
            self.assertTrue(any("자리가 모자랍니다" in x for x in e), e)
            e, _ = thumbs.problems(self.write_report(d, face_found=False))
            self.assertTrue(any("얼굴을 못 찾은" in x for x in e), e)
            _, w = thumbs.problems(self.write_report(d, face=[900, 300, 980, 380]))
            self.assertTrue(any("얼굴이 작습니다" in x for x in w), w)
            e, _ = thumbs.problems(self.write_report(d, face=[-20, 150, 330, 560]))  # 6% 넘침: 넉넉한 상자 → 통과
            self.assertFalse(any("잘립니다" in x for x in e), e)
            e, _ = thumbs.problems(self.write_report(d, face=[-120, 150, 230, 560]))  # 34% 넘침 → 잘림
            self.assertTrue(any("얼굴이 화면 밖" in x for x in e), e)
            e, _ = thumbs.problems(self.write_report(d, face_source=[-0.2, 0.3, 0.3, 0.6]))
            self.assertTrue(any("원본에서" in x for x in e), e)
            e, _ = thumbs.problems(self.write_report(d, text=[[-20, 250, 600, 360]]))
            self.assertTrue(any("잘립니다" in x for x in e), e)

    def test_variants_and_refs(self):
        from babyreel import thumbs
        plan = load_test_plan()
        with tempfile.TemporaryDirectory() as d:
            a = thumbs.spec_for(plan, "long", Path(d) / "thumbnail.jpg", "a")
            b = thumbs.spec_for(plan, "long", Path(d) / "thumbnail_b.jpg", "b")
            c = thumbs.spec_for(plan, "long", Path(d) / "thumbnail_c.jpg", "c")
            cover = thumbs.spec_for(plan, "cover", Path(d) / "cover.jpg")
        self.assertEqual((a["mirror"], b["mirror"], c["pick"]), (False, True, 1))
        self.assertEqual((cover["W"], cover["H"], a["W"], a["H"]), (1080, 1920, 1280, 720))
        self.assertLessEqual(len(a["lines"]), 2)
        self.assertTrue(a["blurOthers"])
        self.assertTrue(Path(a["font"]).exists())

    @unittest.skipUnless(sys.platform == "darwin", "macOS 전용 (Vision·Core Text)")
    def test_render_real(self):
        """실제 테스트 영상으로 렌더링: 규격·글자 맞춤·얼굴 겹침 없음, 긴 글자는 '자리 모자람'으로 잡힘."""
        from babyreel import thumbs
        plan = load_test_plan()
        plan.thumbnail.text = ["처음 본 공", "반응ㅋㅋ"]
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "thumbnail.jpg"
            rep = thumbs.render(plan, "long", out)
            self.assertTrue(out.exists())
            e, _ = thumbs.problems(out)
            self.assertEqual([x for x in e if "얼굴을 못 찾은" not in x], [], e)  # 테스트 영상에 얼굴이 없을 수 있다
            self.assertTrue(rep["fit"])
            self.assertGreaterEqual(rep["font_size"], thumbs.LONG["minFont"])
            plan.thumbnail.text = ["이건정말로아주아주아주아주긴썸네일문구", "이것도아주아주아주아주긴두번째줄"]
            rep = thumbs.render(plan, "long", out)
            self.assertFalse(rep["fit"])
            self.assertTrue(any("자리가 모자랍니다" in x for x in thumbs.problems(out)[0]))


class CoverTest(unittest.TestCase):
    """가림 스티커(목욕 장면 등): 계획 파싱·검증·목욕 경고 (2026-10-22)."""

    def test_parse_and_validate(self):
        from babyreel.plan import Cover, validate
        plan = load_test_plan()
        c = plan.clips[0]
        c.covers = [Cover(emoji="🐥", size=0.3, points=[[c.start, 0.5, 0.8], [c.end or c.start + 1, 0.6, 0.8]])]
        raw = asdict(plan)
        again = Plan.from_dict(raw)
        self.assertEqual(again.clips[0].covers[0].points[1][1], 0.6)
        self.assertFalse(any("covers" in e for e in validate(plan).errors))
        c.covers = [Cover(points=[[0.0, 1.4, 0.5]])]
        self.assertTrue(any("covers[0].points" in e for e in validate(plan).errors))
        c.covers = [Cover(emoji="오리", points=[[0.0, 0.5, 0.5]])]
        self.assertTrue(any("이모지" in e for e in validate(plan).errors))

    def test_bath_needs_cover(self):
        from babyreel.plan import Caption, validate
        plan = load_test_plan()
        plan.clips[0].covers = []
        plan.clips[0].captions = [Caption(at=0.1, text="목욕 시간", style="sub", dur=1.5)]
        self.assertTrue(any("목욕 장면" in w for w in validate(plan).warnings))

    def test_thumbnail_avoids_cover_scene(self):
        from babyreel.plan import Cover, thumbnail_report
        plan = load_test_plan()
        plan.clips[plan.thumbnail.clip].covers = [Cover(points=[[0.0, 0.5, 0.5]])]
        self.assertTrue(any("가림 스티커" in w for w in thumbnail_report(plan)))


class ChallengeTest(unittest.TestCase):
    """댓글 챌린지 (2026-10-23): 개월 수·안전 거르기, 시점 판단, 댓글 수집, 에피소드 생성."""

    def test_screen_safety(self):
        from babyreel.challenges import screen
        ok = lambda t, m=24: screen(t, m).ok  # noqa: E731
        self.assertTrue(ok("하루 동안 간식 안 주고 밥만 주기"))
        self.assertTrue(ok("3일 동안 매일 다른 놀이터 가기"))
        self.assertTrue(ok("꿀잠 자는 첫째 기록하기"))  # '꿀잠' 은 꿀이 아님
        self.assertTrue(ok("떡뻥 먹방 하기"))
        for bad in ("하루 동안 밥 안 주기", "100시간 안 재우기", "몰카로 놀래키기", "꿀 먹여 보기", "통포도 주기",
                    "혼자 두고 반응 보기", "다이어트 시키기", "30일 동안 정리하기", "매운 거 먹이기", "TV 보여주기 7일" ):
            self.assertFalse(ok(bad, 6 if "TV" in bad else 24), bad)
        self.assertFalse(screen("하루 동안 간식 대신 밥만", 9).ok)  # 돌 전
        self.assertTrue(screen("하루 동안 간식 대신 밥만", 13).ok)
        self.assertTrue(screen("하루 동안 간식 대신 밥만", 24).notes)  # 주의 문구

    def test_phase_by_story_lag(self):
        from babyreel import challenges as chg
        with tempfile.TemporaryDirectory() as d:
            for name, dt, series in (("a", "2026-09-20", "observe"), ("b", "2026-10-20", "special"), ("_test", "2026-10-22", "observe")):
                (Path(d) / name).mkdir()
                (Path(d) / name / "plan.json").write_text(json.dumps({"date": dt, "series": series}))
            cfg = {**config.load(), "challenge": {"mode": "auto", "warmup_within_days": 75, "live_within_days": 21}}
            with mock.patch.object(chg, "EPISODES", Path(d)), mock.patch.object(config, "load", return_value=cfg):
                self.assertEqual(chg.story_lag(date(2026, 10, 23)), (33, "a"))  # 번외편·테스트 제외
                self.assertEqual(chg.phase(date(2026, 10, 23)), "warmup")
                self.assertEqual(chg.phase(date(2026, 10, 1)), "live")
                self.assertEqual(chg.phase(date(2027, 3, 1)), "archive")
            cfg["challenge"]["mode"] = "live"
            with mock.patch.object(config, "load", return_value=cfg):
                self.assertEqual(chg.phase(), "live")

    def test_ideas_store_and_comments(self):
        from babyreel import challenges as chg
        with tempfile.TemporaryDirectory() as d, mock.patch.object(chg, "IDEAS", Path(d) / "i.json"):
            a = chg.add("하루 동안 간식 대신 밥만 먹기 해주세요!", "manual")
            self.assertEqual((a.id, a.status, a.child), (1, "ok", "첫째"))
            self.assertIsNone(chg.add("하루 동안 간식 대신 밥만 먹기 해주세요", "telegram"))  # 중복
            b = chg.add("첫째 밤새 안 재우기 챌린지", "manual")
            self.assertEqual(b.status, "blocked")

            class FakeYT:
                def commentThreads(self):
                    return self
                def list(self, **kw):
                    return self
                def execute(self):
                    mk = lambda t, n, who: {"snippet": {"topLevelComment": {"snippet": {  # noqa: E731
                        "textDisplay": t, "likeCount": n, "authorDisplayName": who}}}}
                    return {"items": [mk("3일 동안 매일 다른 놀이터 가기 해봐요", 12, "@fan1"),
                                      mk("너무 귀엽다ㅠㅠ", 50, "@fan2"),
                                      mk("둘째 까꿍 100번 챌린지!", 3, "@fan3")]}
            n = chg.collect_comments(FakeYT(), ["vid1"])
            self.assertEqual(n, 2)  # 칭찬 댓글은 아이디어 아님
            top = chg.ranked()
            self.assertEqual(top[0].author, "@fan1")
            self.assertEqual([i.child for i in top if "까꿍" in i.text], ["둘째"])
            self.assertIn("@fan1", chg.credit_line(top[0]))
            self.assertIn("했더니 생기는 일", chg.title_for(top[0], 3))

    def test_plan_validation(self):
        plan = load_test_plan()
        plan.series = "challenge"
        plan.challenge = {}
        self.assertTrue(any("challenge.text" in e for e in validate(plan).errors))
        plan.challenge = {"text": "하루 동안 밥 안 주기", "child": "첫째"}
        self.assertTrue(any("안전 기준" in e for e in validate(plan).errors))
        plan.challenge = {"text": "3일 동안 매일 다른 놀이터 가기", "child": "첫째"}
        self.assertFalse(any("챌린지" in e for e in validate(plan).errors))

    def test_cta_only_when_close(self):
        from babyreel import challenges as chg
        with mock.patch.object(chg, "phase", return_value="archive"):
            self.assertEqual(chg.cta({"첫째": 24}), "")
        with mock.patch.object(chg, "phase", return_value="live"):
            line = chg.cta({"첫째": 24, "둘째": 9})
            self.assertIn("첫째(24개월)", line)
            self.assertIn("둘째(9개월)", line)

    def test_telegram_parse(self):
        from babyreel import telegram_bot as tb
        self.assertEqual(tb.parse("챌린지: 하루 동안 간식 대신 밥만"), ("challenge", "하루 동안 간식 대신 밥만"))
        self.assertEqual(tb.parse("챌린지"), ("challenge", ""))
        self.assertEqual(tb.parse("영상 만들어줘: 챌린지 1일차")[0], "make")



class StorageTest(unittest.TestCase):
    """외장 SSD (babyreel/storage.py): 끊겨도 원본 파일이 남는지, 새 폴더가 SSD 로 가는지."""

    def _env(self, tmp: Path):
        from babyreel import config, storage
        ssd = tmp / "ssd"
        ssd.mkdir()
        return [mock.patch.object(storage, "_cfg", return_value={"ssd_root": str(ssd), "min_free_gb": 0}),
                mock.patch.object(storage, "online", return_value=True),
                mock.patch.object(config, "LIBRARY", tmp / "library"),
                mock.patch.dict(os.environ, {}, clear=False)], ssd

    def test_safe_copy_failure_leaves_nothing(self):
        from babyreel import storage
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "a.mov"
            src.write_bytes(os.urandom(3 << 20))
            dest = Path(t) / "ssd" / "a.mov"
            real = open
            calls = {"n": 0}

            def flaky(path, mode="r", *a, **k):  # 쓰는 도중 끊김
                f = real(path, mode, *a, **k)
                if "w" in mode:
                    w = f.write

                    def write(b):
                        calls["n"] += 1
                        if calls["n"] > 0:
                            raise OSError(5, "unplugged")
                        return w(b)
                    f.write = write
                return f
            with mock.patch("builtins.open", flaky):
                with self.assertRaises(OSError):
                    storage.safe_copy(src, dest)
            self.assertFalse(dest.exists())
            self.assertEqual(list(dest.parent.iterdir()), [])  # .part 도 남지 않음
            self.assertTrue(src.exists())
            storage.safe_move(src, dest)  # 다시 하면 성공, 원본은 그때 지움
            self.assertTrue(dest.exists() and not src.exists())

    def test_new_month_goes_to_ssd_and_tests_stay_local(self):
        from babyreel import storage
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            patches, ssd = self._env(tmp)
            with patches[0], patches[1], patches[2], mock.patch.dict(os.environ, {"BABYREEL_NO_SSD": ""}):
                d = storage.library_dir("2026-11")
                self.assertTrue(d.is_symlink())
                self.assertEqual(Path(os.readlink(d)), ssd / "library" / "2026-11")
                ep = tmp / "episodes" / "2026-11-01_x"
                ep.mkdir(parents=True)
                self.assertTrue(storage.out_dir(ep).is_symlink())
                test_ep = tmp / "episodes" / "_test"
                test_ep.mkdir()
                self.assertFalse(storage.out_dir(test_ep).is_symlink())

    def test_ssd_offline_stops_before_changes(self):
        from babyreel import storage
        with mock.patch.object(storage, "_cfg", return_value={"ssd_root": "/Volumes/__없는디스크__/x"}), \
                mock.patch.dict(os.environ, {"BABYREEL_NO_SSD": ""}):
            with self.assertRaises(storage.StorageError) as e:
                storage.require("편집")
            self.assertIn("연결돼 있지 않습니다", str(e.exception))

    def test_archive_path_is_ascii(self):
        from babyreel import storage
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "youtube" / "2026-10-18_sample-episode"
            d.mkdir(parents=True)
            (d / ".episode").write_text("2024-11-09_first-week-home:short2")
            with mock.patch.object(storage, "_cfg", return_value={"ssd_root": "/Volumes/X/filmcraft"}):
                a = storage.archive_path(d / "쇼츠영상.mp4")
                b = storage.archive_path(d / "롱폼영상.mp4")
        self.assertEqual(a.name, "2026-10-18_2024-11-09_first-week-home_short2.mp4")
        self.assertEqual(b.name, "2026-10-18_2024-11-09_first-week-home_long.mp4")
        self.assertTrue(str(a.relative_to("/Volumes/X/filmcraft")).isascii())

    def test_migrate_resumes_and_keeps_stale_ssd_files(self):
        from babyreel import storage
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            local, target = tmp / "out", tmp / "ssd" / "out"
            local.mkdir()
            (local / "short.mp4").write_bytes(b"new" * 1000)
            (local / "long.mp4").write_bytes(b"long" * 1000)
            target.mkdir(parents=True)
            (target / "short.mp4").write_bytes(b"old")  # SSD 의 예전 사본 → 최신본으로 덮어씀
            (target / "old_only.mp4").write_bytes(b"x")  # 내장 쪽에 없는 예전 파일 → 따로 보관
            with mock.patch.object(storage, "_remap_face_cache"):
                storage.migrate_dir(local, target, log=lambda *a: None)
            self.assertTrue(local.is_symlink())
            self.assertEqual((local / "short.mp4").read_bytes(), b"new" * 1000)
            self.assertFalse((target / "old_only.mp4").exists())
            self.assertTrue(any(p.name == "old_only.mp4" for p in (tmp / "ssd").rglob("*")))
            self.assertFalse((tmp / "out.옮기는중").exists())


if __name__ == "__main__":
    unittest.main()
