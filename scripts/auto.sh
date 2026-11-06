#!/bin/zsh -l
# 무인 자동 실행: inbox/ 정리 → Claude 가 기획·편집·검수(쇼츠 1부·2부 + 롱폼) → YouTube 비공개 업로드 + 인스타·틱톡 폴더 → 알림
# 매일 첫 실행 때 '오늘 올릴 영상' 알림 (쇼츠 매일 · 롱폼 이틀에 한 번)
#
# 직접 실행:      ./scripts/auto.sh
# 자동 실행 켜기:  ./scripts/install_auto.sh   (inbox 에 영상이 들어오면 + 매일 21시)
#
# 비용: 로컬 도구(FilmCraft·Supertonic·Whisper)는 무료. 기획/검수는 Claude Code 구독 사용량을 쓴다.
set -u
cd "$(dirname "$0")/.."
INBOX="${BABYREEL_INBOX:-inbox}"   # 촬영 폴더 (보통 iCloud 로 연결된 링크)
mkdir -p data logs "$INBOX"
LOCK=data/auto.lock
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "이미 실행 중"; exit 0
fi
trap 'rmdir "$LOCK"' EXIT
LOG="logs/auto_$(date +%Y%m%d_%H%M%S).log"
REQUEST="${BABYREEL_REQUEST:-}"     # 텔레그램 요청 설명 (babyreel/telegram_bot.py)
SOURCE="${BABYREEL_SOURCE:-launchd}"
notify() {
  osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true
  # 텔레그램 연결 시 폰으로도 (텔레그램 요청은 봇이 결과를 직접 보낸다)
  [[ "$SOURCE" != "telegram" ]] && ./br telegram send "$1"$'\n'"$2" >/dev/null 2>&1 || true
}

# 하루 한 번 점검: 오늘 올릴 영상 알림 · 성과 분석 · 채널 공개 영상(일정·조회수) · 알림 (scripts/daily.sh)
BABYREEL_LOG="$LOG" BABYREEL_SOURCE="$SOURCE" ./scripts/daily.sh || true

# 외장 SSD 가 없으면 정리·편집을 하지 않는다 (원본·완성 영상은 SSD, babyreel/storage.py). 촬영 폴더의 영상은 그대로 둔다
if ./br storage status 2>&1 | grep -q "연결 안 됨"; then
  echo "외장 SSD 연결 안 됨 → 정리·편집 건너뜀" >>"$LOG"
  ls "$INBOX" 2>/dev/null | grep -qi -E '\.(mov|mp4|heic|jpg|png)$' && \
    notify "외장 SSD 연결 필요" "촬영 폴더에 새 영상이 있어요. SSD 를 연결하면 다음 자동 실행 때 정리·편집합니다"
  exit 0
fi

# iCloud 'Mac 저장 공간 최적화'로 아직 안 내려온 파일(.이름.icloud)을 내려받는다
find -L "$INBOX" -name '.*.icloud' 2>/dev/null | while read -r f; do
  d=$(dirname "$f"); b=$(basename "$f" .icloud); brctl download "$d/${b#.}" >/dev/null 2>&1 || true
done

# AirDrop·복사·iCloud 동기화 중인 파일을 건드리지 않도록 inbox 가 30초 동안 변하지 않을 때까지 기다린다
prev=""
for _ in {1..40}; do
  cur=$(find -L "$INBOX" -type f -exec stat -f '%N %z' {} + 2>/dev/null | sort | shasum)
  [[ "$cur" == "$prev" ]] && break
  prev="$cur"; sleep 30
  [[ -n "${BABYREEL_SKIP_WAIT:-}" ]] && break  # 봇이 이미 기다렸음
done

{
  echo "== $(date) ingest"
  ./br ingest
} >>"$LOG" 2>&1

UNUSED=$(./.venv/bin/python -c "
import json,pathlib
p=pathlib.Path('library/index.json')
d=json.loads(p.read_text()) if p.exists() else {}
print(sum(1 for v in d.values() if not v.get('used_in')))" 2>/dev/null || echo 0)
if [[ "$UNUSED" == "0" ]]; then
  echo "새 영상 없음" >>"$LOG"; exit 0
fi

echo "== $(date) claude (미사용 클립 ${UNUSED}개)" >>"$LOG"
PROMPT="$(cat prompts/auto_episode.md)"
if [[ -n "$REQUEST" ]]; then
  PROMPT="$PROMPT"$'\n\n'"## 이번 요청 (아빠가 텔레그램으로 보낸 설명 — 메모와 같은 무게로 가장 먼저 반영)"$'\n'"$REQUEST"
fi
SUMMARY=$(claude -p "$PROMPT" \
  --permission-mode acceptEdits \
  --allowedTools "Bash(./br *),Read,Write,Edit,Glob,Grep" \
  --output-format text 2>>"$LOG")
STATUS=$?
echo "$SUMMARY" >>"$LOG"

if [[ $STATUS -eq 0 ]]; then
  notify "육아일지 준비 완료" "$(echo "$SUMMARY" | tail -5 | tr '\n' ' ' | cut -c1-200)"
else
  notify "육아일지 자동화 실패" "로그: $LOG"
fi
