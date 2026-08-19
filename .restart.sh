#!/bin/sh
# Restart the dev server, reliably freeing port 8000 first.
PORT=${1:-8000}
LOG="C:/Users/Fathy/AppData/Local/Temp/claude/D--WORK-Wanas-chatbot/7675dff6-0057-4668-8640-3e19f42d544a/scratchpad/server.log"
PID=$(netstat -ano | grep -E "TCP.*127\.0\.0\.1:$PORT.*LISTENING" | awk '{print $NF}' | head -1)
[ -n "$PID" ] && taskkill //F //PID "$PID" > /dev/null 2>&1
sleep 1.5
rm -f "$LOG"
"C:/Users/Fathy/anaconda3/envs/wanas/python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > "$LOG" 2>&1 &
for i in $(seq 1 90); do curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/health" && break; sleep 0.5; done
grep -E "Catalog:|Startup complete|Errno" "$LOG"
