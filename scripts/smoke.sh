#!/usr/bin/env bash
# Astoria post-deploy smoke test — run from anywhere on the LAN.
#   ASTORIA_URL=http://192.168.1.134:8933 scripts/smoke.sh
# Checks: /health 200 + db ok + tei ok · MCP initialize + tools/list handshake at /mcp/ ·
#         T1 quick (POST /facts Guinness → POST /correct IPA → GET /facts active == IPA only →
#         POST /recall mentions IPA, never Guinness) against a throwaway user, then DELETE /users/{id}.
# Prints one PASS/FAIL line per check; exit 0 iff every check passed. Needs curl + python3 (json parsing).
set -u
ASTORIA_URL="${ASTORIA_URL:-http://192.168.1.134:8933}"
ASTORIA_URL="${ASTORIA_URL%/}"
USER_ID="smoke-$RANDOM$RANDOM"
CLIENT_HDR="X-Astoria-Client: smoke"
CURL="curl -sS -m 30 -H Content-Type:application/json -H $CLIENT_HDR"
fails=0

pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; fails=$((fails+1)); }
jget() { python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    if isinstance(d,list): d=d[int(k)]
    else: d=d.get(k) if isinstance(d,dict) else None
    if d is None: break
print(json.dumps(d) if isinstance(d,(dict,list)) else ("" if d is None else d))' "$1" 2>/dev/null; }

echo "== Astoria smoke @ $ASTORIA_URL (user $USER_ID)"

# --- 1. /health ---------------------------------------------------------------------------------
code=$(curl -s -m 10 -o /tmp/astoria_smoke_health.$$ -w '%{http_code}' "$ASTORIA_URL/health" 2>/dev/null || echo 000)
health=$(cat /tmp/astoria_smoke_health.$$ 2>/dev/null); rm -f /tmp/astoria_smoke_health.$$
if [ "$code" = "200" ]; then pass "/health 200"; else fail "/health HTTP $code"; echo "$health" | head -c 300; echo; echo "== RESULT: FAIL (server unreachable)"; exit 1; fi
status=$(printf '%s' "$health" | jget status)
[ "$status" = "ok" ] && pass "health.status=ok" || fail "health.status=$status"
dbok=$(printf '%s' "$health" | jget db)
[ -n "$dbok" ] && [ "$dbok" != "null" ] && pass "health.db present ($(printf '%s' "$dbok" | head -c 80))" || fail "health.db missing"
teiok=$(printf '%s' "$health" | jget tei.ok)
[ "$teiok" = "True" ] || [ "$teiok" = "true" ] && pass "health.tei.ok" || fail "health.tei.ok=$teiok (vector recall degraded)"
ver=$(printf '%s' "$health" | jget version); [ -n "$ver" ] && pass "version $ver" || fail "health.version missing"
qp=$(printf '%s' "$health" | jget queue.pending); qd=$(printf '%s' "$health" | jget queue.dead)
[ -n "$qp" ] && pass "queue pending=$qp dead=${qd:-?}" || fail "health.queue missing"

# --- 2. MCP handshake ------------------------------------------------------------------------------
mcp_init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
hdrs=$(mktemp); body=$(mktemp)
code=$(curl -s -m 20 -o "$body" -D "$hdrs" -w '%{http_code}' -X POST "$ASTORIA_URL/mcp/" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$mcp_init" 2>/dev/null || echo 000)
sid=$(grep -i '^mcp-session-id:' "$hdrs" | tr -d '\r' | awk '{print $2}')
if [ "$code" = "200" ] && grep -q '"serverInfo"' "$body"; then
  pass "MCP initialize (session ${sid:-<stateless>})"
  # initialized notification (required by spec before tools/list), then tools/list
  curl -s -m 10 -o /dev/null -X POST "$ASTORIA_URL/mcp/" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' ${sid:+-H "Mcp-Session-Id: $sid"} \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1
  curl -s -m 20 -o "$body" -X POST "$ASTORIA_URL/mcp/" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' ${sid:+-H "Mcp-Session-Id: $sid"} \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' >/dev/null 2>&1
  tools=$(grep -o '"name":"[a-z_]*"' "$body" | sed 's/"name":"//;s/"//' | sort -u | tr '\n' ' ')
  missing=""
  for t in recall capture remember forget memory retrieve_memory add_memory get_user_profile; do
    echo " $tools " | grep -q " $t " || missing="$missing $t"
  done
  if [ -z "$missing" ]; then pass "MCP tools/list: $tools"; else fail "MCP tools/list missing:$missing (have: $tools)"; fi
else
  fail "MCP initialize at /mcp/ (HTTP $code)"; head -c 300 "$body"; echo
fi
rm -f "$hdrs" "$body"

# --- 3. T1 quick: facts → correct → recall ---------------------------------------------------------
r=$($CURL -X POST "$ASTORIA_URL/facts" -d "{\"user_id\":\"$USER_ID\",\"subject\":\"$USER_ID\",\"predicate\":\"favorite_beer\",\"value\":\"Guinness\"}")
act=$(printf '%s' "$r" | jget action); old_id=$(printf '%s' "$r" | jget fact.id)
[ "$act" = "inserted" ] && pass "POST /facts Guinness → inserted" || { fail "POST /facts → ${act:-err}: $(printf '%s' "$r" | head -c 200)"; }
r=$($CURL -X POST "$ASTORIA_URL/correct" -d "{\"user_id\":\"$USER_ID\",\"subject\":\"$USER_ID\",\"predicate\":\"favorite_beer\",\"value\":\"IPA\"}")
act=$(printf '%s' "$r" | jget action); sup=$(printf '%s' "$r" | jget superseded)
if [ "$act" = "superseded" ] && printf '%s' "$sup" | grep -q "$old_id"; then pass "POST /correct IPA → superseded $old_id"; else fail "POST /correct → ${act:-err}: $(printf '%s' "$r" | head -c 200)"; fi
r=$($CURL "$ASTORIA_URL/facts?user_id=$USER_ID&predicate=favorite_beer&status=active")
vals=$(printf '%s' "$r" | python3 -c 'import json,sys; d=json.load(sys.stdin); d=d.get("facts",d) if isinstance(d,dict) else d; print(",".join(sorted(f["value"] for f in d)))' 2>/dev/null)
[ "$vals" = "IPA" ] && pass "GET /facts active == [IPA]" || fail "GET /facts active == [$vals]"
r=$($CURL "$ASTORIA_URL/facts/$old_id?user_id=$USER_ID")
st=$(printf '%s' "$r" | jget status); sb=$(printf '%s' "$r" | jget superseded_by)
[ "$st" = "superseded" ] && [ -n "$sb" ] && pass "old row superseded (superseded_by=$sb)" || fail "old row status=$st superseded_by=$sb"
r=$($CURL -X POST "$ASTORIA_URL/recall" -d "{\"user_id\":\"$USER_ID\",\"query\":\"what beer do I like\"}")
top=$(printf '%s' "$r" | jget items.0.value); ctx=$(printf '%s' "$r" | jget context)
if printf '%s' "$ctx" | grep -q Guinness; then fail "recall context still mentions Guinness"; fi
if [ "$top" = "IPA" ]; then pass "POST /recall items[0].value == IPA"; else
  if printf '%s' "$ctx" | grep -q IPA; then pass "POST /recall context mentions IPA (top=$top)"; else fail "POST /recall top=$top ctx=$(printf '%s' "$ctx" | head -c 120)"; fi
fi
# detector path (no LLM)
r=$($CURL -X POST "$ASTORIA_URL/capture" -d "{\"user_id\":\"$USER_ID\",\"kind\":\"note\",\"text\":\"Actually, my favorite beer is stout\",\"cognify\":false}")
dv=$(printf '%s' "$r" | jget detector.value); da=$(printf '%s' "$r" | jget detector.action)
[ "$dv" = "stout" ] && pass "capture detector → favorite_beer=stout ($da)" || fail "capture detector: $(printf '%s' "$r" | head -c 200)"

# --- 4. wipe --------------------------------------------------------------------------------------
code=$(curl -s -m 30 -o /dev/null -w '%{http_code}' -X DELETE -H "$CLIENT_HDR" "$ASTORIA_URL/users/$USER_ID" 2>/dev/null || echo 000)
[ "$code" = "200" ] && pass "DELETE /users/$USER_ID" || fail "DELETE /users/$USER_ID HTTP $code"
r=$($CURL "$ASTORIA_URL/facts?user_id=$USER_ID&status=any")
n=$(printf '%s' "$r" | python3 -c 'import json,sys; d=json.load(sys.stdin); d=d.get("facts",d) if isinstance(d,dict) else d; print(len(d))' 2>/dev/null)
[ "$n" = "0" ] && pass "user wiped (0 facts)" || fail "user still has $n facts after wipe"

if [ "$fails" -eq 0 ]; then echo "== RESULT: PASS"; exit 0; else echo "== RESULT: FAIL ($fails)"; exit 1; fi
