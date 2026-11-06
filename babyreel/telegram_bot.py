"""텔레그램으로 영상 제작 요청 받기 (docs/FEEDBACK.md 2026-10-17).

사용 흐름
  1. Mac 의 촬영 폴더(동영상/촬영폴더 = inbox/)에 영상을 넣는다 (AirDrop 등)
  2. 텔레그램 봇에게 "영상 만들어줘: 둘째 첫 뒤집기, 첫째가 옆에서 응원" 처럼 보낸다
  3. Mac 의 이 봇이 설명을 메모로 저장하고 scripts/auto.sh 를 돌린다
     (정리 → Claude 기획 → 쇼츠 1·2부 + 롱폼 조립 → 검수 → 업로드 준비)
  4. 끝나면 결과 요약 + 썸네일 + 올릴 날짜를 텔레그램으로 보낸다

명령 (봇 대화창)
  영상 만들어줘 <설명>   제작 요청 (/make 도 가능)
  오늘                   오늘 올릴 영상 (/today)
  일정                   2주 업로드 일정·재고 (/schedule)
  상태                   지금 만드는 중인지 (/status)
  도움말                 (/help)

설정: secrets/telegram.json {"token": "...", "allowed_chat_ids": [123]}
  ./br telegram setup <봇 토큰>   토큰 저장 + 첫 메시지를 보낸 사람(본인)을 등록
  ./br telegram run               봇 실행 (scripts/install_auto.sh 가 자동 실행 등록)
  ./br telegram send "<문장>"     알림 보내기 (auto.sh·today 가 사용)

비용 없음: 텔레그램 Bot API(무료) + Mac 에서 직접 실행. 표준 라이브러리만 사용.
보안: allowed_chat_ids 에 등록된 대화방의 메시지만 처리한다 (다른 사람이 봇을 찾아도 무시).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime
from pathlib import Path

from .config import DATA, EPISODES, INBOX
from .fc import ROOT

SECRET = ROOT / "secrets" / "telegram.json"
OFFSET = DATA / "telegram_offset"
LOCK = DATA / "auto.lock"
API = "https://api.telegram.org/bot{token}/{method}"
MEDIA_EXT = {".mov", ".mp4", ".m4v", ".heic", ".heif", ".jpg", ".jpeg", ".png"}
MAKE_WORDS = ("만들어", "만들어줘", "제작", "/make")

HELP = """[<채널 이름> 봇]
1) Mac 의 촬영 폴더(동영상 › 촬영폴더)에 영상을 넣고
2) 여기로 '영상 만들어줘: 간단한 설명' 을 보내 주세요
   예) 영상 만들어줘: 둘째 첫 뒤집기 성공, 첫째가 옆에서 박수
→ 쇼츠 2개(오늘·내일) + 롱폼 1개를 만들어 업로드 폴더에 준비하고 결과를 보내 드려요.

아빠 목소리 내레이션: 이 문장을 음성 메시지로 읽어 보내 주세요 (조용한 곳, 평소 말투로)
  "{script}"
→ 등록하고 아빠 목소리로 읽은 샘플을 보내 드려요.

댓글 챌린지 아이디어: '챌린지: 하루 동안 간식 대신 밥만 먹기' (안전·개월 수 자동 검사), '챌린지' 만 보내면 후보 목록

