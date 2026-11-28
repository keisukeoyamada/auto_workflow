#!/bin/sh
# Pull-based deploy for a home NAS: run every few minutes (Synology: Task Scheduler, user root).
# When origin/main has a new commit AND its GitHub checks (CI) all passed, fast-forward and rebuild.
# Nothing on GitHub can reach the NAS; no secrets are needed (public repo, read-only API).
#
#   sh /volume1/docker/auto_workflow/scripts/nas-autodeploy.sh            # this repo
#   AUTODEPLOY_BRANCH=main sh scripts/nas-autodeploy.sh /path/to/repo
#
# Safety: never deploys a commit whose checks failed or are still running; never touches local changes
# (tracked files) or untracked data (.env, config/, workspace/); only fast-forwards; if the rebuild fails it
# goes back to the previous commit, rebuilds that, and does not retry the same commit.
# Log: <repo>/logs/autodeploy.log
set -eu
export PATH="$PATH:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"  # Synology task: docker is in /usr/local/bin

REPO=$(cd "${1:-$(dirname "$0")/..}" && pwd)
BRANCH="${AUTODEPLOY_BRANCH:-main}"
LOG="$REPO/logs/autodeploy.log"
SKIP="$REPO/logs/autodeploy.skip"    # commit we gave up on (failed CI or failed build): not retried
LOCK="${TMPDIR:-/tmp}/nakama-autodeploy.lock"

mkdir -p "$REPO/logs"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }
mkdir "$LOCK" 2>/dev/null || exit 0          # another run is still going
trap 'rmdir "$LOCK"' EXIT INT TERM

# Run git as the owner of the checkout so pulled files stay theirs (the task runs as root for docker).
OWNER=$(ls -ld "$REPO" | awk '{print $3}')
g() {
  if [ "$(id -u)" = 0 ] && [ "$OWNER" != root ]; then sudo -u "$OWNER" git -C "$REPO" "$@"
  else git -C "$REPO" "$@"; fi
}

g fetch -q origin "$BRANCH" || { log "git fetch failed"; exit 0; }
LOCAL=$(g rev-parse HEAD)
REMOTE=$(g rev-parse "origin/$BRANCH")
[ "$LOCAL" = "$REMOTE" ] && exit 0
[ -f "$SKIP" ] && [ "$(cat "$SKIP")" = "$REMOTE" ] && exit 0

# The commit must have finished, successful checks on GitHub.
SLUG=$(g remote get-url origin | sed -E 's#^(git@github\.com:|https://([^@/]+@)?github\.com/)##; s#\.git$##')
JSON=$(curl -fsS -m 20 -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$SLUG/commits/$REMOTE/check-runs?per_page=100") || { log "GitHub API not reachable"; exit 0; }
TOTAL=$(printf '%s' "$JSON" | grep -o '"total_count": *[0-9]*' | head -n1 | grep -o '[0-9]*$' || echo 0)
[ "${TOTAL:-0}" -gt 0 ] || exit 0           # no checks yet (CI not started): try again next time
RUNNING=$(printf '%s' "$JSON" | grep -o '"status": *"[a-z_]*"' | grep -vc '"completed"' || true)
[ "$RUNNING" -eq 0 ] || exit 0              # still running
BAD=$(printf '%s' "$JSON" | grep -o '"conclusion": *"[a-z_]*"' | grep -Evc '"(success|skipped|neutral)"' || true)
if [ "$BAD" -gt 0 ]; then
  log "CI did not pass for $REMOTE; not deploying it"
  echo "$REMOTE" > "$SKIP"; exit 0
fi

if ! g diff --quiet || ! g diff --cached --quiet; then
  log "local changes to tracked files; not deploying (commit or discard them)"; exit 0
fi
g merge --ff-only -q "origin/$BRANCH" || { log "cannot fast-forward to $REMOTE; not deploying"; exit 0; }

log "deploying $LOCAL -> $REMOTE"
cd "$REPO"
build_info() { GIT_COMMIT=$(g rev-parse HEAD); GIT_COMMIT_DATE=$(g log -1 --format=%cI); export GIT_COMMIT GIT_COMMIT_DATE; }
build_info   # the dashboard footer shows which commit is running
if docker compose up -d --build >> "$LOG" 2>&1; then
  log "deployed $REMOTE"
  rm -f "$SKIP"
else
  log "rebuild failed for $REMOTE; going back to $LOCAL"
  echo "$REMOTE" > "$SKIP"
  g reset -q --hard "$LOCAL"
  build_info
  docker compose up -d --build >> "$LOG" 2>&1 || log "rebuild of $LOCAL also failed; check the log"
fi
