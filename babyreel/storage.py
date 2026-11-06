"""외장 SSD 저장소 (2026-11-01 사용자 요청 "저장공간 문제로 무거운 것은 전부 SSD 에서, 단 SSD 가 전력 문제로 끊긴 이력").

무엇을 어디에 두나
- SSD (config [storage] ssd_root, 기본 /Volumes/ExternalDisk/filmcraft): 무거운 것
  library/<YYYY-MM>/ 촬영 원본 · library/_sheets 컨택트시트 · library/_duplicates · episodes/<ep>/out/ 완성 영상
  → Mac 쪽에는 같은 이름의 심볼릭 링크 (경로가 그대로라 plan.json·index.json·FilmCraft 프로젝트를 고칠 필요 없음)
- 내장 디스크: 가벼운 상태 (plan/build/metadata/publish.json, data/, index.json, docs, 설정, 비밀 키)
  → SSD 가 빠져도 일정·분석·알림(scripts/daily.sh)은 계속 돈다
- 업로드 폴더 SSD 업로드/<회차 폴더>/<숏폼1 (날짜)> (2026-11-02 부터, iCloud 전달 폴더 대신, deliver.arrange), Mac 쪽 episodes/<ep>/숏폼1 등은 링크.
  아래 archive/evict 는 예전 iCloud 전달 폴더용 (deliver.root() 아래 <플랫폼>/<날짜_제목>/ 모양만 찾으므로 지금은 할 일 없음)

끊김에 대비한 규칙 (exFAT 은 저널링이 없어 쓰는 중에 끊기면 그 파일이 깨질 수 있다)
1. 무거운 작업 전에 점검(require): 연결·쓰기 가능·빈 공간. 안 되면 아무것도 바꾸지 않고 멈춘다
2. 복사는 항상 '이름.part 로 복사 → 디스크까지 기록(fsync) → 캐시 없이 다시 읽어 해시 비교 → 이름 바꾸기'.
   원본은 검증이 끝난 뒤에만 지운다 → 중간에 끊겨도 원본은 그대로, 다시 실행하면 이어서 한다
3. 작업 중에는 Mac 이 잠들지 않게(caffeinate)
4. 연결 속도가 USB 2.0(480Mb/s)이면 경고: 전력도 2.5W 뿐이라 NVMe SSD 가 끊기기 쉽다 (2026-11-01 실측)
   → 큰 이동(migrate)은 USB 3 연결에서만 한다
5. 매일 가벼운 상태를 SSD 에 백업 (data/·json·메모·받아쓰기·설정·문서, 최근 7일)
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

from . import config
from .fc import ROOT, rel

CHUNK = 8 << 20
LIB_SHARED = ("_sheets", "_duplicates")  # library 안에서 SSD 로 보내는 공용 폴더 (월 폴더 외)
BACKUP_KEEP = 7
SMALL = 200 << 20  # 이보다 작은 폴더는 USB 2.0 연결이어도 옮긴다 (빈 월 폴더 등)


class StorageError(RuntimeError):
    pass


def _cfg() -> dict:
    return config.load().get("storage", {})


def enabled() -> bool:
    """[storage] 가 있고 BABYREEL_NO_SSD 가 아니면 SSD 사용 (selftest·다른 채널 프로필은 지금처럼 로컬)."""
    return bool(_cfg().get("ssd_root")) and not os.environ.get("BABYREEL_NO_SSD")


def ssd_root() -> Path:
    return Path(_cfg().get("ssd_root", "/Volumes/ExternalDisk/filmcraft"))


def volume() -> Path:
    r = ssd_root()
    return Path(*r.parts[:3]) if r.parts[:2] == ("/", "Volumes") else r


def online() -> bool:
    return os.path.ismount(volume()) and ssd_root().is_dir()


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def link_info() -> dict:
    """SSD 의 USB 연결 속도·전력 (system_profiler). 못 찾으면 빈 dict."""
    name = _cfg().get("usb_device", "Ugreen")
    try:
        out = subprocess.run(["system_profiler", "-json", "SPUSBHostDataType"], capture_output=True, text=True,
                             timeout=30).stdout
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    found: list[dict] = []

    def walk(x):
        if isinstance(x, dict):
            if name in str(x.get("_name", "")) or name in str(x.get("USBDeviceKeyVendorName", "")):
                found.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    if not found:
        return {}
    d = found[0]
    speed = d.get("USBDeviceKeyLinkSpeed", "")
    return {"speed": speed, "power": d.get("USBDeviceKeyPowerAllocation", ""),
            "slow": speed.startswith("480") or speed.startswith("12 ")}


def problems(need_gb: float = 0.0, write: bool = True) -> list[str]:
    """지금 SSD 로 작업할 수 없는 이유들 (없으면 빈 목록)."""
    if not online():
        return [f"외장 SSD({volume().name})가 연결돼 있지 않습니다 → 연결한 뒤 다시 (Finder 에 {volume().name} 가 보이면 됨)"]
    out = []
    if write:
        probe = ssd_root() / f".write_test_{os.getpid()}"
        try:
            with open(probe, "wb") as f:
                f.write(b"ok")
                f.flush()
                os.fsync(f.fileno())
            probe.unlink()
        except OSError as e:
            out.append(f"외장 SSD 에 쓸 수 없습니다 ({e.strerror}) → 다시 연결하거나 디스크 유틸리티 '응급 처치'")
    need = max(need_gb, float(_cfg().get("min_free_gb", 20)))
    if free_gb(ssd_root()) < need:
        out.append(f"외장 SSD 빈 공간 {free_gb(ssd_root()):.0f}GB < 필요 {need:.0f}GB")
    return out


def require(what: str, need_gb: float = 0.0) -> None:
    """무거운 작업 전 점검. 문제가 있으면 아무것도 바꾸지 않고 멈춘다."""
    if not enabled():
        return
    p = problems(need_gb)
    if p:
        raise StorageError(f"{what}: " + " / ".join(p))


def keep_awake() -> None:
    """이 프로세스가 끝날 때까지 Mac·디스크가 잠들지 않게 (전원 문제로 끊기는 것 줄이기)."""
    try:
        subprocess.Popen(["caffeinate", "-i", "-m", "-s", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# --- 끊겨도 안전한 복사 ---------------------------------------------------------------------

def _hash_file(path: Path, nocache: bool = False) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        if nocache:
            try:
                fcntl.fcntl(f.fileno(), fcntl.F_NOCACHE, 1)  # 메모리 캐시가 아니라 디스크에 실제로 쓰인 내용을 읽는다
            except (AttributeError, OSError):
                pass
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()


def safe_copy(src: Path, dest: Path) -> None:
    """src → dest. 'dest.part' 로 복사·fsync·다시 읽어 검증한 뒤에만 dest 로 바꾼다. 실패하면 dest 는 그대로(없음)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.part")
    st = src.stat()
    h = hashlib.sha1()
    try:
        with open(src, "rb") as r, open(tmp, "wb") as w:
            while b := r.read(CHUNK):
                h.update(b)
                w.write(b)
            w.flush()
            os.fsync(w.fileno())
        if tmp.stat().st_size != st.st_size or _hash_file(tmp, nocache=True) != h.hexdigest():
            raise StorageError(f"복사 검증 실패: {src.name} (다시 실행하면 처음부터 다시 복사)")
        os.utime(tmp, (st.st_atime, st.st_mtime))  # 촬영·수정 시각 유지 (얼굴 캐시 키에 쓰임)
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass  # SSD 가 빠졌으면 지울 수도 없다 → 다음 실행 때 같은 .part 를 덮어씀
        raise


