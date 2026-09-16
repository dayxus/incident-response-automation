#!/usr/bin/env bash
# End-to-end demo: boot the API, deliver the committed Alertmanager payloads in
# the order the playbook describes, acknowledge the incident, and print the
# postmortem incidentd generated from the recorded timeline.
#
# No network access beyond 127.0.0.1: the notifier is the stdout notifier.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"
HOST="${DEMO_HOST:-127.0.0.1}"
PORT="${DEMO_PORT:-8099}"
BASE_URL="http://${HOST}:${PORT}"
OUT_DIR="${DEMO_OUT_DIR:-${REPO_ROOT}/var/demo}"
PAYLOADS="${REPO_ROOT}/examples/alertmanager"
SUMMARY_FILE="${OUT_DIR}/summary.txt"
POSTMORTEM_FILE="${OUT_DIR}/postmortem.md"

mkdir -p "$OUT_DIR"
rm -f "${OUT_DIR}/incidents.db" "${OUT_DIR}/incidents.db-wal" "${OUT_DIR}/incidents.db-shm"
: > "$SUMMARY_FILE"

say() { printf '%s\n' "$*" | tee -a "$SUMMARY_FILE"; }

# One line per delivery, so the output shows incident ids without the raw JSON.
describe() {
  "$PYTHON" -c '
import json, sys
body = json.load(sys.stdin)
print("incident=%s status=%s deduplicated=%s action=%s severity=%s service=%s"
      % (body["incident_id"], body["status"], str(body["deduplicated"]).lower(),
         body["results"][0]["action"], body["results"][0]["severity"],
         body["results"][0]["service"]))
'
}

deliver() {
  local name="$1"
  local response
  response="$(curl -fsS -X POST "${BASE_URL}/webhook/alertmanager" \
    -H 'Content-Type: application/json' \
    --data-binary "@${PAYLOADS}/${name}")"
  say "  ${name} -> $(printf '%s' "$response" | describe)"
}

echo "incidentd demo — synthetic incident 2026-03-04 (see examples/playbook/README.md)"
say "server: ${BASE_URL}  database: ${OUT_DIR}/incidents.db"

"$PYTHON" -m incidentd serve --host "$HOST" --port "$PORT" \
  --db "${OUT_DIR}/incidents.db" --policy policy/policy.yaml \
  --notifier stdout --log-level warning &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  if curl -fsS "${BASE_URL}/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.25
done
say "healthz: $(curl -fsS "${BASE_URL}/healthz")"

say ""
say "1. Alertmanager deliveries"
deliver 01-firing-checkout-api.json
CHECKOUT_ID="$("$PYTHON" -c '
import json, subprocess, sys
# The first incident of the replay is the checkout-api one; ask the API for it.
body = json.loads(subprocess.check_output(
    ["curl", "-fsS", sys.argv[1] + "/incidents?service=checkout-api"]))
print(body["incidents"][0]["id"])
' "$BASE_URL")"
deliver 02-firing-checkout-api-repeat.json

say ""
say "2. Operator acknowledgement of ${CHECKOUT_ID} (recorded at 2026-03-04T13:06:10Z, deadline 5m)"
say "  $(curl -fsS -X POST "${BASE_URL}/incidents/${CHECKOUT_ID}/ack" \
  -H 'Content-Type: application/json' \
  -d '{"actor":"oncall-alice","at":"2026-03-04T13:06:10Z"}' \
  | "$PYTHON" -c 'import json,sys; b=json.load(sys.stdin); print("status=%s mttd=%ss mttd_state=%s owner=%s route=%s" % (b["status"], b["mttd_seconds"], b["ack_deadline_state"], b["owner"], b["route"]))')"

say ""
say "3. The other services keep failing while ${CHECKOUT_ID} is acknowledged"
deliver 03-firing-search-api.json
deliver 05-firing-payments-worker.json
deliver 04-resolved-checkout-api.json

say ""
say "4. Incidents in the store"
"$PYTHON" -m incidentd list --db "${OUT_DIR}/incidents.db" | tee -a "$SUMMARY_FILE"

say ""
say "5. Prometheus exposition (/metrics)"
curl -fsS "${BASE_URL}/metrics" > "${OUT_DIR}/metrics.txt"
"$PYTHON" -c '
import sys
for line in sys.stdin.read().splitlines():
    if line and not line.startswith("#"):
        print("  " + line)
' < "${OUT_DIR}/metrics.txt" | tee -a "$SUMMARY_FILE"

say ""
say "6. Generated postmortem (${POSTMORTEM_FILE})"
curl -fsS "${BASE_URL}/incidents/${CHECKOUT_ID}/postmortem.md" > "$POSTMORTEM_FILE"
cat "$POSTMORTEM_FILE"

say ""
say "artifacts: ${POSTMORTEM_FILE} ${SUMMARY_FILE}"
