#!/bin/zsh -l
# 매일 점검만 (영상은 만들지 않음): 오늘 올릴 영상 알림 · YouTube 성과 분석 · 채널 공개 영상 확인(일정·조회수 기록) · 알림
# · (외장 SSD 가 연결돼 있으면) 가벼운 상태 백업
# 같은 날 두 번째 실행부터는 각 단계를 건너뛴다 (data/*_done 표시).
#
# 직접 실행:  ./scripts/daily.sh
# 매일 자동:  ./scripts/install_auto.sh --check-only   (08:00·21:00, 영상 자동 제작 없이)
# scripts/auto.sh(전체 자동 실행)도 맨 앞에서 이 스크립트를 부른다.
#
# 테스트용 환경 변수: BABYREEL_QUIET=1 (알림 안 보냄), BABYREEL_*_MARK (하루 한 번 표시 파일 위치)
set -u
cd "$(dirname "$0")/.."
mkdir -p data logs
LOCK=data/daily.lock
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "이미 실행 중"; exit 0
fi
trap 'rmdir "$LOCK"' EXIT
LOG="${BABYREEL_LOG:-logs/auto_$(date +%Y%m%d_%H%M%S).log}"
find logs -name "auto_*.log" -mtime +30 -delete 2>/dev/null  # 용량 최소화: 실행 기록은 30일만
SOURCE="${BABYREEL_SOURCE:-launchd}"
notify() {
  echo "[알림] $1: $2" >>"$LOG"
  [[ -n "${BABYREEL_QUIET:-}" ]] && return 0
  osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true
  # 텔레그램 연결 시 폰으로도 (텔레그램 요청은 봇이 결과를 직접 보낸다)
  [[ "$SOURCE" != "telegram" ]] && ./br telegram send "$1"$'\n'"$2" >/dev/null 2>&1 || true
}

# 오늘 올릴 영상 알림 (쇼츠 매일 · 롱폼 이틀에 한 번, data/schedule.json)
MARK="${BABYREEL_NOTIFY_MARK:-data/today_notified}"
if [[ "$(cat "$MARK" 2>/dev/null)" != "$(date +%F)" ]]; then
  ./br today $([[ -z "${BABYREEL_QUIET:-}" ]] && echo --notify) >>"$LOG" 2>&1 && date +%F > "$MARK"
fi
# YouTube 성과 분석 (공개 점검·수익 창출 진행·첫 6초·롱폼 유입 → docs/INSIGHTS.md). 설정 안 했으면 건너뜀
IMARK="${BABYREEL_INSIGHTS_MARK:-data/insights_done}"
if [[ "$(cat "$IMARK" 2>/dev/null)" != "$(date +%F)" ]]; then
  IOUT=$(./br insights $([[ -z "${BABYREEL_QUIET:-}" ]] && echo --notify) 2>&1); IRC=$?; echo "$IOUT" >>"$LOG"
  [[ $IRC -eq 0 ]] && date +%F > "$IMARK"
  echo "$IOUT" | grep -q "로그인이 만료\|로그인 필요\|끊길 수 있음" && notify "YouTube 분석 로그인" "로그인이 곧 끊기거나 끊겼어요 → 터미널에서 ./br insights login (1분)"
  # 댓글 챌린지: 실시간에 가까워졌을 때(warmup/live)만 댓글에서 아이디어 모으기
  if ./br challenge status 2>/dev/null | grep -q "단계: \(곧\|실시간\)"; then
    ./br challenge collect >>"$LOG" 2>&1 || true
  fi
fi
# 채널 공개 영상 확인 → 공개 링크·조회수 기록(data/views.json) + 일정과 어긋나면 알림
SMARK="${BABYREEL_SYNC_MARK:-data/sync_done}"
if [[ "$(cat "$SMARK" 2>/dev/null)" != "$(date +%F)" ]]; then
  OUT=$(./br schedule sync --check 2>&1); SRC=$?; echo "$OUT" >>"$LOG"
  echo "$OUT" | grep -q "일정과 다름" && notify "업로드 일정 확인" "공개 날짜가 일정과 다른 영상이 있어요 → ./br schedule sync"
  echo "$OUT" | grep -q "롱폼 연결 확인" && notify "롱폼 연결 확인" "롱폼 조회가 쇼츠보다 너무 적어요 → '쇼츠-롱폼 연결 목록.txt' (고급 기능 필요)"
  [[ $SRC -eq 0 ]] && date +%F > "$SMARK"
fi
# 외장 SSD (연결돼 있을 때만): 가벼운 상태 백업(최근 7일). archive·evict 는 예전 iCloud 전달 폴더용 (남은 게 없으면 할 일 없음)
BMARK="${BABYREEL_BACKUP_MARK:-data/backup_done}"
if [[ "$(cat "$BMARK" 2>/dev/null)" != "$(date +%F)" ]]; then
  BOUT=$(./br storage backup 2>&1); echo "$BOUT" >>"$LOG"
  if echo "$BOUT" | grep -q "✓ 백업"; then
    ./br storage archive >>"$LOG" 2>&1 || true   # 올린 지 3일 지난 전달 영상 → SSD 보관함
    ./br storage evict >>"$LOG" 2>&1 || true     # 올릴 영상은 Mac 사본만 내림 (iCloud·폰에는 그대로)
    ./br storage status 2>&1 | grep "⚠" >>"$LOG"
    date +%F > "$BMARK"
  fi
fi
# 바로 공개 업로더(instagram_api 를 켠 경우)는 올릴 날이 된 영상만 게시
./br publish-due >>"$LOG" 2>&1 || true
exit 0
