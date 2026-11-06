"""업로더 레지스트리. config/channel.toml [publish] 의 값이 여기 키와 매칭된다."""

from __future__ import annotations

from pathlib import Path

from .. import config
from .base import Publisher, PublishResult, load_state, record

# 쇼츠 1부(1일차)·롱폼 → 쇼츠 2부(2일차, 이름 끝 _2). 업로더 설정은 _2 를 뗀 이름을 같이 쓴다
TARGETS = ("youtube_shorts", "instagram_reels", "tiktok", "youtube_long",
           "youtube_shorts_2", "instagram_reels_2", "tiktok_2")


def base_target(target: str) -> str:
    """youtube_shorts_2 → youtube_shorts (플랫폼·업로더 종류)."""
    return target.removesuffix("_2")


def video_of(target: str) -> str:
    """업로드 대상 → 영상: short | short2 | long."""
    if target == "youtube_long":
        return "long"
    return "short2" if target.endswith("_2") else "short"


def _registry() -> dict[str, type[Publisher]]:
    from .instagram_api import InstagramPublisher
    from .manual import ManualPublisher
    from .youtube_api import YouTubePublisher

    return {cls.name: cls for cls in (ManualPublisher, YouTubePublisher, InstagramPublisher)}


def publisher_for(target: str, episode_dir: Path, episode_id: str) -> Publisher:
    name = config.load()["publish"].get(base_target(target), "manual")
    registry = _registry()
    if name not in registry:
        raise KeyError(f"[publish] {target} = '{name}': 알 수 없는 업로더 (가능: {', '.join(registry)})")
    return registry[name](episode_dir, episode_id)


__all__ = ["TARGETS", "PublishResult", "base_target", "load_state", "publisher_for", "record", "video_of"]