def same_file(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and _hash_file(a) == _hash_file(b, nocache=True)
    except OSError:
        return False


def safe_move(src: Path, dest: Path) -> None:
    """안전한 이동: 같은 디스크면 이름만 바꾸고, 다른 디스크면 safe_copy 로 검증한 뒤 원본을 지운다."""
    try:
        if os.stat(src).st_dev == os.stat(dest.parent if dest.parent.exists() else dest.parent.parent).st_dev:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)
            return
    except OSError:
        pass
    safe_copy(src, dest)
    src.unlink()


# --- 위치 (Mac 쪽 링크 → SSD) ----------------------------------------------------------------

def _link(local: Path, target: Path) -> Path:
    """local 이 없으면 SSD 의 target 폴더를 만들고 local → target 링크. 이미 진짜 폴더면 그대로(이전 전 상태)."""
    if local.is_symlink() or local.exists():
        return local
    target.mkdir(parents=True, exist_ok=True)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.symlink_to(target, target_is_directory=True)
    return local


def library_dir(name: str, base: Path | None = None) -> Path:
    """library/<YYYY-MM> 또는 library/_sheets 등. SSD 를 쓰면 새 폴더는 SSD 에 만들고 링크.
    base: library 위치 (테스트가 임시 폴더로 바꾼 media.LIBRARY 를 따르게)."""
    base = base or config.LIBRARY
    local = base / name
    if base.resolve() != config.LIBRARY.resolve():  # 테스트·다른 위치: 그 안에서만 (SSD 로 보내지 않음)
        local.mkdir(parents=True, exist_ok=True)
        return local
    if local.is_symlink():
        if not local.exists():
            raise StorageError(f"{rel(local)} 는 SSD 에 있는데 SSD 가 연결돼 있지 않습니다")
        return local
    if not enabled() or local.exists():
        local.mkdir(parents=True, exist_ok=True)
        return local
    if not online():
        raise StorageError(f"새 원본 폴더 {name} 를 SSD 에 만들 수 없습니다: SSD 연결 안 됨")
    return _link(local, ssd_root() / "library" / name)


