#!/bin/zsh
# Run manually AFTER closing Desktop and letting active tasks finish.
set -euo pipefail
cd -- "${0:A:h}"
APP="/Applications/ChatGPT.app"
BRIDGE_DIR="$PWD"
if /usr/bin/pgrep -f '^/Applications/ChatGPT.app/Contents/MacOS/' >/dev/null; then
  print 'Сначала завершите активные задачи и полностью закройте Desktop (Cmd+Q), затем повторите запуск.'
  exit 1
fi
if [[ ! -x "$BRIDGE_DIR/.venv/bin/python" ]]; then
  print 'Сначала установите зависимости bridge: uv sync --python 3.13'
  exit 1
fi
print 'Проверяю штатное чтение startup config через адаптер.'
"$BRIDGE_DIR/desktop-codex" app-server --print-startup-config >/dev/null
print 'Запускаю Desktop с локальным адаптером его App Server. Терминал можно закрыть.'
/usr/bin/open -a "$APP" --env "CODEX_CLI_PATH=$BRIDGE_DIR/desktop-codex" --env CODEX_APP_SERVER_USE_LOCAL_DAEMON=0