다른 명령: 오늘 · 일정 · 상태 · 도움말"""


# --- 설정 ---------------------------------------------------------------------------------

def load_secret() -> dict:
    try:
        return json.loads(SECRET.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_secret(data: dict) -> None:
    SECRET.parent.mkdir(parents=True, exist_ok=True)
    SECRET.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(SECRET, 0o600)


def configured() -> bool:
    s = load_secret()
    return bool(s.get("token") and s.get("allowed_chat_ids"))


# --- Bot API ------------------------------------------------------------------------------

def call(method: str, token: str | None = None, timeout: float = 70, **params) -> dict:
    token = token or load_secret().get("token", "")
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                   for k, v in params.items()}).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read().decode())
    if not res.get("ok"):
        raise RuntimeError(f"telegram {method}: {res}")
    return res["result"]


def send_photo(chat_id: int, path: Path, caption: str = "") -> None:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in (("chat_id", str(chat_id)), ("caption", caption[:1000])):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{path.name}"\r\n'
                  "Content-Type: image/jpeg\r\n\r\n").encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(API.format(token=load_secret()["token"], method="sendPhoto"), data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        r.read()


def send(text: str, chat_ids: list[int] | None = None) -> bool:
    """등록된 대화방에 메시지. 설정이 없으면 조용히 False (알림은 선택 기능)."""
    s = load_secret()
    if not s.get("token"):
        return False
    ok = False
    for cid in chat_ids or s.get("allowed_chat_ids", []):
        try:
            call("sendMessage", chat_id=cid, text=text[:4000], timeout=20)
            ok = True
        except (OSError, RuntimeError, urllib.error.URLError) as e:
            print(f"  ! 텔레그램 전송 실패: {e}", file=sys.stderr)
    return ok


# --- 요청 처리 ----------------------------------------------------------------------------

def parse(text: str) -> tuple[str, str]:
    """메시지 → (명령, 설명). 명령: make | today | schedule | status | help | unknown"""
    t = text.strip()
    low = t.lower()
    if low.startswith(("/start", "/help")) or t in ("도움말", "help", "?"):
        return "help", ""
    if low.startswith("/today") or t in ("오늘", "오늘 뭐 올려", "오늘 올릴 거"):
        return "today", ""
    if low.startswith("/schedule") or t in ("일정", "업로드 일정"):
        return "schedule", ""
    if low.startswith("/status") or t in ("상태", "진행 상황"):
        return "status", ""
    if re.match(r"^(/challenge|챌린지)\s*[:：]?", t) and not any(w in t for w in MAKE_WORDS):
        return "challenge", re.sub(r"^(/challenge(@\w+)?|챌린지)\s*[:：]?\s*", "", t).strip()
    if any(w in t for w in MAKE_WORDS):
        desc = re.sub(r"^/make(@\w+)?", "", t).strip()
        desc = re.sub(r"^(영상\s*)?(좀\s*)?(만들어\s*줘|만들어\s*주세요|만들어|제작해\s*줘|제작)\s*[:：,.!~]*", "", desc).strip()
        return "make", desc
    return "unknown", t


def inbox_media() -> tuple[list[Path], list[Path]]:
    """(정리 대기 중인 사진·영상, 아직 iCloud 에서 내려받지 않은 파일)."""
    if not INBOX.exists():
        return [], []
    files = [p for p in INBOX.rglob("*") if p.is_file()]
    media = [p for p in files if p.suffix.lower() in MEDIA_EXT and not p.name.startswith(".")]
    pending = [p for p in files if p.name.startswith(".") and p.name.endswith(".icloud")]
    return media, pending


def unused_library() -> int:
    idx = DATA.parent / "library" / "index.json"
    try:
        return sum(1 for v in json.loads(idx.read_text(encoding="utf-8")).values() if not v.get("used_in"))
    except (OSError, ValueError):
        return 0


def wait_for_media(reply, minutes: int = 15) -> int:
    """폰에서 넣은 파일이 Mac 에 내려올 때까지 기다린다 (iCloud 동기화). 반환: 파일 수."""
    told = False
    for _ in range(minutes * 2):
        media, pending = inbox_media()
        for p in pending:  # 'Mac 저장 공간 최적화'로 아직 내려받지 않은 파일
            subprocess.run(["brctl", "download", str(p.with_name(p.name[1:-len(".icloud")]))], capture_output=True)
        if media and not pending:
            return len(media)
        if not told:
            reply("촬영 폴더에서 영상을 기다리는 중이에요 (복사·동기화 중). 최대 "
                  f"{minutes}분 기다릴게요." if not media else f"영상 {len(media)}개 확인, 나머지 내려받는 중…")
            told = True
        time.sleep(30)
    return len(inbox_media()[0])


def run_make(desc: str, reply, chat_id: int) -> None:
    started = datetime.now()
    n = wait_for_media(reply)
    if n == 0 and unused_library() == 0:
        reply("촬영 폴더에 새 영상이 없어요. 폴더에 영상을 넣은 뒤 다시 보내 주세요.\n"
              "(Mac Finder → 동영상 → 촬영폴더)")
        return
    if desc:
        INBOX.mkdir(parents=True, exist_ok=True)
        # 짝이 없는 메모 = 이번에 넣은 모든 파일의 공통 메모 (media.ingest). Claude 가 가장 먼저 읽는다
        (INBOX / f"텔레그램요청_{started:%Y%m%d_%H%M%S}.txt").write_text(desc + "\n", encoding="utf-8")
    while LOCK.exists():
        reply("앞 작업이 끝나면 바로 이어서 만들게요.")
        while LOCK.exists():
            time.sleep(30)
    reply(f"영상 {n}개로 만들기 시작할게요. 보통 20~40분 걸려요.\n(쇼츠 1부·2부 + 롱폼 → 검수 → 업로드 폴더)")
    env = {**os.environ, "BABYREEL_REQUEST": desc, "BABYREEL_SOURCE": "telegram", "BABYREEL_SKIP_WAIT": "1"}
    r = subprocess.run(["/bin/zsh", "-l", str(ROOT / "scripts" / "auto.sh")], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    logs = sorted((ROOT / "logs").glob("auto_*.log"))
    summary = ""
    if logs:
        tail = logs[-1].read_text(encoding="utf-8", errors="replace").strip().splitlines()
        summary = "\n".join(tail[-8:])
    if r.returncode != 0:
        reply(f"만들다가 문제가 생겼어요. Mac 의 로그를 확인해 주세요:\n{logs[-1].name if logs else ''}\n{summary[-600:]}")
        return
    report_results(started, reply, chat_id, summary)


def report_results(since: datetime, reply, chat_id: int, summary: str) -> None:
    """이번에 만든 에피소드: 요약 + 썸네일 사진 + 올릴 날짜."""
    from . import schedule
    from .plan import TARGET_NAMES

    made = []
    for bj in EPISODES.glob("*/build.json"):
        if bj.parent.name.startswith("_"):
            continue
        try:
            built = datetime.fromisoformat(json.loads(bj.read_text(encoding="utf-8"))["built_at"])
        except (OSError, ValueError, KeyError):
            continue
        if built >= since:
            made.append(bj.parent)
    if not made:
        reply("새로 만든 영상이 없어요.\n" + summary[-800:])
        return
    reply("완성했어요! 🎉\n" + summary[-1500:])
    days = schedule.load()
    for d in sorted(made):
        plan = json.loads((d / "plan.json").read_text(encoding="utf-8"))
        when = days.get(d.name, {})
        for video, img in (("short", "cover.jpg"), ("short2", "cover2.jpg"), ("long", "thumbnail.jpg")):
            p = d / "out" / img
            if not p.exists() or not plan.get("outputs", {}).get(video):
                continue
            title = plan.get("youtube_title", "") if video == "short" else (
                (plan.get("short2") or {}).get("youtube_title", "") if video == "short2"
                else plan.get("youtube_long_title") or plan.get("youtube_title", ""))
            day = schedule.label(date.fromisoformat(when[video])) if video in when else "(일정 미정)"
            try:
                send_photo(chat_id, p, f"{TARGET_NAMES[video]} · {day}\n{title}")
            except (OSError, urllib.error.URLError) as e:
                print(f"  ! 사진 전송 실패: {e}", file=sys.stderr)
    reply(schedule.today_text() + "\n업로드: 외장 SSD filmcraft/업로드/ (회차 폴더 이름순 = 올릴 순서, 안에 숏폼1·숏폼2·롱폼)")


def help_text() -> str:
    from .voice import SCRIPT

    return HELP.replace("{script}", SCRIPT)


def download(file_id: str, folder: Path) -> Path:
    info = call("getFile", file_id=file_id, timeout=30)
    url = f"https://api.telegram.org/file/bot{load_secret()['token']}/{info['file_path']}"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{datetime.now():%Y%m%d_%H%M%S}{Path(info['file_path']).suffix or '.ogg'}"
    with urllib.request.urlopen(url, timeout=120) as r:
        out.write_bytes(r.read())
    return out


def send_file(chat_id: int, path: Path, method: str = "sendAudio", field: str = "audio", caption: str = "") -> None:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in (("chat_id", str(chat_id)), ("caption", caption[:1000]))]
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'
                  "Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(API.format(token=load_secret()["token"], method=method), data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        r.read()


def enroll_voice(file_id: str, reply, chat_id: int) -> None:
    """음성 메시지 → 아빠 목소리 등록 → 아빠 목소리로 읽은 샘플을 돌려보낸다."""
    from . import voice

    reply("목소리 받았어요. 등록하고 샘플을 만드는 중이에요 (1분쯤)…")
    try:
        src = download(file_id, DATA / "voice_inbox")
        info = voice.enroll(src, "아빠")
        ok, why = voice.clone_ready("아빠")
        msg = [f"등록 완료: {info['seconds']}초", f"알아들은 내용: {info['text']}"]
        if info["level_db"] < -35 or info["noise_db"] > -45:
            msg.append("소리가 작거나 주변 소음이 있어요. 조용한 곳에서 다시 보내면 더 닮게 나와요.")
        if not ok:
            reply("\n".join(msg + [f"아빠 목소리를 쓸 수 없어요: {why}"]))
            return
        reply("\n".join(msg))
        sample = "<샘플 문장은 비공개 설정에서 읽음>"
        wav, _ = voice.speak(sample, "narrator")
        m4a = wav.with_suffix(".m4a")
        subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(wav), str(m4a)], check=True, capture_output=True)
        send_file(chat_id, m4a, caption="아빠 목소리 샘플. 마음에 안 들면 다시 녹음해 보내 주세요 (새 녹음으로 바뀝니다)")
        m4a.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001 - 봇은 계속 돌아야 한다
        reply(f"목소리 등록 실패: {e}")


def handle(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    text = msg.get("text") or msg.get("caption") or ""

    def reply(t: str) -> None:
        send(t, [chat_id])

    audio = msg.get("voice") or msg.get("audio")
    doc = msg.get("document") or {}
    if not audio and str(doc.get("mime_type", "")).startswith("audio/"):
        audio = doc
    if audio:
        enroll_voice(audio["file_id"], reply, chat_id)
        return

    from . import schedule

    cmd, arg = parse(text)
    if cmd == "help" or cmd == "unknown":
        reply(help_text() if cmd == "help" else "무엇을 할까요? 영상을 만들려면 '영상 만들어줘: 설명' 처럼 보내 주세요.\n\n" + help_text())
    elif cmd == "today":
        reply(schedule.today_text())
    elif cmd == "schedule":
        rows = schedule.upcoming(date.today(), 14)
        from .plan import TARGET_NAMES

        lines = [f"{schedule.label(d)} {TARGET_NAMES[v]} {t[:28]}" for d, _, v, t in rows] or ["잡힌 일정 없음"]
        s, l = schedule.stock()
        reply("\n".join(lines + [f"재고: 쇼츠 {s}개 · 롱폼 {l}개"]))
    elif cmd == "status":
        media, pending = inbox_media()
        reply(("지금 영상을 만드는 중이에요." if LOCK.exists() else "쉬는 중이에요.") +
              f"\n촬영 폴더 대기: {len(media)}개" + (f" (+ 내려받는 중 {len(pending)}개)" if pending else "") +
              f"\n아직 안 쓴 영상: {unused_library()}개")
    elif cmd == "challenge":
        from . import challenges as chg

        if not arg:
            ok = chg.ranked()[:5]
            lines = [f"#{i.id} [{i.child} ♥{i.likes}] {i.text[:40]}" for i in ok] or ["아직 아이디어가 없어요"]
            reply("할 수 있는 챌린지 (좋아요 순)\n" + "\n".join(lines) + f"\n\n지금 단계: {chg.phase()}")
        else:
            idea = chg.add(arg, "telegram")
            if idea is None:
                reply("이미 있는 아이디어예요")
            elif idea.status == "ok":
                reply(f"#{idea.id} 저장 ✓ ({idea.child} {chg.months_old(idea.child)}개월에 가능)"
                      + ("\n주의: " + " / ".join(idea.notes) if idea.notes else ""))
            else:
                reply(f"#{idea.id} 저장했지만 지금은 어려워요: " + " / ".join(idea.reasons))
    elif cmd == "make":
        reply("요청 받았어요: " + (arg or "(설명 없음)"))
        run_make(arg, reply, chat_id)


def run() -> None:
    """롱폴링 루프 (LaunchAgent 가 계속 살려 둔다). 허용된 대화방만 처리."""
    s = load_secret()
    if not s.get("token"):
        sys.exit(f"{SECRET} 가 없습니다. ./br telegram setup <봇 토큰>")
    allowed = set(s.get("allowed_chat_ids", []))
    offset = int(OFFSET.read_text()) if OFFSET.exists() else 0
    print(f"텔레그램 봇 시작 (허용 대화방 {len(allowed)}개)", flush=True)
    while True:
        try:
            updates = call("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
        except (OSError, RuntimeError, urllib.error.URLError) as e:
            print(f"  ! {e}", flush=True)
            time.sleep(15)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            OFFSET.parent.mkdir(parents=True, exist_ok=True)
            OFFSET.write_text(str(offset))
            msg = u.get("message")
            if not msg:
                continue
            if msg["chat"]["id"] not in allowed:
                print(f"  무시: 등록되지 않은 대화방 {msg['chat']['id']}", flush=True)
                continue
            try:
                handle(msg)
            except Exception as e:  # 봇은 죽지 않고 다음 메시지를 받는다
                print(f"  ! 처리 실패: {e}", flush=True)
                send(f"처리 중 오류가 났어요: {e}", [msg["chat"]["id"]])


def setup(token: str, wait_seconds: int = 300) -> None:
    """토큰 저장 → 봇에게 첫 메시지를 보낸 사람(본인)의 대화방을 등록."""
    me = call("getMe", token=token, timeout=20)
    print(f"✓ 봇 확인: @{me['username']}")
    print(f"  이제 텔레그램에서 @{me['username']} 를 열고 '시작'(/start)을 눌러 주세요 ({wait_seconds // 60}분 기다림)")
    deadline = time.time() + wait_seconds
    offset = 0
    while time.time() < deadline:
        for u in call("getUpdates", token=token, offset=offset, timeout=30):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if msg.get("chat", {}).get("type") == "private":
                cid = msg["chat"]["id"]
                who = msg["chat"].get("first_name", "")
                save_secret({"token": token, "allowed_chat_ids": [cid], "bot": me["username"]})
                OFFSET.parent.mkdir(parents=True, exist_ok=True)
                OFFSET.write_text(str(offset))
                call("sendMessage", token=token, chat_id=cid, text="연결됐어요! 👋\n\n" + help_text())
                print(f"✓ 등록 완료: {who} (대화방 {cid}) → {SECRET}")
                return
    sys.exit("시간 안에 메시지가 오지 않았습니다. 다시 실행해 주세요.")
