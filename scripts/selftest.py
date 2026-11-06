"""전체 기능 점검 (실제 FilmCraft·TTS·STT 사용, 약 3~4분).

    ./br selftest

테스트 영상으로 모든 명령을 실제로 실행하고, 끝나면 테스트가 만든 흔적(라이브러리 항목,
에피소드, 전달 폴더, 로그 줄)을 지운다. 사용자의 실제 영상·에피소드는 건드리지 않는다.
YouTube/Instagram 실제 업로드는 하지 않는다 (자격 증명이 없으면 폴더 방식으로 대체되는지 확인).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from babyreel import config, transcribe, voice  # noqa: E402
from babyreel.media import INDEX, load_index, save_index  # noqa: E402

TAG = "zz_selftest"
DATE = time.strftime("%Y-%m-%d")
EP = f"{DATE}_selftest-ep"
EP_LONG = f"{DATE}_selftest-weekly"
BR = [str(ROOT / "br")]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  — {detail}" if detail and not ok else ""))
    return ok


def br(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(BR + list(args), cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def step(title: str) -> None:
    print(f"\n▶ {title}")


TEST_DAY = "2001-01-01"  # 테스트 영상의 촬영일 (실제 원본 폴더와 겹치지 않는 달)


def main() -> int:
    deliver_tmp = Path(tempfile.mkdtemp(prefix="babyreel-deliver-"))
    # 업로드 폴더·업로드 일정은 임시 위치로 (사용자의 실제 일정·업로드 폴더를 건드리지 않음)
    # 촬영 폴더도 임시 위치로 (실제 inbox 는 iCloud 라 테스트 파일이 폰에 동기화됨)
    env = {"BABYREEL_DELIVER_DIR": str(deliver_tmp), "BABYREEL_SCHEDULE": str(deliver_tmp / "schedule.json"),
           "BABYREEL_INBOX": str(deliver_tmp / "inbox"),
           "BABYREEL_QUIET": "1", "BABYREEL_NOTIFY_MARK": str(deliver_tmp / "today_notified"),
           "BABYREEL_INSIGHTS_MARK": str(deliver_tmp / "insights_done"), "BABYREEL_SYNC_MARK": str(deliver_tmp / "sync_done"),
           "BABYREEL_BACKUP_MARK": str(deliver_tmp / "backup_done"),
           "BABYREEL_ORDER_DIR": str(deliver_tmp / "업로드"),  # SSD 업로드/ 의 날짜순 폴더 대신
           "BABYREEL_NO_SSD": "1"}  # 테스트 영상은 내장 디스크에서 (외장 SSD 를 건드리지 않음)
    os.environ.update(env)
    added_keys: list[str] = []
    try:
        step("1. 단위 테스트")
        r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_unit"], cwd=ROOT,
                           capture_output=True, text=True)
        check("단위 테스트", r.returncode == 0, r.stderr[-400:])

        step("2. 환경 점검 (doctor)")
        r = br("doctor")
        check("FilmCraft CLI·swift·BGM·효과음", r.returncode == 0, r.stdout)

        step("3. AI 음성 → 받아쓰기 왕복 (Supertonic → Whisper)")
        wav, _ = voice.speak("기록 결과, 오늘도 공을 발견함", "narrator")
        check("TTS 생성", wav.exists() and voice.duration(wav) > 1)
        if transcribe.available():
            import mlx_whisper
            text = mlx_whisper.transcribe(transcribe._load16k(wav), path_or_hf_repo=transcribe.MODEL,
                                          language="ko")["text"]
            check("TTS 음성이 알아들을 수 있음", "기록" in text and "공" in text, text)
            for kid in ("첫째", "둘째"):
                kw, _ = voice.speak("이건 내 거야", kid)
                check(f"{kid} 음색 생성", kw.exists())
        else:
            check("mlx-whisper 설치", False, "./scripts/setup.sh")
        from babyreel import kotext
        check("숫자 읽기 (세 번·두 시간·육아일지 칠 번)",
              kotext.spoken("육아일지 7번, 3번 울고 2시간") == "육아일지 칠 번, 세 번 울고 두 시간")
        ready, why = voice.clone_ready()
        if ready:
            line = "육아일지 7번. 기록 결과, 공을 발견함."
            cw, cvc = voice.speak(line, "narrator")
            err = voice.check(cw, line)
            check("아빠 목소리 내레이션 (Qwen3-TTS) + 받아쓰기 확인", cvc["engine"] == "clone" and err <= voice.MAX_CER,
                  f"{cvc['engine']} 오류율 {err:.0%}")
        else:
            check(f"아빠 목소리 미설정 → 기본 음성으로 대체 ({why})", voice.resolve_engine(voice.voice_config("narrator"))
                  == voice.engine())

        step("4. ingest: 영상 + 사진 + 메모")
        footage = ROOT / "tests" / "footage"
        if not (footage / "test_portrait_01.mp4").exists():
            subprocess.run([sys.executable, str(ROOT / "scripts" / "make_test_footage.py")], check=True)
        inbox = Path(env["BABYREEL_INBOX"])
        inbox.mkdir(exist_ok=True)
        # 테스트 전용 날짜(2001-01) → library/2001-01 은 내장 디스크 임시 폴더 (외장 SSD 의 이번 달 폴더를 건드리지 않음)
        shutil.copy(footage / "test_portrait_01.mp4", inbox / f"{TEST_DAY}_{TAG}_clip.mp4")
        shutil.copy(footage / "test_landscape_01.mp4", inbox / f"{TEST_DAY}_{TAG}_clip2.mp4")
        shutil.copy(footage / "test_photo.jpg", inbox / f"{TEST_DAY}_{TAG}_photo.jpg")
        (inbox / f"{TEST_DAY}_{TAG}_clip.txt").write_text("둘째가 공을 처음 봄. 첫째는 옆에서 구경", encoding="utf-8")
        (inbox / f"{TAG}_batch.txt").write_text("시리즈: 남매 대전", encoding="utf-8")
        before = set(load_index())
        r = br("ingest")
        index = load_index()
        added_keys = [k for k in index if k not in before and TAG in k]
        check("3개 정리됨", len(added_keys) == 3, r.stdout + r.stderr)
        clip_key = next((k for k in added_keys if k.endswith("_clip.mp4")), "")
        clip2_key = next((k for k in added_keys if k.endswith("_clip2.mp4")), "")
        photo_key = next((k for k in added_keys if k.endswith(".jpg")), "")
        if clip_key:
            memo = index[clip_key].get("memo", "")
            check("영상 전용 메모 + 공통 메모 연결", "공을 처음 봄" in memo and "남매 대전" in memo, memo)
            check("공통 메모만 연결 (다른 영상)", index[clip2_key].get("memo") == "시리즈: 남매 대전")
            check("컨택트시트 생성", (ROOT / "library/_sheets" / f"{Path(clip_key).stem}.png").exists())
            if transcribe.available():
                check("받아쓰기 파일 생성", (ROOT / "library/_transcripts" / f"{Path(clip_key).stem}.json").exists())
        if photo_key:
            check("사진 시트·번호", bool(index[photo_key].get("sheet")) and index[photo_key].get("label") == "#01")
        check("메모가 inbox 에서 치워짐", not list(inbox.glob(f"{TAG}*.txt")))

        step("5. new → plan 작성 → validate")
        from babyreel.__main__ import next_episode_no
        expected_no = next_episode_no()
        r = br("new", "selftest-ep", "--date", DATE, "--series", "siblings", "--cast", "첫째", "둘째",
               "--concept", "셀프테스트")
        ep_dir = config.EPISODES / EP
        plan = json.loads((ep_dir / "plan.json").read_text(encoding="utf-8"))
        check("에피소드 생성 + 육아일지 번호 자동", plan["episode_no"] == expected_no and plan["series"] == "siblings",
              r.stdout + r.stderr)
        plan.update({
            "title_lines": ["공을 처음 본", "둘째의 반응ㅋㅋ"],
            "clips": [
                {"src": clip_key, "start": 0.5, "end": 5.5, "focus_y": 0.5, "chapter": "첫 만남", "people": ["둘째", "아빠"],
                 "captions": [
                     {"at": 0.2, "text": "기록 시작 14:02", "style": "chip", "dur": 1.5},
                     {"at": 0.3, "text": "기록 결과, 공을 발견함", "style": "sub", "dur": 2.4, "voice": True},
                     {"at": 3.0, "text": "이건 내 거야", "style": "talk", "speaker": "둘째", "voice": True,
                      "sfx": "bloop", "dur": 1.8}],
                 "punch": [{"at": 3.0, "dur": 1.0, "scale": 1.3}],
                 "stickers": [{"at": 3.1, "emoji": "❓", "dur": 1.4, "x": 0.8, "y": 0.2}]},
                {"src": clip_key, "start": 5.5, "end": 6.7, "hold": True, "chapter": "정적",
                 "captions": [{"at": 0.0, "text": "(정적)", "style": "big", "sfx": "dundun", "dur": 1.2}]},
                {"src": clip2_key, "start": 1.0, "end": 5.5, "focus": 0.7, "chapter": "첫째 등장",
                 "mute_ranges": [[2.0, 2.6]], "music": "quirky_dog",  # 장면 전환: 여기서부터 다른 곡 (크로스페이드)
                 # 창밖 흐리게: 카메라가 움직이면 영역이 따라감 (2026-11-03 사용자 요청)
                 "blur_areas": [{"points": [[1.0, 0.0, 0.0, 0.3, 0.4], [5.5, 0.3, 0.0, 0.6, 0.4]], "what": "창밖"}],
                 "captions": [{"at": 0.3, "text": "나도 할래!", "style": "talk", "speaker": "첫째", "dur": 1.8},
                              {"at": 2.5, "text": "특이사항: 둘 다 포기 안 함", "style": "sub", "dur": 1.8}]},
                {"src": photo_key, "start": 0, "end": 2.0, "targets": ["short"],
                 "captions": [{"at": 0.1, "text": "기록 완료.", "style": "chip", "dur": 1.8}]},
                {"src": clip_key, "start": 2.0, "end": 5.0, "speed": 0.75, "chapter": "다시 보기",
                 "captions": [{"at": 0.1, "text": "다시 보기", "style": "chip", "sfx": "whoosh", "dur": 1.5}]},
                # 쇼츠 2부 (2일차): 1부와 다른 장면
                {"src": clip2_key, "start": 5.5, "end": 7.0, "targets": ["short2", "long"], "chapter": "2라운드",
                 "people": ["첫째", "엄마"],
                 "captions": [{"at": 0.1, "text": "2라운드 시작", "style": "chip", "sfx": "pop", "dur": 1.3},
                              {"at": 0.2, "text": "기록 결과, 아직 포기 안 함", "style": "sub", "dur": 1.2}],
                 "stickers": [{"at": 0.3, "emoji": "🔥", "dur": 1.0, "x": 0.8, "y": 0.25}]},
                {"src": clip_key, "start": 6.0, "end": 8.0, "targets": ["short2"],
                 "captions": [{"at": 0.1, "text": "결국 공은 둘째 차지", "style": "big", "sfx": "ding", "dur": 1.6}],
                 "punch": [{"at": 0.2, "dur": 0.8, "scale": 1.25}]},
            ],
            "music": {"track": "monkeys_spinning_monkeys", "cuts": [[3.2, 3.8]]},  # 정적은 대사가 나오는 동안에
            "outputs": {"short": True, "short2": True, "long": True},
            "short2": {"title_lines": ["공 쟁탈전", "2라운드ㅋㅋ"],
                       "thumbnail": {"clip": 6, "at": 6.5, "text": ["2라운드", "승자는?"], "no_face": True},
                       "youtube_title": "샘플 2편 제목", "description": "샘플 설명 2.",
                       "instagram_caption": "공 쟁탈전 2라운드", "hashtags": ["#공놀이"]},
            "thumbnail": {"clip": 0, "at": 3.5, "text": ["공 vs 둘째", "결과는?"], "no_face": True},
            "youtube_title": "공을 처음 본 둘째의 반응ㅋㅋ",
            "description": "샘플 설명 1.",
            "hashtags": ["#공놀이", "#남매일상"],
            "instagram_caption": "공이랑 처음 만난 날",
        })
        (ep_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        r = br("validate", EP)
        check("validate 통과", r.returncode == 0, r.stdout)
        check("가족 등장 점검 (쇼츠 1·2부·롱폼 모두 부모 컷 있음 → 경고 없음)", "엄마·아빠 얼굴이" not in r.stdout, r.stdout)

        step("6. build --no-export (미리보기)")
        r = br("build", EP, "--no-export")
        check("빠른 조립", r.returncode == 0 and (ep_dir / "out/preview_short.png").exists(), r.stdout[-600:] + r.stderr[-600:])
        keys = []
        if (ep_dir / "project.fcproj").exists():
            def masks(o):
                if isinstance(o, dict):
                    yield from o.get("masks") or []
                    for v in o.values():
                        yield from masks(v)
                elif isinstance(o, list):
                    for v in o:
                        yield from masks(v)
            proj = json.loads((ep_dir / "project.fcproj").read_text(encoding="utf-8"))
            keys = [sorted(round(k["time"] / 254016000000, 2) for k in m["path"].get("keyframes", [])) for m in masks(proj)]
        check("창밖 블러: 영역이 원본 1.0초→5.5초로 따라감 (마스크 키프레임 = 원본 시각)", [1.0, 5.5] in keys,
              str(keys) + r.stdout[-300:])

        step("7. build (내보내기·마스터링·썸네일·메타데이터)")
        r = br("build", EP)
        ok = r.returncode == 0
        for f in ("out/short.mp4", "out/short2.mp4", "out/long.mp4", "out/thumbnail.jpg", "out/cover.jpg",
                  "out/cover2.jpg", "out/short2_nobgm.mp4", "project.fcproj", "build.json", "metadata.json"):
            ok &= check(f"생성: {f}", (ep_dir / f).exists(), r.stderr[-400:])
        manifest = json.loads((ep_dir / "build.json").read_text(encoding="utf-8")) if ok else {}
        check("AI 음성 사용 기록", manifest.get("ai_voice") is True)
        used = [m["id"] for m in manifest["outputs"].get("short", {}).get("music_all", [])]
        check("장면별 BGM: 쇼츠에 두 곡(전환) 기록", used == ["monkeys_spinning_monkeys", "quirky_dog"], str(used))
        meta = json.loads((ep_dir / "metadata.json").read_text(encoding="utf-8"))
        check("장면별 BGM: 설명란에 두 곡 모두 크레딧",
              sum("🎵 Music:" in line for line in meta["youtube_shorts"]["description"].splitlines()) == 2)
        idx = load_index()
        check("라이브러리에 사용 기록(used_in)", EP in idx.get(clip_key, {}).get("used_in", []))

        step("8. qa (규격·라우드니스)")
        r = br("qa", EP)
        check("QA 통과 (규격·음량·멈춘 화면·소리 끊김·자막 겹침/잘림, 쇼츠 1·2부+롱폼)", r.returncode == 0, r.stdout)
        check("QA 가 쇼츠 2부도 검사", "short2.mp4" in r.stdout, r.stdout[-400:])
        from babyreel import thumbs
        reps = {n: json.loads(thumbs.report_path(ep_dir / "out" / n).read_text()) for n in
                ("thumbnail.jpg", "thumbnail_b.jpg", "thumbnail_c.jpg", "cover.jpg", "cover2.jpg")}
        check("썸네일: 롱폼 A·B·C + 쇼츠 커버 2장, 글자가 자리에 맞음", all(r_["fit"] for r_ in reps.values()), str(reps.keys()))
        check("썸네일: 규격 1280x720·1080x1920, 2MB 이하",
              all((r_["width"], r_["height"]) == ((1280, 720) if n.startswith("thumbnail") else (1080, 1920))
                  for n, r_ in reps.items()) and all((ep_dir / "out" / n).stat().st_size < 2_000_000 for n in reps))

        step("8b. 검수기 자체 점검 (일부러 겹치고 잘리는 자막을 잡는지)")
        from babyreel import qa as qa_mod
        from babyreel.fc import FilmCraft, seconds_to_ticks as T
        bad = deliver_tmp / "bad.fcproj"
        with FilmCraft() as fc:
            fc.run("file.newSequence", name="short", width=1080, height=1920, fps=30, video=4)
            for txt, y, t, tr in [("겹치는 자막 하나", 1500, 0, 0), ("겹치는 자막 둘", 1505, 1.0, 1),
                                  ("화면 밖으로 나가는 아주 아주 긴 자막입니다 정말로 길어요", 900, 3, 2)]:
                c = fc.run("graphics.newText", text=txt, position=[540, y], size=80, seconds=2, track=tr, time=T(t),
                           font="Apple SD Gothic Neo")["clip"]
                fc.run("graphics.set", clip=c, layer=0, props={"align": "center", "font": "Apple SD Gothic Neo"})
            fc.run("file.saveAs", path=str(bad))
        errs, _ = qa_mod.layout_problems(bad, "short")
        check("겹침 감지", any("겹침" in e for e in errs), str(errs))
        check("잘림 감지", any("잘림" in e for e in errs), str(errs))
        check("멈춘 화면 감지 함수 동작", isinstance(qa_mod.freezes(ep_dir / "out/short.mp4"), list))
        # 소리 끊김: 가운데 0.5초가 완전히 무음인 wav 를 만들어 잡는지
        import array, math as _m, wave as _w
        gap_wav = deliver_tmp / "gap.wav"
        with _w.open(str(gap_wav), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(22050)
            a = array.array("h", [0 if 2.0 <= i / 22050 < 2.5 else int(8000 * _m.sin(i / 7)) for i in range(22050 * 6)])
            w.writeframes(a.tobytes())
        gaps = qa_mod.dropouts(gap_wav)
        check("소리 끊김 감지", len(gaps) == 1 and abs(gaps[0][0] - 2.0) < 0.1, str(gaps))

        step("9. publish (YouTube 비공개 업로드 → 자격 증명 없으면 폴더 대체, 인스타 폴더)")
        r = br("publish", EP, env=env)
        check("publish 실행", r.returncode == 0, r.stdout[-800:] + r.stderr[-800:])
        from datetime import date as _date, timedelta as _td
        DAY2 = (_date.fromisoformat(DATE) + _td(days=1)).isoformat()
        WD = "월화수목금토일"
        up = deliver_tmp / EP  # 업로드 폴더: <회차>/숏폼1·숏폼2·롱폼 (플랫폼 파일이 한 폴더에)
        s1, s2, lf = up / "숏폼1", up / "숏폼2", up / "롱폼"
        check("업로드 폴더: 회차/숏폼1·숏폼2·롱폼 3개", sorted(d.name for d in up.iterdir() if d.is_dir()) == ["롱폼", "숏폼1", "숏폼2"],
              str(list(up.iterdir())) if up.exists() else "없음")
        sched = json.loads((deliver_tmp / "schedule.json").read_text(encoding="utf-8")).get(EP, {})
        check("업로드 일정: 1부 1일차, 2부·롱폼 2일차", sched == {"short": DATE, "short2": DAY2, "long": DAY2}, str(sched))
        order = deliver_tmp / "업로드"
        eps = [d for d in order.iterdir() if d.is_dir()] if order.exists() else []
        names = sorted(d.name for d in eps[0].iterdir() if d.is_dir()) if len(eps) == 1 else []
        md = lambda d: f"{d[5:]} {WD[_date.fromisoformat(d).weekday()]}"  # noqa: E731
        check("업로드 폴더: 회차 폴더('날짜~ 제목') 안에 숏폼1·숏폼2·롱폼 (회차 폴더는 링크)",
              len(eps) == 1 and eps[0].name.startswith(DATE + "~")
              and names == [f"롱폼 ({md(DAY2)})", f"숏폼1 ({md(DATE)})", f"숏폼2 ({md(DAY2)})"]
              and s1.is_symlink() and (s1 / "숏폼영상.mp4").exists(), str([e.name for e in eps]) + str(names))
        board = (order / "업로드일정.txt").read_text(encoding="utf-8") if (order / "업로드일정.txt").exists() else ""
        check("업로드일정.txt (업로드/ 맨 위): 숏폼2", "숏폼2" in board and "업로드/" in board, board[-400:])
        for f in ("숏폼영상.mp4", "커버.jpg", "인스타_캡션.txt", "인스타_업로드방법.txt", "틱톡영상.mp4", "틱톡_캡션.txt"):
            check(f"숏폼1/{f}", (s1 / f).exists())
        check("숏폼2 = 다른 영상 (2라운드)", (s2 / "숏폼영상.mp4").exists()
              and "2라운드" in (s2 / "유튜브_제목.txt").read_text(encoding="utf-8")
              if (s2 / "유튜브_제목.txt").exists() else False)
        check("올릴 날: 숏폼1 오늘 · 숏폼2 내일", (s1 / "인스타_업로드방법.txt").exists()
              and DATE in (s1 / "인스타_업로드방법.txt").read_text(encoding="utf-8").splitlines()[0]
              and (s2 / "인스타_업로드방법.txt").exists()
              and DAY2 in (s2 / "인스타_업로드방법.txt").read_text(encoding="utf-8").splitlines()[0])
        if (s1 / "인스타_캡션.txt").exists():
            cap = (s1 / "인스타_캡션.txt").read_text(encoding="utf-8")
            tags = [w for w in cap.split() if w.startswith("#")]
            check("캡션: 서명·크레딧·AI 고지·해시태그≤5",
                  "<채널 이름>" in cap and "Kevin MacLeod" in cap and "AI 음성" in cap and len(tags) <= 5, cap)
        secrets = (ROOT / "secrets" / "youtube_client_secret.json").exists()
        if secrets:
            check("YouTube 비공개 업로드 결과", all((d / "유튜브_업로드결과.txt").exists() for d in (s1, s2, lf)))
        else:
            check("YouTube 폴더 대체 (자격 증명 없음): 숏폼1·2·롱폼에 영상·썸네일·제목",
                  all((d / "유튜브_제목.txt").exists() for d in (s1, s2, lf)) and (lf / "롱폼영상.mp4").exists()
                  and (lf / "썸네일.jpg").exists() and (s1 / "커버.jpg").exists())
            check("YouTube 롱폼 폴더: A/B 테스트용 썸네일_B·C + 안내",
                  (lf / "썸네일_B.jpg").exists() and (lf / "썸네일_C.jpg").exists()
                  and "A/B 테스트" in (lf / "유튜브_업로드방법.txt").read_text(encoding="utf-8"))
        state = json.loads((ep_dir / "publish.json").read_text(encoding="utf-8"))
        check("숏폼 영상은 숏폼영상.mp4·틱톡영상.mp4 2개만 (쇼츠=릴스 한 파일)",
              sorted(f.name for f in s1.iterdir() if f.suffix == ".mp4") == ["숏폼영상.mp4", "틱톡영상.mp4"])
        check("틱톡 = BGM 없는 버전 (외부 음원 음소거 대비)",
              (ep_dir / "out/short_nobgm.mp4").exists() and (s1 / "틱톡영상.mp4").exists()
              and (s1 / "틱톡영상.mp4").stat().st_size == (ep_dir / "out/short_nobgm.mp4").stat().st_size
              and "사운드 추가" in (s1 / "틱톡_업로드방법.txt").read_text(encoding="utf-8"))
        check("publish.json 기록 7건 (쇼츠 1·2부 × 3곳 + 롱폼)",
              set(state) == {"youtube_shorts", "instagram_reels", "tiktok", "youtube_long",
                             "youtube_shorts_2", "instagram_reels_2", "tiktok_2"}, str(set(state)))
        meta = json.loads((ep_dir / "metadata.json").read_text(encoding="utf-8"))
        check("2부 설명: 육아일지 No.N-2", f"No.{plan['episode_no']}-2" in meta["youtube_shorts_2"]["description"])

        step("10. published (수동 업로드 기록)")
        r = br("published", EP, "instagram_reels", "https://instagram.com/reel/test")
        state = json.loads((ep_dir / "publish.json").read_text(encoding="utf-8"))
        check("URL 기록", state["instagram_reels"]["url"] == "https://instagram.com/reel/test")

        step("11. export (앱에서 손본 project.fcproj 다시 내보내기)")
        mtime = (ep_dir / "out/short.mp4").stat().st_mtime
        r = br("export", EP)
        check("다시 내보내기", r.returncode == 0 and (ep_dir / "out/short.mp4").stat().st_mtime > mtime, r.stderr[-400:])

        step("12. compile (몰아보기 롱폼)")
        r = br("compile", "selftest-weekly", EP, "--date", DATE)
        long_dir = config.EPISODES / EP_LONG
        lp = json.loads((long_dir / "plan.json").read_text(encoding="utf-8"))
        check("몰아보기 plan", lp["outputs"] == {"short": False, "long": True} and lp["clips"]
              and lp["clips"][0]["chapter"] and "No." in lp["description"], r.stdout)
        lp.update({"youtube_title": "첫째·둘째 육아일지 몰아보기", "youtube_long_title": "첫째·둘째 육아일지 몰아보기",
                   "thumbnail": {"clip": 0, "at": 2.0, "text": ["육아일지", "몰아보기"], "no_face": True}})
        (long_dir / "plan.json").write_text(json.dumps(lp, ensure_ascii=False, indent=2), encoding="utf-8")
        r = br("build", EP_LONG, "--no-export")
        check("몰아보기 조립", r.returncode == 0 and (long_dir / "out/preview_long.png").exists(), r.stderr[-400:])

        step("13. status · music · schedule · today")
        check("status", br("status").returncode == 0)
        r = br("listen", clip_key)
        check("listen (진짜 대사/환각 구분)", r.returncode == 0, r.stderr[-300:])
        r = br("schedule", env=env)
        check("schedule (업로드 일정·재고)", r.returncode == 0 and "숏폼2" in r.stdout and "재고" in r.stdout, r.stdout)
        r = br("today", "--notify", env=env)
        check("today (오늘 올릴 영상)", r.returncode == 0 and "숏폼1" in r.stdout, r.stdout)
        check("music", "fluffing_a_duck" in br("music").stdout)

        step("14. 무인 실행 스크립트 (새 영상 없을 때)")
        # 매일 점검(daily.sh): 오늘 이미 한 것으로 표시 → 단계마다 건너뛰고 정상 종료하는지 (네트워크·알림 없이)
        from datetime import date as _date
        for m in ("today_notified", "insights_done", "sync_done", "backup_done"):
            (deliver_tmp / m).write_text(_date.today().isoformat() + "\n")
        r = subprocess.run(["zsh", str(ROOT / "scripts/daily.sh")], cwd=ROOT, capture_output=True, text=True,
                           env={**os.environ, **env, "BABYREEL_LOG": str(deliver_tmp / "daily.log")})
        check("daily.sh: 하루 한 번 점검 (이미 한 단계는 건너뜀) → 정상 종료",
              r.returncode == 0 and not (ROOT / "data/daily.lock").exists(), r.stdout[-300:] + r.stderr[-300:])
        unused = [k for k, v in load_index().items() if not v.get("used_in") and TAG not in k]
        if unused:
            check("auto.sh (건너뜀: 사용자의 미사용 영상이 있어 실제 Claude 실행을 피함)", True)
        else:
            for k in [k for k in added_keys if not load_index()[k].get("used_in")]:
                i = load_index(); i[k]["used_in"].append("_selftest"); save_index(i)
            r = subprocess.run(["zsh", str(ROOT / "scripts/auto.sh")], cwd=ROOT, capture_output=True, text=True,
                               env={**os.environ, **env})
            log = sorted((ROOT / "logs").glob("auto_*.log"))[-1].read_text(encoding="utf-8")
            check("auto.sh: 새 영상 없음 → 종료", r.returncode == 0 and "새 영상 없음" in log, log[-300:])
    finally:
        step("정리")
        index = load_index()
        for k in added_keys:
            stem = Path(k).stem
            (ROOT / k).unlink(missing_ok=True)
            for p in [ROOT / "library/_sheets" / f"{stem}.png", ROOT / "library/_transcripts" / f"{stem}.json",
                      ROOT / index.get(k, {}).get("sheet", "/nonexistent")]:
                if p.is_file():
                    p.unlink()
            index.pop(k, None)
        save_index(index)
        for m in (ROOT / "library/_memos").glob(f"*{TAG}*"):
            m.unlink()
        test_month = ROOT / "library" / TEST_DAY[:7]
        if test_month.is_dir() and not test_month.is_symlink() and not any(test_month.iterdir()):
            test_month.rmdir()
        for d in (config.EPISODES / EP, config.EPISODES / EP_LONG):
            shutil.rmtree(d, ignore_errors=True)
        from babyreel import series  # 테스트 에피소드가 연재 기록부(docs/SERIES.md)에 남지 않게 다시 생성
        series.write()
        shutil.rmtree(deliver_tmp, ignore_errors=True)
        log = config.DATA / "publish_log.jsonl"
        if log.exists():
            keep = [l for l in log.read_text(encoding="utf-8").splitlines() if "selftest" not in l]
            log.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        for p in list(config.INBOX.glob(f"{TAG}*")) + list((deliver_tmp / "inbox").glob(f"{TAG}*")):
            p.unlink()
        print("  테스트 흔적 삭제 완료")

    failed = [r for r in results if not r[1]]
    print(f"\n결과: {len(results) - len(failed)}/{len(results)} 통과")
    for name, _, detail in failed:
        print(f"  ✗ {name}: {detail[:300]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