def out_dir(episode_dir: Path) -> Path:
    """episodes/<ep>/out. SSD 를 쓰면 SSD 의 episodes/<ep>/out 으로 링크 ('_' 로 시작하는 테스트 회차는 로컬)."""
    return episode_subdir(episode_dir, "out")


def episode_subdir(episode_dir: Path, name: str) -> Path:
    """episodes/<ep>/<name> (out, 업로드 폴더 숏폼1·숏폼2·롱폼). SSD 를 쓰면 SSD 의 같은 자리로 링크."""
    local = episode_dir / name
    if not enabled() or episode_dir.name.startswith("_"):
        local.mkdir(parents=True, exist_ok=True)
        return local
    if local.is_symlink():
        if not local.exists():
            if not online():
                raise StorageError(f"{rel(local)} 는 SSD 에 있는데 SSD 가 연결돼 있지 않습니다")
            Path(os.readlink(local)).mkdir(parents=True, exist_ok=True)
        return local
    if local.exists():
        return local  # 아직 옮기기 전 (./br storage migrate)
    if not online():
        raise StorageError(f"{name} 폴더를 SSD 에 만들 수 없습니다: SSD 연결 안 됨")
    return _link(local, ssd_root() / "episodes" / episode_dir.name / name)


# --- 옮기기 (내장 → SSD) ---------------------------------------------------------------------

def candidates() -> list[tuple[Path, Path]]:
    """아직 내장 디스크에 진짜 폴더로 있는 무거운 것 → SSD 위치."""
    out = []
    if config.LIBRARY.exists():
        for d in sorted(config.LIBRARY.iterdir()):
            if d.is_dir() and not d.is_symlink() and (d.name[:4].isdigit() or d.name in LIB_SHARED):
                out.append((d, ssd_root() / "library" / d.name))
    for ep in sorted(config.EPISODES.iterdir()) if config.EPISODES.exists() else []:
        o = ep / "out"
        if not ep.name.startswith("_") and o.is_dir() and not o.is_symlink():
            out.append((o, ssd_root() / "episodes" / ep.name / "out"))
    return out


def _files(d: Path) -> list[Path]:
    return sorted(p for p in d.rglob("*") if p.is_file() and not p.name.startswith("._") and p.name != ".DS_Store"
                  and not p.name.endswith(".part"))


def _nfc(p: Path) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", str(p))


def dir_size(d: Path) -> int:
    return sum(p.stat().st_size for p in _files(d))


