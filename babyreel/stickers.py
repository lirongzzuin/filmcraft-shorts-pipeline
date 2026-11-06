"""컬러 이모지 → 투명 PNG (화자 아이콘·스티커용). macOS swift + Apple Color Emoji 사용."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import ASSETS
from .fc import ROOT

STICKER_DIR = ASSETS / "stickers"
SCRIPT = ROOT / "scripts" / "render_emoji.swift"


def png_for(emoji: str, size: int = 256) -> Path:
    name = "-".join(f"{ord(ch):x}" for ch in emoji)
    path = STICKER_DIR / f"{name}.png"
    if not path.exists():
        STICKER_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["swift", str(SCRIPT), emoji, str(path), str(size)], check=True)
    return path
