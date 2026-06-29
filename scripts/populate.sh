#!/bin/bash
# Sequentially run scraper batches (avoids the concurrency-lock cancellations),
# then pull the fresh DB and redeploy to Vercel after each batch so the live
# site fills with real listings incrementally.
set -u
cd "$(dirname "$0")/.." || exit 1

active() {
  gh run list --workflow=scrape.yml --limit 8 --json status --jq '.[].status' \
    | grep -qE 'in_progress|pending|queued'
}

drain() { while active; do sleep 90; done; }

echo "[populate] waiting for any in-flight runs to finish..."
drain
git pull --no-edit -q || true

for off in 700 980 1260 1540 1820; do
  echo "[populate] dispatching batch offset=$off"
  gh workflow run scrape.yml -f batch=140 -f max_results=15 -f offset="$off"
  sleep 30
  drain
  git pull --no-edit -q || true
  COUNT=$(python -c "import sys;sys.path.insert(0,'scraper');import db;print(db.count(db.connect()))" 2>/dev/null)
  echo "[populate] batch offset=$off done. total listings=$COUNT -> redeploying"
  vercel deploy --prod --yes >/dev/null 2>&1 && echo "[populate] redeployed."
done

echo "[populate] ALL DONE. final listings=$COUNT"