def migrate_dir(local: Path, target: Path, log=print) -> None:
    """local 폴더를 SSD 의 target 으로: 파일마다 검증 복사(이미 같은 파일이면 건너뜀) → 전부 확인 →
    local 을 '.옮기는중' 으로 이름만 바꾸고 링크 → 링크로 전부 보이는지 확인 → 그때 내장 쪽 삭제.
    어느 단계에서 끊겨도 내장 쪽 원본은 남아 있고, 다시 실행하면 이어서 한다."""
    files = _files(local)
    if target.is_dir():  # SSD 쪽에 예전 사본만 있는 파일 (내장 쪽 최신본에는 없음) → 지우지 않고 따로
        # exFAT 은 한글 이름을 자모 분리형(NFD)으로 돌려줄 수 있다 → 같은 형태로 맞춰 비교
        names = {_nfc(f.relative_to(local)) for f in files}
        stale = [g for g in _files(target) if _nfc(g.relative_to(target)) not in names]
        for g in stale:
            keep = target.parent / f"{target.name}_이전_{date.today():%Y%m%d}" / g.relative_to(target)
            keep.parent.mkdir(parents=True, exist_ok=True)
            os.replace(g, keep)
        if stale:
            log(f"    SSD 쪽 예전 파일 {len(stale)}개 → {target.name}_이전_{date.today():%Y%m%d}/ (지우지 않음)")
    for i, f in enumerate(files, 1):
        dest = target / f.relative_to(local)
        if dest.exists() and dest.stat().st_size == f.stat().st_size and same_file(f, dest):
            continue
        log(f"    {i}/{len(files)} {f.name} ({f.stat().st_size / 1e9:.2f}GB)")
        safe_copy(f, dest)
    for f in files:  # 전부 다시 확인 (크기)
        dest = target / f.relative_to(local)
        if not dest.exists() or dest.stat().st_size != f.stat().st_size:
            raise StorageError(f"확인 실패: {dest} — 내장 쪽은 그대로 둠, 다시 실행")
    hold = local.with_name(local.name + ".옮기는중")
    if hold.exists():
        raise StorageError(f"{rel(hold)} 가 남아 있습니다 (지난번 이동이 끝나지 않음) → 확인 후 지우고 다시")
    _remap_face_cache(local, target)
    os.rename(local, hold)
    try:
        local.symlink_to(target, target_is_directory=True)
        # 원래 경로(이제 링크를 거쳐 SSD)로 모든 파일이 같은 크기로 보이는지
        missing = [f for f in files if not f.exists() or f.stat().st_size != (hold / f.relative_to(local)).stat().st_size]
        if missing:
            raise StorageError(f"링크 확인 실패 ({len(missing)}개) → 원래대로 되돌림")
    except BaseException:
        if local.is_symlink():
            local.unlink()
        os.rename(hold, local)
        raise
    shutil.rmtree(hold)


def _remap_face_cache(local: Path, target: Path) -> None:
    """얼굴 인식 캐시 키는 실제 경로를 쓴다 → 옮긴 뒤에도 다시 계산하지 않게 키의 경로만 바꾼다."""
    from . import faces

    old, new = str(local.resolve()) + "/", str(target) + "/"
    cache = faces._load_cache()
    moved = {k.replace(f"|{old}", f"|{new}", 1): v for k, v in cache.items()}
    if moved != cache:
        faces._save_cache(moved)


def migrate(dry_run: bool = False, log=print, allow_slow: bool = False) -> list[tuple[Path, Path, int]]:
    plan = [(a, b, dir_size(a)) for a, b in candidates()]
    total = sum(s for *_, s in plan)
    if dry_run or not plan:
        return plan
    require("SSD 로 옮기기", need_gb=total / 1e9 + 5)
    li = link_info()
    held = [x for x in plan if li.get("slow") and not allow_slow and x[2] >= SMALL]
    keep_awake()
    done = []
    for a, b, size in plan:
        if (a, b, size) in held:
            continue
        log(f"  → {rel(a)} ({size / 1e9:.1f}GB) → {b}")
        migrate_dir(a, b, log)
        log(f"  ✓ {rel(a)} → SSD")
        done.append((a, b, size))
    if held:
        raise StorageError(f"작은 폴더 {len(done)}개는 옮겼고, 큰 폴더 {len(held)}개"
                           f"({sum(x[2] for x in held) / 1e9:.1f}GB)는 기다립니다: SSD 가 USB 2.0({li['speed']}, 전력 "
                           f"{li['power']})로 연결돼 있어 큰 이동 중 끊길 위험이 큼 → USB 3 케이블로 Mac 의 USB-C 포트에 "
                           "직접 연결한 뒤 다시 (./br storage status 에서 10 Gb/s 확인)")
    return done


