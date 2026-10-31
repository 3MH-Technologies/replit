#!/bin/bash
LOG="$HOME/hosting-run.log"
APPLOG="$HOME/hosting-app.log"
exec >>"$LOG" 2>&1
echo "=== zero-start $(date -u +%FT%TZ) in $PWD ==="
LAUNCHED=""
APP_PID=""
launch() {
  echo "LAUNCHED: $1"
  LAUNCHED="$1"
  nohup bash -c "$1" >>"$APPLOG" 2>&1 &
  APP_PID=$!
}
if [ -f hosting.json ]; then
  CMD=$(python3 -c "import json;print((json.load(open('hosting.json')).get('cmd') or '').strip())" 2>/dev/null)
  if [ -n "$CMD" ]; then launch "$CMD"; fi
fi
if [ -z "$LAUNCHED" ] && [ -f Procfile ]; then
  CMD=$(sed -n 's/^web:[[:space:]]*//p' Procfile | head -n 1)
  if [ -n "$CMD" ]; then launch "$CMD"; fi
fi
if [ -z "$LAUNCHED" ] && [ -f package.json ]; then
  (npm ci || npm install) >>"$LOG" 2>&1
  if node -e "process.exit((require('./package.json').scripts||{}).start?0:1)"; then
    launch "npm start"
  else
    for f in index.js server.js main.js app.js; do
      if [ -f "$f" ]; then launch "node $f"; break; fi
    done
  fi
fi
if [ -z "$LAUNCHED" ] && [ -f requirements.txt ]; then
  python3 -m pip install -r requirements.txt >>"$LOG" 2>&1
  for f in main.py bot.py app.py server.py run.py; do
    if [ -f "$f" ]; then launch "python3 $f"; break; fi
  done
fi
if [ -z "$LAUNCHED" ]; then
  for f in main.py bot.py app.py server.py run.py index.js server.js main.js app.js; do
    if [ -f "$f" ]; then
      case "$f" in *.py) launch "python3 $f" ;; *) launch "node $f" ;; esac
      break
    fi
  done
fi
if [ -z "$LAUNCHED" ]; then
  DOCROOT=""
  if [ -f public/index.php ]; then DOCROOT="public"; elif [ -f index.php ]; then DOCROOT="."; fi
  if [ -n "$DOCROOT" ]; then
    if [ -f composer.json ]; then composer install --no-interaction >>"$LOG" 2>&1; fi
    launch "php -S 0.0.0.0:8080 -t $DOCROOT"
  fi
fi
if [ -z "$LAUNCHED" ]; then
  set -- ./*.py
  if [ "$#" -eq 1 ] && [ -f "$1" ]; then launch "python3 $1"; fi
fi
{
  echo "checked_at=$(date -u +%FT%TZ)"
  if [ -z "$LAUNCHED" ]; then
    echo "status=no_command"
    echo "launched_cmd="
  else
    echo "launched_cmd=$LAUNCHED"
    sleep 10
    if [ -n "$APP_PID" ] && kill -0 "$APP_PID" 2>/dev/null; then
      echo "alive=yes"
      echo "status=running"
    else
      echo "alive=no"
      echo "status=died"
    fi
    PORTS=$(ss -tln 2>/dev/null | awk 'NR>1 {print $4}' | sed 's/.*://' | sort -un | tr '
' ' ')
    echo "ports=$PORTS"
    for p in 3000 5000 7860 8000 8080 8501; do
      case " $PORTS " in
        *" $p "*)
          code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$p/" 2>/dev/null)
          echo "http_$p=${code:-000}"
          ;;
      esac
    done
  fi
  echo "--- app log tail ---"
  tail -n 15 "$APPLOG" 2>/dev/null
} > .zero-status.txt
git config user.email "hosting@zero.bot" >/dev/null 2>&1
git config user.name "ZERO Hosting" >/dev/null 2>&1
git add .zero-status.txt >/dev/null 2>&1
git commit -m "ZERO run status" >/dev/null 2>&1
timeout 90 git push >/dev/null 2>&1
echo "zero-start done"
exit 0
