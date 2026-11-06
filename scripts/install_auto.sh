#!/bin/sh
# macOS LaunchAgent 등록: inbox/ 에 파일이 생기면 + 매일 08:00(오늘 올릴 영상 알림)·21:00 에 scripts/auto.sh 실행
#                         + (설정했으면) 텔레그램 봇 상시 실행
#   ./scripts/install_auto.sh               켜기 (새 영상 자동 제작 포함)
#   ./scripts/install_auto.sh --check-only  매일 점검만 켜기 (08:00·21:00 scripts/daily.sh: 오늘 올릴 영상·성과 분석·일정 확인, 영상 제작 없음)
#   ./scripts/install_auto.sh --remove      끄기
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=com.babyreel.auto
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

BOT_LABEL=com.babyreel.telegram
BOT_PLIST="$HOME/Library/LaunchAgents/$BOT_LABEL.plist"

DAILY_LABEL=com.babyreel.daily
DAILY_PLIST="$HOME/Library/LaunchAgents/$DAILY_LABEL.plist"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$BOT_LABEL" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$DAILY_LABEL" 2>/dev/null || true
  rm -f "$PLIST" "$BOT_PLIST" "$DAILY_PLIST"
  echo "자동 실행·매일 점검·텔레그램 봇 꺼짐"; exit 0
fi

if [ "${1:-}" = "--check-only" ]; then
  mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"
  cat >"$DAILY_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$DAILY_LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>-l</string><string>$DIR/scripts/daily.sh</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>StartCalendarInterval</key><array>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/$DAILY_LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$DAILY_PLIST"
  echo "매일 점검 켜짐: 08:00·21:00 (오늘 올릴 영상 알림 · 성과 분석 · 일정·조회수 기록, 영상 제작 없음). 끄기: $0 --remove"
  exit 0
fi

CLAUDE_BIN="$(command -v claude || true)"
[ -n "$CLAUDE_BIN" ] || { echo "claude CLI 를 찾을 수 없습니다"; exit 1; }
mkdir -p "$DIR/inbox" "$DIR/logs" "$HOME/Library/LaunchAgents"
# inbox 가 iCloud 촬영 폴더로 연결(심볼릭 링크)돼 있으면 실제 폴더를 감시한다
WATCH="$(cd "$DIR/inbox" && pwd -P)"
PATHS="$(dirname "$CLAUDE_BIN"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>-l</string><string>$DIR/scripts/auto.sh</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATHS</string></dict>
  <key>WatchPaths</key><array><string>$WATCH</string></array>
  <key>StartCalendarInterval</key><array>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>ThrottleInterval</key><integer>120</integer>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "자동 실행 켜짐: $WATCH 감시 + 매일 08:00·21:00 (아침에 오늘 올릴 영상 알림)"

# 텔레그램 봇 (./br telegram setup <토큰> 을 먼저 했을 때만): 항상 켜 두고, 꺼지면 다시 살린다
if [ -f "$DIR/secrets/telegram.json" ]; then
  cat >"$BOT_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$BOT_LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/br</string><string>telegram</string><string>run</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATHS</string><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$DIR/logs/telegram.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/telegram.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/$BOT_LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$BOT_PLIST"
  echo "텔레그램 봇 켜짐: 봇에게 '영상 만들어줘: 설명' 을 보내면 제작 시작 (로그 logs/telegram.log)"
else
  echo "텔레그램 봇: 아직 설정 안 됨 (원하면 ./br telegram setup <토큰> 후 이 스크립트를 다시 실행)"
fi
echo "끄기: $0 --remove"