# --- iCloud 전달 폴더: 올린 영상은 SSD 보관함으로, 올릴 영상은 Mac 사본만 내리기 ---------------------

VIDEO_EXT = (".mp4", ".mov")


def _delivered_videos() -> list[Path]:
    from . import deliver

    try:
        root = deliver.root()
    except OSError:
        return []
    return sorted(f for f in root.glob("*/*/*") if f.suffix.lower() in VIDEO_EXT and f.parent.name[:4].isdigit())


def archive_path(f: Path) -> Path:
    """SSD 보관 위치. exFAT 은 한글 이름을 지우거나 비교할 때 문제가 있어 영어 이름으로:
    delivered/<플랫폼>/<올린 날짜>_<회차>_<영상>.mp4 (회차·영상은 폴더의 .episode 표시에서)."""
    marker = f.parent / ".episode"
    try:
        ep, _, video = marker.read_text(encoding="utf-8").strip().partition(":")
    except OSError:
        ep, video = "unknown", f.stem.encode("utf-8").hex()[:12]
    kind = {"롱폼영상": "long"}.get(f.stem, video or "video")
    return ssd_root() / "delivered" / f.parent.parent.name / f"{f.parent.name[:10]}_{ep}_{kind}{f.suffix.lower()}"


def archive_delivered(days: int | None = None, dry_run: bool = False, log=print) -> list[tuple[Path, Path]]:
    """올릴 날짜가 days 일 이상 지난 전달 영상: SSD 보관함으로 검증 복사 → iCloud 에서 지움 (Mac·iCloud 공간 확보).
    제목·설명·썸네일 같은 작은 파일은 기록으로 iCloud 에 남긴다. 다시 필요하면 SSD delivered/ 에서."""
    days = int(_cfg().get("archive_after_days", 3)) if days is None else days
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    todo = [(f, archive_path(f)) for f in _delivered_videos() if f.parent.name[:10] < cutoff
            or (days == 0 and f.parent.name[:10] < date.today().isoformat())]
    if dry_run or not todo:
        return todo
    require("전달 영상 보관", need_gb=sum(f.stat().st_size for f, _ in todo) / 1e9 + 1)
    keep_awake()
    for i, (f, dest) in enumerate(todo, 1):
        if not (dest.exists() and same_file(f, dest)):  # iCloud 에만 있으면 읽을 때 내려받는다
            log(f"    {i}/{len(todo)} {f.parent.parent.name}/{f.parent.name[:40]}")
            safe_copy(f, dest)
        f.unlink()  # iCloud '최근 삭제된 항목'에 30일 남는다
    return todo


def evict_delivered(days: int | None = None, dry_run: bool = False) -> list[Path]:
    """아직 올릴 전달 영상: iCloud·폰에는 그대로 두고 Mac 사본만 내린다(brctl evict). Mac 에서 열면 다시 받는다.
    iCloud 에 다 올라가기 전 파일은 iCloud 가 거절한다 (안전)."""
    out = []
    for f in _delivered_videos():
        try:
            if f.stat().st_blocks == 0:  # 이미 내려 둠
                continue
        except OSError:
            continue
        out.append(f)
        if not dry_run:
            subprocess.run(["brctl", "evict", str(f)], capture_output=True)
    return out


# --- 매일 백업 (가벼운 상태 → SSD) -------------------------------------------------------------

def backup_sources() -> list[Path]:
    srcs = [p for p in config.DATA.glob("*") if p.is_file() and p.suffix in (".json", ".jsonl", "")
            and not p.name.startswith(".")]
    for ep in sorted(config.EPISODES.glob("*")) if config.EPISODES.exists() else []:
        if not ep.name.startswith("_"):
            srcs += sorted(ep.glob("*.json"))
    srcs += [config.LIBRARY / "index.json"]
    for d in ("_memos", "_transcripts"):
        srcs += sorted((config.LIBRARY / d).glob("*")) if (config.LIBRARY / d).is_dir() else []
    srcs += sorted((ROOT / "config").glob("*")) + sorted((ROOT / "docs").glob("*.md"))
    return [p for p in srcs if p.is_file()]


