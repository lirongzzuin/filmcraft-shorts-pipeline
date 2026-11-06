"""babyreel CLI.

  python3 -m babyreel setup                 음원·효과음·스티커 준비 + 환경 점검
  python3 -m babyreel doctor                환경 점검만
  python3 -m babyreel ingest                inbox/ → library/ 정리 + 컨택트시트
  python3 -m babyreel sheet <영상>...        컨택트시트 다시 만들기
  python3 -m babyreel new <slug> [--date]   에피소드 폴더 + plan.json 템플릿
  python3 -m babyreel validate <ep>         plan.json 검사
  python3 -m babyreel build <ep> [--only short|short2|long] [--no-export]   (쇼츠 1부·2부 + 롱폼)
  python3 -m babyreel export <ep>           (앱에서 손본) project.fcproj 를 다시 내보내기
  python3 -m babyreel qa <ep>               결과물 규격·라우드니스 검사
  python3 -m babyreel publish <ep> [--target ...]   YouTube 비공개 업로드 + 인스타 폴더 준비
  python3 -m babyreel published <ep> <target> <url> 수동 업로드 후 URL 기록
  python3 -m babyreel compile <slug> <ep>...        여러 에피소드 → 롱폼 몰아보기 plan
  python3 -m babyreel status                에피소드 진행 현황
  python3 -m babyreel schedule              업로드 일정 (쇼츠 매일 · 롱폼 이틀에 한 번) + 재고
  python3 -m babyreel today [--notify]      오늘 올릴 영상 (Mac 알림 + 텔레그램)
  python3 -m babyreel insights [login|offline] [--notify]   YouTube 성과 수집 → docs/INSIGHTS.md
  python3 -m babyreel telegram setup <토큰> | run | send <문장> | test    텔레그램으로 제작 요청·알림
  python3 -m babyreel music                 BGM 카탈로그 (분위기별)
  python3 -m babyreel selftest              전체 기능 점검 (테스트 영상으로 모든 명령 실행 후 정리)

<ep> 는 episodes/ 아래 폴더 이름 또는 경로.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from . import config, deliver, metadata, music, sfx
from .config import EPISODES, INBOX
from .fc import ROOT, cli_path, rel
from .plan import TARGET_NAMES


def ep_dir(name: str) -> Path:
    p = Path(name)
    if p.is_dir():
        return p.resolve()
    p = EPISODES / name
    if not p.is_dir():
        sys.exit(f"에피소드 없음: {name}")
    return p


def cmd_doctor(_: argparse.Namespace) -> bool:
    import subprocess

    ok = True

    def line(good: bool, text: str) -> None:
        nonlocal ok
        ok &= good
        print(f"  {'✓' if good else '✗'} {text}")

    try:
        v = subprocess.run([str(cli_path()), "--version"], capture_output=True, text=True).stdout.strip()
        line(True, f"FilmCraft CLI: {v}")
    except FileNotFoundError as e:
        line(False, str(e))
    line(shutil.which("swift") is not None, "swift (이모지 스티커 렌더링)")
    cat = music.load_catalog()
    have = [e for e in cat if config.resolve(e["file"]).exists()]
    line(len(have) > 0, f"BGM {len(have)}/{len(cat)}곡")
    line(all((sfx.SFX_DIR / f"{n}.wav").exists() for n in sfx.CATALOG), f"효과음 {len(sfx.CATALOG)}종")
    from . import faces, telegram_bot

    try:
        faces._binary()
        line(faces.BIN.exists(), "얼굴 보호 (macOS Vision 얼굴·글자 인식)")
    except Exception as e:  # noqa: BLE001
        line(False, f"얼굴 보호: {e}")
    inbox = INBOX.resolve()
    kind = "iCloud" if "CloudDocs" in str(inbox) else ("로컬" if inbox != (ROOT / "inbox") else "저장소 안 inbox/")
    print(f"  · 촬영 폴더(inbox, {kind}): {inbox}")
    print("  · 텔레그램: " + ("연결됨" if telegram_bot.configured() else "미설정 (선택: ./br telegram setup <토큰>)"))
    from . import voice

    ready, why = voice.clone_ready()
    print("  · 내레이션 목소리: " + (why if ready else f"기본 AI 음성 (아빠 목소리 미설정: {why})"))
    cfg = config.load()
    print(f"  · 채널: {cfg['channel']['name']} {cfg['channel'].get('handle', '')}")
    for c in config.children():
        refs = [p for p in config.resolve(c.get("ref", "")).glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png")] \
            if c.get("ref") else []
        if not c.get("birth_date"):
            print(f"  · {c['nick']}: 생일 미설정 → 영상에 나이 표기 안 됨 (config/channel.toml [[children]])")
        if not refs:
            print(f"  · {c['nick']}: 기준 사진 없음 → {c.get('ref')}/ 에 얼굴 사진 1~3장 (누가 누군지 판별용)")
    return ok


def cmd_setup(args: argparse.Namespace) -> None:
    from . import stickers

    for d in (INBOX, config.LIBRARY, EPISODES, config.DATA):
        d.mkdir(parents=True, exist_ok=True)
    print("▶ 효과음 합성")
    sfx.generate()
    print("▶ BGM 다운로드 (CC BY 4.0, Kevin MacLeod)")
    music.download()
    print("▶ 스티커/화자 아이콘")
    for e in [s["emoji"] for s in config.load().get("speakers", {}).values()] + list("❓❗✨🤣😳😭👍💕🎉💤"):
        stickers.png_for(e)
    print("▶ 점검")
    cmd_doctor(args)


def cmd_ingest(_: argparse.Namespace) -> None:
    from .media import ingest

    added = ingest(INBOX)
    print(f"{len(added)}개 정리됨. 컨택트시트: library/_sheets/")


def cmd_sheet(args: argparse.Namespace) -> None:
    from .media import contact_sheets

    contact_sheets([Path(p).resolve() for p in args.files])


TEMPLATE = {
    "series": "observe",
    "cast": [],
    "episode_no": 0,
    "layout": "frame",
    "title_lines": ["포인트 문구", "본문 문구ㅋㅋ"],
    "clips": [],
    "music": {"track": "fluffing_a_duck", "cuts": []},
    # 한 번에 쇼츠 2개(1부=1일차, 2부=2일차) + 롱폼 1개 = 이틀치 (docs/CONCEPT.md 12번)
    "outputs": {"short": True, "short2": True, "long": True},
    "thumbnail": {"clip": 0, "at": 0.0, "text": []},
    "short2": {"title_lines": ["포인트 문구", "본문 문구ㅋㅋ"], "thumbnail": {"clip": 0, "at": 0.0, "text": []},
               "youtube_title": "", "description": "", "instagram_caption": "", "hashtags": []},
    "youtube_title": "",
    "youtube_long_title": "",
    "description": "",
    "tags": [],
    "hashtags": [],
    "instagram_caption": "",
    "safety_reviewed": False,
}


def cmd_new(args: argparse.Namespace) -> None:
    d = args.date or date.today().isoformat()
    ep_id = f"{d}_{args.slug}"
    path = EPISODES / ep_id
    if (path / "plan.json").exists():
        sys.exit(f"이미 있음: {rel(path)}")
    path.mkdir(parents=True)
    plan = {"id": ep_id, "date": d, "concept": args.concept or "", **TEMPLATE,
            "series": args.series, "cast": args.cast or [], "episode_no": next_episode_no()}
    # BGM 기본값: 최근 편에서 안 쓴 유쾌한 곡 (1부 '웃김', 2부 '귀여움') — 장면에 맞게 바꾸는 건 기획 때 (STYLE 7번)
    recent = music.recent_tracks(before=d)
    picks = [(music.suggest(tag, recent, n=1) or [{"id": dflt}])[0]["id"]
             for tag, dflt in (("웃김", "fluffing_a_duck"), ("귀여움", "merry_go"))]
    plan["music"] = {"track": picks[0], "cuts": []}
    plan["short2"] = {**plan["short2"], "music": {"track": picks[1], "cuts": []}}
    (path / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✓ {rel(path / 'plan.json')}")


def next_episode_no() -> int:
    """육아일지 번호: 지금까지 쇼츠 에피소드 중 가장 큰 번호 + 1 (테스트·몰아보기 제외)."""
    n = 0
    for f in EPISODES.glob("*/plan.json"):
        if f.parent.name.startswith("_"):
            continue
        p = json.loads(f.read_text(encoding="utf-8"))
        if p.get("outputs", {}).get("short"):
            n = max(n, int(p.get("episode_no") or 0))
    return n + 1


def cmd_validate(args: argparse.Namespace) -> bool:
    from .plan import Plan, validate

    plan = Plan.load(ep_dir(args.ep))
    report = validate(plan, for_package=args.package)
    report.print()
    return report.ok


def cmd_build(args: argparse.Namespace) -> None:
    from .build import build

    d = ep_dir(args.ep)
    targets = [args.only] if args.only else None
    manifest = build(d, targets, export=not args.no_export, verbose=args.verbose)
    from .plan import Plan

    metadata.write(d, Plan.load(d))
    print(f"✓ 완료 ({manifest['build_seconds']}s)")
    for t, o in manifest["outputs"].items():
        print(f"  {t}: {o['file'] or '(내보내기 생략)'}  {o['duration']}s  미리보기 {o['preview']}")
    print(f"  썸네일 {manifest['thumbnail']}  ·  프로젝트 {rel(d / 'project.fcproj')}  ·  metadata.json")


def cmd_export(args: argparse.Namespace) -> None:
    """앱에서 project.fcproj 를 손본 경우: 다시 조립하지 않고 그대로 내보낸다."""
    from .build import Builder
    from .fc import FilmCraft
    from .plan import Plan

    d = ep_dir(args.ep)
    plan = Plan.load(d)
    cfg = config.load()
    with FilmCraft(project=d / "project.fcproj") as fc:
        seqs = {c["name"]: c["item"] for c in fc.inspect()["project"]["root"]["children"] if c["type"] == "Sequence"}
        for target in ("short", "short2", "long"):
            if target not in seqs or (args.only and args.only != target):
                continue
            view = plan.part(target) if target != "long" else plan
            b = Builder(fc, view, d, name=target)
            c = cfg["long" if target == "long" else "short"]
            preset = "YouTube 1080p Full HD" if target == "long" else "Social Vertical 1080×1920"
            out = d / "out" / f"{target}.mp4"
            print(f"▶ {rel(out)}")
            b.export(seqs[target], out, preset, c["video_kbps"], c["audio_kbps"], c["fps"])
            blocked = music.blocked_platforms(view.music)
            if target != "long" and blocked:
                nb = d / "out" / f"{target}_nobgm.mp4"
                print(f"▶ {rel(nb)} (BGM 없음)")
                b.export_without_music(seqs[target], nb, preset, c["video_kbps"], c["audio_kbps"], c["fps"])
                bj = d / "build.json"
                if bj.exists():
                    m = json.loads(bj.read_text(encoding="utf-8"))
                    m["outputs"].setdefault(target, {}).update({"file_nobgm": rel(nb), "nobgm_for": blocked})
                    bj.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_thumbnail(args: argparse.Namespace) -> bool:
    """썸네일만 다시 만든다 (plan.json 의 thumbnail 을 고친 뒤). 롱폼 A·B·C + 쇼츠 커버, 만든 뒤 바로 검사."""
    from . import thumbs
    from .build import COVER
    from .plan import Plan

    d = ep_dir(args.ep)
    plan = Plan.load(d)
    plan.fill_ends()
    made = []
    if plan.outputs.get("long"):
        made += thumbs.render_long(plan, d / "out")
    for target in plan.shorts():
        thumbs.render(plan.part(target), "cover", d / "out" / COVER[target])
        made.append(d / "out" / COVER[target])
    ok = True
    for img in made:
        e, w = thumbs.problems(img)
        ok &= not e
        print(f"{'✓' if not e else '✗'} {rel(img)}")
        for x in e + w:
            print(f"    {'✗' if x in e else '!'} {x}")
    return ok


def cmd_qa(args: argparse.Namespace) -> bool:
    from . import qa

    errors, warnings = qa.full_check(ep_dir(args.ep))
    for w in warnings:
        print(f"    ! {w}")
    for e in errors:
        print(f"    ✗ {e}")
    print("  ✓ QA 통과 (쇼츠·롱폼: 규격·음량·멈춘 화면·자막 겹침/잘림)" if not errors else f"  ✗ QA 실패 {len(errors)}건")
    return not errors


def cmd_publish(args: argparse.Namespace) -> None:
    from . import qa, schedule
    from .plan import Plan, validate
    from .publish import TARGETS, publisher_for, record, video_of

    d = ep_dir(args.ep)
    plan = Plan.load(d)
    manifest = json.loads((d / "build.json").read_text(encoding="utf-8"))
    days = schedule.assign(plan)  # 쇼츠 매일·롱폼 이틀에 한 번 (업로드방법 txt 첫 줄 = 올릴 날짜)
    print("▶ 업로드 일정: " + " · ".join(f"{TARGET_NAMES[v]} {schedule.label(day)}" for v, day in
                                         sorted(days.items(), key=lambda x: (x[1], x[0]))))
    meta = metadata.build_metadata(plan, manifest)
    targets = args.target or [t for t in TARGETS if t in meta]
    public_auto = any(config.load()["publish"].get(t) == "instagram_api" for t in targets)
    report = validate(plan, for_package=True, require_review=public_auto)
    report.print()
    if not report.ok:
        sys.exit("업로드 전 plan.json 을 정리하세요")
    meta = metadata.write(d, plan)
    from . import compliance

    c_err, c_warn = compliance.check(meta, manifest, plan)
    for w in c_warn:
        print(f"  ! 규정 {w}")
    if c_err:
        sys.exit("YouTube 규정·한도 위반 → 고친 뒤 다시:\n  " + "\n  ".join(c_err))
    if not args.skip_qa:
        print("▶ QA (쇼츠·롱폼 전체 검수)")
        errors, warnings = qa.full_check(d, sorted({video_of(x) for x in targets}))
        for w in warnings:
            print(f"    ! {w}")
        if errors:
            sys.exit("QA 실패 → 고친 뒤 다시 build:\n  " + "\n  ".join(errors))
    from .publish.manual import ManualPublisher

    today = date.today().isoformat()
    from .publish import load_state

    done = load_state(d)
    for target in targets:
        if (done.get(target) or {}).get("status") == "uploaded" and not args.target:
            print(f"▶ {target}: 이미 올라간 영상 → 건너뜀 (기록 유지)")
            continue
        pub = publisher_for(target, d, plan.id)
        if pub.name in IMMEDIATE_PUBLIC and meta[target].get("upload_date", today) > today:
            # 바로 '공개' 게시하는 업로더는 올릴 날까지 기다린다 (scripts/auto.sh 의 publish-due 가 그날 게시)
            print(f"▶ {target}: {meta[target]['upload_date']} 에 자동 게시 예정 → 지금은 폴더만 준비")
            pub = ManualPublisher(d, plan.id)
        print(f"▶ {target} ← {pub.name}")
        try:
            result = pub.publish(target, meta[target])
        except Exception as e:  # 자격 증명 미준비·토큰 만료·쿼터 초과 등 → 폴더 방식으로 대체 (무인 실행이 멈추지 않게)
            if pub.name == "manual":
                raise
            print(f"  ! {pub.name} 사용 불가: {e}\n  → 업로드 폴더에 수동 업로드용으로 준비합니다")
            result = ManualPublisher(d, plan.id).publish(target, meta[target])
        record(d, plan.id, result)
        print(f"  {result.status}  {result.url or ''} {result.detail}")
    from . import series

    series.write()
    deliver.resync()
    board = schedule.write_board()
    od = deliver.order_dir()
    if od:
        print("\n업로드 폴더 (외장 SSD, 회차 폴더 이름순 = 올릴 순서): " + str(od))
        for video in ("short", "short2", "long"):
            if video in schedule.load().get(plan.id, {}):
                print(f"  {deliver.order_path(plan.id, video)}")
    else:
        print(f"\n업로드 폴더: {rel(deliver.root() / plan.id)}/숏폼1·숏폼2·롱폼")
    if board:
        print(f"  (날짜별 할 일: {board})")


IMMEDIATE_PUBLIC = {"instagram_api"}  # 비공개 단계 없이 바로 공개되는 업로더


def cmd_publish_due(_: argparse.Namespace) -> None:
    """올릴 날이 된 영상을 바로 공개 업로더(instagram_api 등)로 게시 (scripts/auto.sh 가 매일 부름)."""
    from .plan import Plan
    from .publish import TARGETS, load_state, publisher_for, record

    today = date.today().isoformat()
    done = 0
    for d in sorted(EPISODES.glob("*/metadata.json")):
        d = d.parent
        if d.name.startswith("_"):
            continue
        meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
        state = load_state(d)
        plan = Plan.load(d)
        for target in TARGETS:
            m = meta.get(target)
            if not m or state.get(target, {}).get("status") in ("uploaded", "draft"):
                continue
            pub = publisher_for(target, d, plan.id)
            if pub.name not in IMMEDIATE_PUBLIC or m.get("upload_date", today) > today:
                continue
            if not plan.safety_reviewed:
                print(f"  ! {d.name} {target}: safety_reviewed=false → 자동 공개 게시 안 함")
                continue
            result = pub.publish(target, m)
            record(d, plan.id, result)
            print(f"  {d.name} {target}: {result.status} {result.url or ''}")
            done += 1
    print(f"✓ 오늘 게시 {done}건")


def cmd_published(args: argparse.Namespace) -> None:
    from .publish import PublishResult, TARGETS, record

    if args.target not in TARGETS:
        sys.exit(f"target 은 {TARGETS}")
    d = ep_dir(args.ep)
    record(d, d.name, PublishResult(target=args.target, status="uploaded", url=args.url, detail="manual"))
    print("✓ 기록됨")


def cmd_compile(args: argparse.Namespace) -> None:
    """여러 쇼츠 에피소드의 클립을 이어 붙인 롱폼 몰아보기 plan 을 만든다."""
    from .plan import Plan

    d = args.date or date.today().isoformat()
    ep_id = f"{d}_{args.slug}"
    clips, cast, nos = [], [], []
    for name in args.episodes:
        src = Plan.load(ep_dir(name))
        cast += [c for c in src.cast if c not in cast]
        if src.episode_no:
            nos.append(src.episode_no)
        first = True
        for c in [c for c in src.clips if {"short", "short2"} & set(c.targets)]:
            c = Plan.from_dict(asdict(src)).clips[src.clips.index(c)]
            c.targets = ["long"]
            c.chapter = (" ".join(src.title_lines) or src.youtube_title or src.concept) if first else None
            first = False
            clips.append(asdict(c))
    plan = {
        **TEMPLATE, "id": ep_id, "date": d, "concept": f"몰아보기: {', '.join(args.episodes)}",
        "clips": clips, "title_lines": [], "outputs": {"short": False, "long": True}, "short2": None,
        "music": {"track": "wallpaper", "cuts": []}, "cast": cast, "series": "observe",
        "youtube_long_title": "",
        "description": (f"육아일지 No.{min(nos)}~{max(nos)} 몰아보기" if nos else ""),
    }
    path = EPISODES / ep_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✓ {rel(path / 'plan.json')}  클립 {len(clips)}개 (제목·썸네일·설명을 채운 뒤 build)")


def cmd_status(_: argparse.Namespace) -> None:
    from .publish import load_state

    if not EPISODES.exists():
        return
    for d in sorted(p for p in EPISODES.iterdir() if (p / "plan.json").is_dir() is False and (p / "plan.json").exists()):
        plan = json.loads((d / "plan.json").read_text(encoding="utf-8"))
        built = (d / "build.json").exists()
        pub = load_state(d)
        stage = "기획" if not plan.get("clips") else "조립됨" if built else "기획완료"
        if pub:
            stage = ", ".join(f"{t}:{v['status']}" for t, v in pub.items())
        print(f"  {d.name:40s} {stage:30s} {plan.get('youtube_title', '')}")


def cmd_schedule(args: argparse.Namespace) -> None:
    """업로드 일정 (쇼츠 매일 · 롱폼 이틀에 한 번).
    sync: 채널에서 실제 공개된 영상을 읽어 그 날짜로 고정하고, 남은 영상은 다음 날부터 규칙대로 다시 잡은 뒤
          업로드방법 txt·metadata 의 날짜를 맞춘다 (--dry-run 은 바꿀 내용만 보여 줌)."""
    from . import schedule

    today = date.today()
    if getattr(args, "action", None) == "sync":
        from . import channel, deliver, metadata, stats
        from .plan import Plan

        found, unmatched = channel.sync(dry_run=args.dry_run)
        print(f"▶ 채널 공개 영상 {len(found) + len(unmatched)}개 · 회차와 맞춘 것 {len(found)}개")
        if stats.LAST:
            print(f"  공개 지표 기록 → {rel(stats.HISTORY)} · 분석 → {rel(stats.DOC)}")
            for a in stats.alerts(stats.LAST):
                print(f"  ! 롱폼 연결 확인: {a}")
        for v in unmatched:
            print(f"  ? 회차를 못 찾음: {v['published']:%m-%d %H:%M} {v['title'][:40]}")
        published = {k: v["published"].date() for k, v in found.items()}
        if args.check:  # 매일 자동 실행용: 공개 링크만 기록하고, 날짜가 어긋난 것은 알리기만 (예약해 둔 영상이 있을 수 있어 옮기지 않음)
            state = schedule.load()
            off = [(ep, vid, state.get(ep, {}).get(vid), d) for (ep, vid), d in published.items()
                   if state.get(ep, {}).get(vid) != d.isoformat()]
            for ep, vid, want, got in off:
                print(f"  ! 일정과 다름: {ep} {TARGET_NAMES[vid]} 일정 {want} · 실제 공개 {got}")
            print("  → 맞추려면: ./br schedule sync" if off else "  ✓ 공개된 영상이 모두 일정대로입니다")
            return
        last_short = max((d for (ep, vid), d in published.items() if vid != "long"), default=None)
        start = date.fromisoformat(args.start) if args.start else \
            max(today, last_short + timedelta(days=1)) if last_short else today
        print(f"▶ 남은 영상은 {schedule.label(start)} 부터 다시 잡습니다")
        if args.dry_run:
            print("  (--dry-run: 바꾸지 않음)")
            return
        changes = schedule.rebase(start, published)
        for ep, vid, old, new in sorted(changes, key=lambda c: c[3]):
            print(f"  {TARGET_NAMES[vid]:6s} {old or '-':10s} → {new}  {ep}")
        for ep in sorted({c[0] for c in changes}):
            d = config.EPISODES / ep
            if (d / "build.json").exists():
                metadata.write(d, Plan.load(d))  # upload_date·설명의 롱폼 링크 갱신
        fixed = deliver.resync()
        print(f"✓ 일정 {len(changes)}개 · 업로드 폴더·업로드방법 날짜 {len(fixed)}개 고침")
    else:
        from . import deliver

        for f in deliver.resync(dry_run=args.dry_run):  # 날짜순 폴더 이름·업로드방법 txt 의 '올릴 날'을 지금 일정에 맞춤
            print(f"  고침: {f}")
    rows = schedule.upcoming(today - timedelta(days=3), 21)
    if not rows:
        print("  잡힌 일정이 없습니다 (publish 할 때 자동으로 잡힙니다)")
    for day, ep, video, title in rows:
        mark = "  ✓ 올림" if schedule.uploaded(ep, video) else "  ← 오늘" if day == today else ""
        print(f"  {schedule.label(day)}  {deliver.LABELS[video]:5s} {title[:40]}{mark}")
    s, l = schedule.stock(today)
    print(f"  재고: 쇼츠 {s}개 · 롱폼 {l}개" + ("  (쇼츠 재고가 2개 이하 → 촬영 폴더에 새 영상을 넣어 주세요)" if s <= 2 else ""))
    board = schedule.write_board(today)
    if board:
        print(f"  → {board}")


def cmd_today(args: argparse.Namespace) -> None:
    """오늘 올릴 영상. --notify 면 Mac 알림 (scripts/auto.sh 가 매일 부름)."""
    import subprocess

    from . import schedule

    text = schedule.today_text()
    schedule.write_board()
    print(text)
    if args.notify and not os.environ.get("BABYREEL_QUIET"):
        msg = text.replace('"', "'")
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "<채널 이름> · 오늘 업로드"'],
                       capture_output=True)
        from . import telegram_bot

        telegram_bot.send("📅 " + text)  # 텔레그램을 연결했으면 폰으로도


def cmd_telegram(args: argparse.Namespace) -> None:
    """텔레그램 봇: setup <토큰> | run | send <문장> | test."""
    from . import telegram_bot as tg

    if args.action == "setup":
        if not args.text:
            sys.exit("사용법: ./br telegram setup <BotFather 가 준 토큰>")
        tg.setup(args.text[0])
    elif args.action == "run":
        tg.run()
    elif args.action == "send":
        tg.send(" ".join(args.text))
    elif args.action == "test":
        ok = tg.send("테스트 메시지예요. 봇이 잘 연결돼 있어요 👍")
        print("✓ 보냈어요" if ok else "✗ 설정이 없거나 전송 실패 (./br telegram setup <토큰>)")


def cmd_listen(args: argparse.Namespace) -> None:
    """받아쓰기를 '진짜 대사 / 의심(환각)'으로 나눠 보여 준다 (소리 활용 기획용)."""
    from . import transcribe as stt

    src = Path(args.video).resolve()
    end = args.end if args.end is not None else None
    for x in stt.speech_lines(src, args.start or 0.0, end):
        mark = "✓" if x["real"] else "·"
        why = "" if x["real"] else f"  ({', '.join(x['why'])})"
        print(f"  {mark} {x['start']:6.1f}~{x['end']:6.1f}s  {x['level_db']:6.1f}dB  {x['text']}{why}")


def cmd_insights(args: argparse.Namespace) -> bool:
    """YouTube 성과 수집 → docs/INSIGHTS.md (공개 점검·수익 창출 진행·이탈 지점·편집 패턴)."""
    from . import insights

    if args.action == "key":
        if not args.value:
            sys.exit("사용법: ./br insights key <API 키>")
        insights.SECRETS.mkdir(exist_ok=True)
        insights.API_KEY.write_text(args.value.strip() + "\n")
        insights.API_KEY.chmod(0o600)
        try:
            info, videos = insights.fetch_videos(insights.key_service(),
                                                 handle=config.load()["channel"].get("handle", ""))
        except Exception as e:  # noqa: BLE001
            print(f"✗ API 키 확인 실패: {e}\n  → API 키가 맞는지, YouTube Data API v3 를 '사용'으로 켰는지 확인")
            return False
        print(f"✓ API 키 확인: 채널 '{info['title']}' · 공개 영상 {len(videos)}개 · 구독자 {info['subscribers']}")
        return True
    if args.action == "login":
        print("브라우저에서 Google 로그인 창이 열립니다.\n"
              "  1) 테스트 사용자로 추가한 Gmail(채널을 관리하는 계정) 선택\n"
              "  2) '확인되지 않은 앱' 경고가 나오면 '계속'(또는 고급 → 이동)\n"
              "  3) 권한 체크박스 모두 선택 → 계속\n"
              "  '액세스 차단됨'이면 테스트 사용자에 그 Gmail 이 없는 것 (docs/AUTOMATION.md 2-3)")
        insights.TOKEN.unlink(missing_ok=True)
        insights.services(interactive=True)
        insights.TOKEN.chmod(0o600)
        insights.LOGIN.write_text(date.today().isoformat() + "\n")
        print(f"✓ 로그인 완료 → {insights.TOKEN}  (테스트 상태 앱은 7일마다 다시 로그인 필요, 5~6일째에 미리 알림)")
        return True
    if args.action == "offline":
        snaps = sorted(insights.OUT_DIR.glob("*.json"))
        if not snaps:
            sys.exit("저장된 스냅샷이 없습니다 (./br insights 먼저)")
        r = json.loads(snaps[-1].read_text(encoding="utf-8"))
        insights.write_doc(r)
    else:
        if not insights.configured():
            print(f"  · YouTube 성과 분석 미설정 → docs/AUTOMATION.md 2-3 (간단: {insights.API_KEY.name} / 전체: {insights.CLIENT.name})")
            return True
        try:
            r = insights.collect(interactive=not args.notify)
        except Exception as e:  # noqa: BLE001  (네트워크·토큰 만료 등: 자동 실행이 멈추지 않게)
            print(f"  ! 성과 수집 실패: {e}")
            return False
    print(insights.summary_text(r))
    print(f"  → {rel(insights.DOC)}")
    if args.notify and (r["alerts"] or date.today().weekday() == 0):
        from . import telegram_bot

        telegram_bot.send(insights.summary_text(r))  # 경고가 있거나 월요일(주간 요약)이면 폰으로
    return True


def cmd_voice(args: argparse.Namespace) -> bool:
    """아빠 목소리 내레이션: enroll(녹음 등록) · test(문장 읽어 보기) · status."""
    import subprocess

    from . import kotext, voice

    if args.action == "enroll":
        if not args.args:
            print("사용법: ./br voice enroll <녹음 파일(m4a·wav·mov)>  (조용한 곳에서 10~20초 또박또박)")
            return False
        info = voice.enroll(Path(args.args[0]).expanduser(), args.name)
        print(f"✓ {args.name} 목소리 등록: {info['seconds']}초 · 음량 {info['level_db']}dB · 배경 소음 {info['noise_db']}dB")
        print(f"  녹음 내용(ref.txt): {info['text']}")
        print(f"  → {rel(voice.VOICES / args.name)}/ref.txt 가 실제로 말한 내용과 다르면 고쳐 주세요 (따라 읽기 정확도)")
        if info["level_db"] < -35 or info["noise_db"] > -45:
            print("  ! 소리가 작거나 주변 소음이 큽니다. 조용한 방에서 폰을 입에서 20~30cm 두고 다시 녹음하면 더 닮게 나옵니다")
        ok, why = voice.clone_ready(args.name)
        print(("  ✓ " if ok else "  ! ") + why)
        return ok
    if args.action == "test":
        text = " ".join(args.args) or "<샘플 문장은 비공개 설정에서 읽음>"
        print(f"  읽는 말: {kotext.spoken(text)}")
        wav, vc = voice.speak(text, args.voice)
        print(f"✓ {rel(wav)}  ({vc['engine']}, {voice.duration(wav):.1f}초)")
        if args.play:
            subprocess.run(["afplay", str(wav)])
        return True
    ok, why = voice.clone_ready(args.name)
    print(f"  내레이션: {'아빠 목소리 ' if ok else '기본 AI 음성 '}({why})")
    for name in ("narrator", "아빠"):
        try:
            print(f"  [voices.{name}] → {voice.resolve_engine(voice.voice_config(name))}")
        except KeyError:
            pass
    return True


def cmd_challenge(args: argparse.Namespace) -> bool:
    """댓글 챌린지: status | collect | list | add "글" | check "글" | new <번호> [--days N] | skip <번호>."""
    from . import challenges as chg

    a, rest = args.action, args.args
    if a == "status":
        lag, ep = chg.story_lag()
        ph = chg.phase()
        name = {"archive": "과거 영상 올리는 중 (챌린지 안 함)", "warmup": "곧 따라잡음 (댓글로 아이디어 미리 받는 중)",
                "live": "실시간 (챌린지 진행)"}[ph]
        print(f"  단계: {name}")
        if lag is not None:
            cfg = config.load().get("challenge", {})
            print(f"  가장 최근 이야기: {ep} → 오늘보다 {lag}일 전 "
                  f"(warmup {cfg.get('warmup_within_days', 75)}일 / live {cfg.get('live_within_days', 21)}일 안이면 시작)")
        for c in config.children():
            m = chg.months_old(c["nick"])
            if m is not None:
                print(f"  {c['nick']} 지금 {m}개월 · 추천 템플릿: " + " / ".join(chg.templates_for(m)[:3]))
        ok = chg.ranked()
        print(f"  아이디어: 할 수 있음 {len(ok)}개 · 전체 {len(chg.load())}개 (./br challenge list)")
        line = chg.cta()
        print("  설명란 문구: " + (line.replace("\n", " ") if line else "(아직 안 붙임)"))
        return True
    if a == "collect":
        yt = chg.youtube_client()
        if yt is None:
            print("YouTube 연결이 없습니다 → ./br insights key <API 키> (docs/AUTOMATION.md 2-3). 텔레그램 '챌린지: …' 로도 받을 수 있어요")
            return False
        ids = chg.channel_video_ids(yt)
        n = chg.collect_comments(yt, ids)
        print(f"✓ 영상 {len(ids)}개 댓글에서 아이디어 {n}개 추가 (./br challenge list)")
        return True
    if a in ("add", "check"):
        text = " ".join(rest).strip()
        if not text:
            print('사용법: ./br challenge add "하루 동안 간식 대신 밥만 먹기"')
            return False
        if a == "check":
            child = chg.default_child(text)
            v = chg.screen(text, chg.months_old(child))
            print(("✓ 가능" if v.ok else "✗ 안 됨") + f" ({child} {chg.months_old(child)}개월)")
            for r in v.reasons + v.notes:
                print(f"   - {r}")
            return v.ok
        idea = chg.add(text, "manual")
        if idea is None:
            print("이미 있는 아이디어입니다")
            return True
        print(f"#{idea.id} " + ("✓ 가능" if idea.status == "ok" else "✗ 안 됨: " + " / ".join(idea.reasons)))
        return True
    if a == "list":
        ideas = chg.load()
        if not ideas:
            print("아직 아이디어가 없습니다 (./br challenge collect · add)")
        for i in sorted(ideas, key=lambda i: (i.status != "ok", -i.likes, i.id)):
            mark = {"ok": "✓", "blocked": "✗", "planned": "▶", "done": "●", "skip": "·"}.get(i.status, "?")
            who = f" · {i.author}" if i.author else ""
            why = f"  ({' / '.join(i.reasons)})" if i.reasons else ""
            print(f"  {mark} #{i.id:<3} [{i.child} ♥{i.likes}{who}] {i.text[:60]}{why}")
        return True
    if a == "skip":
        ideas = chg.load()
        for i in ideas:
            if rest and str(i.id) == rest[0]:
                i.status = "skip"
        chg.save(ideas)
        return True
    if a == "new":
        ideas = chg.load()
        idea = next((i for i in ideas if rest and str(i.id) == rest[0]), None)
        if not idea:
            print("사용법: ./br challenge new <번호> (./br challenge list 의 #번호)")
            return False
        if idea.status not in ("ok", "planned"):
            print(f"#{idea.id} 은 진행할 수 없는 아이디어입니다: {' / '.join(idea.reasons) or idea.status}")
            return False
        if chg.phase() == "archive" and not args.force:
            print("아직 과거 영상을 올리는 중입니다 (./br challenge status). 그래도 만들려면 --force")
            return False
        d = args.date or date.today().isoformat()
        slug = f"challenge-{idea.id}"
        ns = argparse.Namespace(slug=slug, date=d, series="challenge", cast=[idea.child], concept=idea.text)
        cmd_new(ns)
        path = EPISODES / f"{d}_{slug}" / "plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        plan["challenge"] = {"idea": idea.id, "text": idea.text, "author": idea.author, "source": idea.source,
                             "child": idea.child, "days": args.days}
        title = chg.title_for(idea, args.days)
        plan["youtube_long_title"] = f"{title} | " + config.load()["channel"]["name"] + " 댓글 챌린지"
        plan["story_notes"] = f"댓글 챌린지 #{idea.id}: {idea.text}" + (f" (제안: {idea.author})" if idea.author else "")
        plan["notes"] = "구성 규칙은 비공개 기획 문서 참고. 주의: " + " / ".join(idea.notes)
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        idea.status, idea.episode = "planned", path.parent.name
        chg.save(ideas)
        print(f"  제목 초안: {title}")
        return True
    return False


def cmd_series(_: argparse.Namespace) -> None:
    from . import series

    print(f"✓ {rel(series.write())}")


def cmd_render(args: argparse.Namespace) -> bool:
    """다른 채널 프로필로 세로 영상 한 편 (외부 렌더 작업자). babyreel/render.py"""
    from .render import RenderError, render

    try:
        m = render(Path(args.job_dir), Path(args.profile), export=not args.no_export, verbose=args.verbose)
    except RenderError as e:
        print(f"✗ {e}")
        return False
    print(json.dumps({k: m[k] for k in ("video", "cover", "duration", "qa")}, ensure_ascii=False))
    return True


def cmd_selftest(_: argparse.Namespace) -> bool:
    import subprocess

    return subprocess.run([sys.executable, str(ROOT / "scripts" / "selftest.py")], cwd=ROOT).returncode == 0


def cmd_music(args: argparse.Namespace) -> bool:
    """BGM 목록(상황별) · suggest <상황> [--energy 1~3]: 최근 편에서 안 쓴 곡 위주로 추천."""
    recent = music.recent_tracks()
    if args.action == "suggest":
        tag = args.tag or ""
        if tag not in music.SITUATIONS:
            print(f"상황 태그: {', '.join(music.SITUATIONS)}")
            return False
        print(f"'{tag}' ({music.SITUATIONS[tag]}) 추천 (최근 사용: {', '.join(recent[:4]) or '없음'})")
        for e in music.suggest(tag, recent, args.energy, n=5):
            used = " (최근 사용)" if e["id"] in recent else ""
            print(f"  {e['id']:32s} 에너지 {e.get('energy', 2)}  {e['note']}{used}")
        return True
    entries = music.load_catalog()
    for tag, desc in music.SITUATIONS.items():
        rows = [e for e in entries if e.get("situations", [""])[0] == tag]
        if not rows:
            continue
        print(f"[{tag}] {desc}")
        for e in rows:
            have = "✓" if config.resolve(e["file"]).exists() else "·"
            used = " (최근 사용)" if e["id"] in recent else ""
            print(f"  {have} {e['id']:32s} 에너지 {e.get('energy', 2)}  {e['note']}{used}")
    print(f"\n총 {len(entries)}곡 (모두 Kevin MacLeod, CC BY 4.0 · 크레딧 자동). 장면마다 바꾸기: clips[].music = \"곡 id\"")
    print("효과음:", ", ".join(f"{k}({v.split(' - ')[0]})" for k, v in sfx.CATALOG.items()))
    return True


def main() -> None:
    p = argparse.ArgumentParser(prog="babyreel", description="아기 쇼츠/릴스 제작 파이프라인 (FilmCraft MCP)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("doctor")
    sub.add_parser("ingest")
    s = sub.add_parser("sheet"); s.add_argument("files", nargs="+")
    s = sub.add_parser("new"); s.add_argument("slug"); s.add_argument("--date"); s.add_argument("--concept")
    s.add_argument("--series", default="observe", choices=list(config.load().get("series", {})))
    s.add_argument("--cast", nargs="*", help="등장하는 아이 애칭 (예: 첫째 둘째)")
    s = sub.add_parser("validate"); s.add_argument("ep"); s.add_argument("--package", action="store_true")
    s = sub.add_parser("build"); s.add_argument("ep"); s.add_argument("--only", choices=["short", "short2", "long"])
    s.add_argument("--no-export", action="store_true"); s.add_argument("-v", "--verbose", action="store_true")
    s = sub.add_parser("export"); s.add_argument("ep"); s.add_argument("--only", choices=["short", "short2", "long"])
    s = sub.add_parser("qa"); s.add_argument("ep")
    s = sub.add_parser("thumbnail"); s.add_argument("ep")
    s = sub.add_parser("publish"); s.add_argument("ep"); s.add_argument("--target", action="append")
    s.add_argument("--skip-qa", action="store_true")
    s = sub.add_parser("published"); s.add_argument("ep"); s.add_argument("target"); s.add_argument("url")
    s = sub.add_parser("compile"); s.add_argument("slug"); s.add_argument("episodes", nargs="+"); s.add_argument("--date")
    sub.add_parser("status")
    s = sub.add_parser("music"); s.add_argument("action", nargs="?", default="list", choices=["list", "suggest"])
    s.add_argument("tag", nargs="?"); s.add_argument("--energy", type=int, choices=[1, 2, 3])
    sub.add_parser("selftest")
    sub.add_parser("series")
    s = sub.add_parser("listen"); s.add_argument("video"); s.add_argument("start", nargs="?", type=float)
    s.add_argument("end", nargs="?", type=float)
    sp = sub.add_parser("schedule")
    sp.add_argument("action", nargs="?", choices=["sync"])
    sp.add_argument("--start", help="남은 영상을 이 날짜부터 (기본: 마지막으로 공개된 쇼츠 다음 날)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--check", action="store_true", help="공개 링크 기록 + 어긋난 날짜 알림만 (옮기지 않음)")
    s = sub.add_parser("challenge"); s.add_argument("action", nargs="?", default="status",
                                                     choices=["status", "collect", "list", "add", "check", "new", "skip"])
    s.add_argument("args", nargs="*"); s.add_argument("--days", type=int, default=1); s.add_argument("--date")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("voice"); s.add_argument("action", nargs="?", default="status", choices=["enroll", "test", "status"])
    s.add_argument("args", nargs="*"); s.add_argument("--name", default="아빠"); s.add_argument("--voice", default="narrator")
    s.add_argument("--play", action="store_true")
    s = sub.add_parser("insights"); s.add_argument("action", nargs="?", choices=["collect", "login", "key", "offline"],
                                                   default="collect")
    s.add_argument("value", nargs="?")
    s.add_argument("--notify", action="store_true")
    sub.add_parser("publish-due")
    s = sub.add_parser("today"); s.add_argument("--notify", action="store_true")
    s = sub.add_parser("telegram"); s.add_argument("action", choices=["setup", "run", "send", "test"])
    s.add_argument("text", nargs="*")
    s = sub.add_parser("render"); s.add_argument("job_dir"); s.add_argument("--profile", required=True)
    s.add_argument("--no-export", action="store_true"); s.add_argument("-v", "--verbose", action="store_true")
    s = sub.add_parser("storage"); s.add_argument("action", nargs="?", default="status",
                                                   choices=["status", "migrate", "archive", "evict", "backup"])
    s.add_argument("--days", type=int, help="archive: 올릴 날짜가 이만큼 지난 영상 (기본 [storage] archive_after_days)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--allow-slow", action="store_true", help="USB 2.0 연결이어도 옮기기 (권장하지 않음)")
    args = p.parse_args()
    from . import storage

    try:
        _ssd_guard(args)
        result = globals()[f"cmd_{args.cmd.replace('-', '_')}"](args)
    except storage.StorageError as e:
        sys.exit(f"✗ {e}")
    if result is False:
        sys.exit(1)


# 외장 SSD 가 필요한 명령 (무거운 원본·완성 영상을 읽고 쓴다). '_' 로 시작하는 테스트 회차는 로컬
NEEDS_SSD = {"ingest": "촬영 영상 정리", "sheet": "컨택트시트", "listen": "받아쓰기 보기", "build": "편집",
             "export": "내보내기", "qa": "검수", "thumbnail": "썸네일", "publish": "업로드 폴더 만들기"}


def _ssd_guard(args: argparse.Namespace) -> None:
    """무거운 작업 전: SSD 연결·쓰기·빈 공간 점검(안 되면 아무것도 바꾸지 않고 멈춤) + 작업 중 잠자기 방지 +
    이 회차의 out/ 을 SSD 에 준비 (babyreel/storage.py)."""
    from . import storage

    what = NEEDS_SSD.get(args.cmd)
    ep = getattr(args, "ep", None)
    if not what or not storage.enabled() or (ep and Path(ep).name.startswith("_")):
        return
    storage.require(what, need_gb=5 if args.cmd in ("build", "export") else 0)
    storage.keep_awake()
    if ep and args.cmd in ("build", "export", "thumbnail", "qa", "publish"):
        storage.out_dir(ep_dir(ep))


def cmd_storage(args: argparse.Namespace) -> bool:
    """외장 SSD: status(상태) · migrate(내장 → SSD, 검증 복사) · archive(이미 올린 전달 영상 → SSD 보관함)
    · evict(올릴 전달 영상의 Mac 사본 내리기) · backup."""
    from . import storage

    if args.action == "status":
        print("\n".join(storage.status_lines()))
        return True
    if args.action == "migrate":
        plan = storage.migrate(dry_run=args.dry_run, allow_slow=args.allow_slow)
        if not plan:
            print("✓ 옮길 것 없음 (무거운 폴더는 모두 SSD 에 있음)")
        elif args.dry_run:
            print(f"옮길 폴더 {len(plan)}개 · {sum(x[2] for x in plan) / 1e9:.1f}GB (--dry-run: 옮기지 않음)")
            for a, b, size in plan:
                print(f"  {rel(a)} ({size / 1e9:.1f}GB) → {b}")
        else:
            print(f"✓ {len(plan)}개 폴더를 SSD 로 옮김 · 내장 디스크 빈 공간 {shutil.disk_usage(ROOT).free / 1e9:.0f}GB")
        return True
    if args.action == "archive":
        moved = storage.archive_delivered(days=args.days, dry_run=args.dry_run)
        size = sum(f.stat().st_size for f, _ in moved) / 1e9 if args.dry_run and moved else 0
        print(f"옮길: 이미 올린 전달 영상 {len(moved)}개 ({size:.1f}GB) → {storage.ssd_root() / 'delivered'}"
              if args.dry_run else f"✓ 이미 올린 전달 영상 {len(moved)}개 → SSD 보관함 (iCloud 에서는 지움)")
        return True
    if args.action == "evict":
        files = storage.evict_delivered(dry_run=args.dry_run)
        size = sum(f.stat().st_size for f in files) / 1e9 if files else 0
        print(f"{'내릴' if args.dry_run else '✓ 내림'}: iCloud 전달 영상 {len(files)}개 ({size:.1f}GB) "
              "— iCloud·폰에는 그대로, Mac 에서 열면 다시 받음")
        return True
    dest = storage.backup()
    print(f"✓ 백업 → {dest}" if dest else "· 백업 건너뜀 (SSD 연결 안 됨)")
    return True


if __name__ == "__main__":
    main()
