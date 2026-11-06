"""업로더 공통 인터페이스.

업로더는 '대상(target)' 하나를 맡는다: youtube_shorts | youtube_long | instagram_reels.
입력은 metadata.json 의 해당 항목(dict), 출력은 PublishResult.
새 업로더를 추가하려면 Publisher 를 상속하고 publish/__init__.py 의 REGISTRY 에 등록한 뒤
config/channel.toml [publish] 에서 대상별로 이름을 지정한다.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ..config import DATA

LOG = DATA / "publish_log.jsonl"


@dataclass
class PublishResult:
    target: str
    status: str  # "packaged"(폴더 준비) | "draft"(비공개 업로드, 공개 대기) | "uploaded" | "failed"
    url: str | None = None
    remote_id: str | None = None
    detail: str = ""
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class Publisher(ABC):
    name: str = "base"

    def __init__(self, episode_dir: Path, episode_id: str):
        self.episode_dir = episode_dir
        self.episode_id = episode_id

    @abstractmethod
    def publish(self, target: str, meta: dict) -> PublishResult: ...


def record(episode_dir: Path, episode_id: str, result: PublishResult) -> None:
    """에피소드별 publish.json + 채널 전체 로그(data/publish_log.jsonl)에 기록."""
    path = episode_dir / "publish.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    state[result.target] = asdict(result)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    DATA.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"episode": episode_id, **asdict(result)}, ensure_ascii=False) + "\n")


def load_state(episode_dir: Path) -> dict:
    path = episode_dir / "publish.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