def backup(today: date | None = None) -> Path | None:
    """data/·회차 json·index·메모·받아쓰기·설정·문서 → SSD _backup/<날짜>.tar.gz 한 파일 (최근 7개).
    exFAT 은 작은 파일도 128KB 씩 차지해 파일째 복사하면 40배로 커진다 → 압축 한 파일. 비밀 키(secrets/)는 넣지 않는다."""
    import tarfile
    import tempfile

    if not enabled() or problems(write=True):
        return None
    day = (today or date.today()).isoformat()
    folder = ssd_root() / "_backup"
    with tempfile.TemporaryDirectory() as tmp:
        arc = Path(tmp) / f"{day}.tar.gz"
        with tarfile.open(arc, "w:gz") as tar:
            for p in backup_sources():
                tar.add(p, arcname=str(p.relative_to(ROOT)))
        dest = folder / arc.name
        safe_copy(arc, dest)  # .part → 검증 → 이름 바꾸기 (쓰는 중 끊겨도 어제 백업은 그대로)
    olds = sorted(p for p in folder.glob("20*.tar.gz"))
    for p in olds[:-BACKUP_KEEP]:
        p.unlink(missing_ok=True)
    for d in folder.iterdir():  # 예전 방식(폴더째) 백업 정리
        if d.is_dir() and d.name[:4].isdigit():
            shutil.rmtree(d, ignore_errors=True)
    return dest


# --- 상태 ---------------------------------------------------------------------------------

def status_lines() -> list[str]:
    lines = []
    internal = shutil.disk_usage(ROOT)
    lines.append(f"내장 디스크: 빈 공간 {internal.free / 1e9:.0f}GB ({internal.used / internal.total * 100:.0f}% 사용)")
    if not enabled():
        return lines + ["SSD 사용 안 함 (config/channel.toml [storage] 없음 또는 BABYREEL_NO_SSD)"]
    if not online():
        return lines + [f"⚠ 외장 SSD({volume().name}) 연결 안 됨 → 일정·분석·알림은 계속, 정리·편집·전달은 연결 후"]
    u = shutil.disk_usage(ssd_root())
    lines.append(f"외장 SSD {volume().name}: 빈 공간 {u.free / 1e9:.0f}GB ({u.used / u.total * 100:.0f}% 사용)")
    li = link_info()
    if li:
        mark = "⚠" if li["slow"] else "✓"
        lines.append(f"{mark} 연결: {li['speed']} · 전력 {li['power']}" +
                     (" → USB 2.0 연결이라 느리고 전력이 부족해 끊기기 쉬움. USB 3(10Gbps) 케이블로 Mac 뒤쪽/앞쪽 USB-C 에 "
                      "직접 연결 (허브·USB-A 변환 젠더 X)" if li["slow"] else ""))
    for p in problems():
        lines.append(f"⚠ {p}")
    cands = candidates()
    if cands:
        total = sum(dir_size(a) for a, _ in cands)
        lines.append(f"아직 내장 디스크에 있는 무거운 폴더 {len(cands)}개 ({total / 1e9:.1f}GB) → ./br storage migrate")
        lines += [f"  · {rel(a)}" for a, _ in cands[:8]] + (["  · …"] if len(cands) > 8 else [])
    else:
        lines.append("✓ 무거운 폴더는 모두 SSD 에 있음")
    ar = archive_delivered(dry_run=True)
    if ar:
        lines.append(f"iCloud 의 이미 올린 영상 {len(ar)}개 ({sum(f.stat().st_size for f, _ in ar) / 1e9:.1f}GB) "
                     "→ ./br storage archive (SSD 보관함으로 옮기고 iCloud 에서 지움)")
    ev = evict_delivered(dry_run=True)
    if ev:
        lines.append(f"iCloud 의 올릴 영상 중 Mac 에 내려와 있는 {len(ev)}개 ({sum(f.stat().st_size for f in ev) / 1e9:.1f}GB) "
                     "→ ./br storage evict (iCloud·폰에는 그대로)")
    bk = sorted((ssd_root() / "_backup").glob("20*.tar.gz")) if (ssd_root() / "_backup").is_dir() else []
    lines.append(f"백업: {bk[-1].name if bk else '아직 없음'} (매일 scripts/daily.sh, 최근 {BACKUP_KEEP}개, "
                 f"{sum(p.stat().st_size for p in bk) / 1e6:.1f}MB)")
    return lines
