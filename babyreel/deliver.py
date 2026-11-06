"""업로드 폴더 (2026-11-02 사용자 요청: iCloud 대신 외장 SSD 에서 올림)

영상(쇼츠 1부·2부·롱폼)마다 폴더 하나. 그 영상을 올릴 곳(YouTube·인스타·틱톡)의 파일이 한 폴더에 모인다.
코드는 항상 episodes/<회차>/<숏폼1|숏폼2|롱폼> 으로 찾는다.

SSD 를 쓰면 (2026-11-03 사용자 요청: 올릴 순서와 폴더 이름을 맞춤 + 회차 안 숏폼1·숏폼2·롱폼 구분 유지) 실제 폴더는
SSD 업로드/<회차 폴더>/<영상 폴더>:  '2026-11-04~05 <롱폼 제목>/숏폼1 (11-04 수)' (회차 폴더 이름순 = 올릴 순서,
회차는 겹치지 않고 차례로 올라간다). 올린 영상 폴더는 이름 끝에 '올림', 회차를 다 올리면 업로드/올린 영상/ 안으로.
episodes/<회차>/<구분> 은 그 폴더로 가는 링크. 일정·제목이 바뀌면 resync() 가 폴더를 옮기고 링크를 다시 건다
(같은 디스크 안 이름 바꾸기라 복사 없음).
SSD 가 없으면(selftest 등) episodes/<회차>/<구분> 이 진짜 폴더.

exFAT 은 한글 이름을 NFD 로 돌려준다 → 목록으로 찾은 이름은 NFC 로 바꿔서 쓰고,
폴더 안 파일은 목록(glob)으로 찾지 않고 정해진 이름(NFC)으로만 연다.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import date
from pathlib import Path

from . import config, storage
from .plan import Plan

# 폴더 이름·업로드일정.txt 에 쓰는 영상 구분 (올릴 때 보는 이름)
LABELS = {"short": "숏폼1", "short2": "숏폼2", "long": "롱폼"}
# 업로드방법 txt (첫 줄의 올릴 날을 일정에 맞춰 고친다)
GUIDES = ("유튜브_업로드방법.txt", "인스타_업로드방법.txt", "틱톡_업로드방법.txt")
BOARD = "업로드일정.txt"
UPLOADED = "올린 영상"  # 업로드/ 안: 이미 올린 영상 폴더를 모아 두는 곳
MARK = ".video"  # 날짜순 폴더 안 표시 파일: "<회차>/<short|short2|long>" (링크가 끊겨도 다시 찾는다)


def root() -> Path:
    """회차 폴더들이 있는 곳 (기본 episodes/). 테스트는 BABYREEL_DELIVER_DIR 로 임시 폴더."""
    raw = os.environ.get("BABYREEL_DELIVER_DIR") or config.load().get("deliver", {}).get("dir", "episodes")
    return config.resolve(os.path.expanduser(raw))


def order_dir() -> Path | None:
    """날짜순 업로드 폴더들이 있는 곳: SSD 의 업로드/ (테스트는 BABYREEL_ORDER_DIR). None = 회차 폴더가 진짜 폴더."""
    if os.environ.get("BABYREEL_ORDER_DIR"):
        return Path(os.environ["BABYREEL_ORDER_DIR"])
    if os.environ.get("BABYREEL_DELIVER_DIR") or not storage.enabled():
        return None
    return storage.ssd_root() / "업로드" if root().resolve() == config.EPISODES.resolve() else None


def board_dirs() -> list[Path]:
    """업로드일정.txt 를 둘 곳: 회차 폴더들 맨 위(Mac) + 날짜순 업로드/ 맨 위 + SSD 의 episodes/ 맨 위."""
    dirs = [root()]
    od = order_dir()
    if od and od.parent.is_dir():
        dirs.append(od)
    if root().resolve() == config.EPISODES.resolve() and storage.enabled() and storage.online():
        dirs.append(storage.ssd_root() / "episodes")
    return dirs


def folder(plan: Plan, platform: str, video: str = "short") -> Path:
    """platform: instagram | youtube | tiktok (같은 영상이면 같은 폴더), video: short | short2 | long."""
    ep = root() / plan.id
    od = order_dir()
    if od and not plan.id.startswith("_"):
        if not od.parent.is_dir():
            raise storage.StorageError(f"업로드 폴더를 만들 수 없습니다: {od.parent} 없음 (SSD 연결 확인)")
        arrange(plan.id, video)
        return ep / LABELS[video]
    if ep.resolve() == (config.EPISODES / plan.id).resolve():
        return storage.episode_subdir(ep, LABELS[video])
    path = ep / LABELS[video]
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe(text: str, limit: int = 60) -> str:
    """폴더 이름에 쓸 수 있게: ' | 육아일지 No.N' 같은 꼬리는 빼고, exFAT 금지 문자 제거, 길면 단어 단위로 자름."""
    text = unicodedata.normalize("NFC", text).split("|")[0]
    text = re.sub(r'[:*?"<>]', "", text)
    text = re.sub(r"[\\/\x00-\x1f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text.strip(" .")


def _span(days: list[str]) -> str:
    """회차 폴더 이름의 날짜: '2026-11-04~05' · '2026-10-31~11-01' · '2026-11-04' (없으면 '날짜 미정')."""
    if not days:
        return "날짜 미정"
    a, b = min(days), max(days)
    if a == b:
        return a
    return a + "~" + (b[8:] if a[:7] == b[:7] else b[5:] if a[:4] == b[:4] else b)


def episode_name(ep: str) -> str:
    """회차 폴더 이름: '2026-11-04~05 주말 나들이 기록, …' (이름순 = 올릴 순서, 회차는 겹치지 않고 차례로 올라감)."""
    from . import schedule

    t = schedule.titles(ep)
    title = _safe(t.get("long") or t.get("short") or "", 50) or ep
    days = [d for v, d in schedule.load().get(ep, {}).items() if v in LABELS and d]
    return f"{_span(days)} {title}"


def video_name(ep: str, video: str) -> str:
    """회차 폴더 안 영상 폴더 이름: '숏폼1 (11-04 수)', 올렸으면 '숏폼1 (11-04 수) 올림'."""
    from . import schedule

    day = schedule.load().get(ep, {}).get(video)
    name = LABELS[video]
    if day:
        d = date.fromisoformat(day)
        name += f" ({d.strftime('%m-%d')} {schedule.WEEKDAYS[d.weekday()]})"
    return name + (" 올림" if schedule.uploaded(ep, video) else "")


def order_path(ep: str, video: str) -> Path:
    """업로드/ 안의 위치: <회차 폴더>/<영상 폴더>. 회차의 영상을 모두 올렸으면 '올린 영상/' 안."""
    from . import schedule

    done = all(schedule.uploaded(ep, v) for v in schedule.load().get(ep, {}) if v in LABELS)
    return Path(UPLOADED if done else "") / episode_name(ep) / video_name(ep, video)


def _find(od: Path, key: str) -> Path | None:
    """표시 파일로 찾기 (링크가 끊긴 경우). 회차 폴더 안(지금)과 업로드/ 바로 아래(2026-11-03 한 줄 모양)를 본다.
    목록 이름은 NFD 일 수 있어 NFC 로 바꿔서 연다."""
    def subdirs(parent: Path) -> list[Path]:
        try:
            return [parent / unicodedata.normalize("NFC", n) for n in os.listdir(parent) if not n.startswith(".")]
        except OSError:
            return []

    for top in (od, od / UPLOADED):
        for d in subdirs(top):
            for cand in [d, *subdirs(d)]:
                try:
                    if (cand / MARK).read_text(encoding="utf-8").strip() == key:
                        return cand
                except OSError:
                    continue
    return None


def _prune(d: Path, od: Path) -> None:
    """옮긴 뒤 비어 버린 회차 폴더 정리 (macOS 부속 파일 ._*·.DS_Store 만 남은 경우만). 업로드/·올린 영상/ 자체는 두고."""
    if d in (od, od / UPLOADED) or od not in d.parents:
        return
    try:
        names = os.listdir(d)
        if any(not (n.startswith("._") or n == ".DS_Store") for n in names):
            return
        for n in names:
            (d / unicodedata.normalize("NFC", n)).unlink(missing_ok=True)
        d.rmdir()
    except OSError:
        pass


def arrange(ep: str, video: str, dry_run: bool = False) -> str:
    """이 영상의 업로드 폴더를 지금 일정·제목·올림 여부에 맞는 위치로 (없으면 만든다) + episodes 링크.
    바뀐 게 있으면 '이전 → 새 위치', 없으면 ""."""
    od = order_dir()
    link = root() / ep / LABELS[video]
    key = f"{ep}/{video}"
    want = od / order_path(ep, video)
    cur = None
    if link.is_symlink():
        target = Path(os.readlink(link))
        cur = target if target.is_dir() else None
    elif link.is_dir():
        cur = link  # 예전 모양: 진짜 폴더 (같은 디스크에서만 이름 바꾸기로 옮긴다)
    cur = cur or _find(od, key)
    if cur == want and link.is_symlink():
        return ""

    def show(p: Path) -> str:
        return str(p.relative_to(od)) if od in p.parents else p.name

    change = f"{show(cur) if cur else '(새 폴더)'} → {want.relative_to(od)}"
    if dry_run:
        return change if cur != want else ""
    want.parent.mkdir(parents=True, exist_ok=True)
    if cur is None:
        want.mkdir(exist_ok=True)
    elif cur != want:
        if want.exists():
            raise FileExistsError(f"같은 이름의 업로드 폴더가 이미 있습니다: {want}")
        os.rename(cur, want)
        _prune(cur.parent, od)
    (want / MARK).write_text(key + "\n", encoding="utf-8")
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.link")
    if tmp.is_symlink():
        tmp.unlink()
    os.symlink(want, tmp)
    os.replace(tmp, link)  # 링크를 한 번에 바꿈
    return change if cur != want else ""


DAY_LINE = re.compile(r"^\[올릴 날: [^\]]*\]")


def day_line(day: str) -> str:
    """업로드방법 txt 첫 줄: [올릴 날: 2026-11-03(화) 18:00 예약]"""
    from . import schedule

    if not day:
        return ""
    t = schedule.upload_time()
    return f"[올릴 날: {schedule.label(date.fromisoformat(day))}" + (f" {t} 예약" if t else "") + "]"


def resync(dry_run: bool = False) -> list[str]:
    """지금 업로드 일정에 맞춘다: 업로드/<회차>/<구분> 위치·이름(올리면 '올림', 다 올린 회차는 '올린 영상' 안으로) + 업로드방법 txt 첫 줄의 '올릴 날'.
    [바뀐 것 '폴더: 이전 → 새 이름' · '회차/구분/파일'] SSD 가 연결 안 돼 있으면 폴더는 건너뛴다."""
    from . import schedule

    changed = []
    od = order_dir()
    for ep, videos in sorted(schedule.load().items()):
        for video, day in videos.items():
            if video not in LABELS:
                continue
            d = root() / ep / LABELS[video]
            if od and not ep.startswith("_") and od.parent.is_dir() and (
                    d.is_symlink() or d.is_dir() or _find(od, f"{ep}/{video}")):  # 만든 적 없는 폴더는 만들지 않는다
                try:
                    moved = arrange(ep, video, dry_run)
                except OSError as e:
                    moved = f"실패 ({e})"
                if moved:
                    changed.append(f"폴더: {moved}")
            for name in GUIDES:
                txt = d / name
                try:
                    body = txt.read_text(encoding="utf-8")
                except OSError:  # 그 플랫폼 안내가 없는 폴더(롱폼의 인스타 등)·SSD 연결 안 됨
                    continue
                new = DAY_LINE.sub(day_line(day), body, count=1)
                if new != body:
                    changed.append(f"{ep}/{LABELS[video]}/{name}")
                    if not dry_run:
                        txt.write_text(new, encoding="utf-8")
    return changed


def place(src: Path, dest: Path) -> None:
    """독립된 복사본 (외장 SSD 는 끊긴 이력이 있어 storage.safe_copy: .part → fsync → 다시 읽어 검증 → 이름 바꾸기).
    같은 파일이 이미 있으면(크기·수정 시각이 같으면) 다시 복사하지 않는다 (safe_copy 가 수정 시각을 유지).

    하드링크는 쓰지 않는다: 다시 빌드할 때 out/ 파일이 제자리에서 덮어써지면 업로드 폴더 파일까지
    같이 바뀌어, 검수 전·내보내기 중인 영상이 업로드 폴더에 나타난다 (2026-10-17 실제 발생).
    """
    st = src.stat()
    try:
        d = dest.stat()
        if d.st_size == st.st_size and int(d.st_mtime) == int(st.st_mtime):
            return
    except OSError:
        pass
    storage.safe_copy(src, dest)
