#!/bin/sh
# 처음 한 번: FilmCraft CLI, 파이썬 환경, 음원·효과음·스티커, AI 모델 준비
set -e
cd "$(dirname "$0")/.."
VER=0.8.0
if [ ! -x tools/bin/filmcraft-cli ]; then
  echo "▶ FilmCraft CLI $VER 다운로드"
  TMP=$(mktemp -d)
  gh release download "v$VER" -R storytold/filmcraft -p "filmcraft-cli-$VER-macos-universal.zip" -p SHA256SUMS.txt -D "$TMP"
  (cd "$TMP" && grep "filmcraft-cli-$VER-macos-universal.zip" SHA256SUMS.txt | shasum -a 256 -c -)
  unzip -q -o "$TMP/filmcraft-cli-$VER-macos-universal.zip" -d "$TMP"
  mkdir -p tools/bin && cp "$TMP/filmcraft-cli-$VER-macos-universal/filmcraft-cli" tools/bin/
  xattr -d com.apple.quarantine tools/bin/filmcraft-cli 2>/dev/null || true
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
./br setup
# 귀여운 서체 (펀치라인·썸네일). OFL 라이선스
mkdir -p "$HOME/Library/Fonts" && cp -n assets/fonts/Jua-Regular.ttf "$HOME/Library/Fonts/" 2>/dev/null || true
# 아빠 목소리 내레이션 (Qwen3-TTS, Apache-2.0). mlx-audio 가 무거워 별도 가상환경. 모델은 첫 사용 때 받는다(~1GB)
[ -d .venv-voice ] || python3 -m venv .venv-voice
.venv-voice/bin/pip install -q --upgrade pip
.venv-voice/bin/pip install -q -r requirements-voice.txt
