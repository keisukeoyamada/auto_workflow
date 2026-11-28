#!/bin/sh
# Pull-based deploy for a home NAS: run every few minutes (Synology: Task Scheduler, user root).
# When origin/main has a new commit AND its GitHub checks (CI) all passed, fast-forward and rebuild.
# Nothing on GitHub can reach the NAS; no secrets are needed (public repo, read-only API).
#
#   sh /volume1/docker/auto_workflow/scripts/nas-autodeploy.sh            # this repo
#   AUTODEPLOY_BRANCH=main sh scripts/nas-autodeploy.sh /path/to/repo
#
# Safety: never deploys a commit whose checks failed or are still running; never touches local changes
# (tracked files) or untracked data (.env, config/, workspace/); only fast-forwards; if the rebuild fails or the
# dashboard does not answer ~90 s after the restart, it goes back to the previous commit, rebuilds that, and does
# not retry the same commit. Network calls have time limits; a run that died leaves a lock that expires in 30 min.
# Every run records when it looked and what it found in logs/autodeploy-check.json (shown in the footer).
# Exit code: 0 = fine (nothing new / waiting for CI / deployed), 1 = needs a look (the Synology task then shows
# 異常終了; tick "send run details only when the script terminates abnormally" to get an e-mail).
# Log: <repo>/logs/autodeploy.log
set -eu
export PATH="$PATH:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:/var/packages/Git/target/bin"
# (Synology task: docker is in /usr/local/bin; git from the Git Server package may be under /var/packages)

REPO=$(cd "${1:-$(dirname "$0")/..}" && pwd)
BRANCH="${AUTODEPLOY_BRANCH:-main}"
LOG="$REPO/logs/autodeploy.log"
SKIP="$REPO/logs/autodeploy.skip"    # commit we gave up on (failed CI or failed build): not retried
LOCK="${TMPDIR:-/tmp}/nakama-autodeploy.lock"
STATE="$REPO/logs/autodeploy-state.json"  # read by the dashboard: "反映中… あと約 N 分"
CHECK="$REPO/logs/autodeploy-check.json"  # read by the dashboard: when this task last looked, and what it found
DEPLOYED="$REPO/logs/autodeploy.deployed" # the commit that was last rebuilt successfully (not just checked out)
LOCK_STALE_MIN=30                         # a lock older than this is from a run that died: take it over
TO=$(command -v timeout || true)          # time limit for network calls (git fetch can hang on a bad connection)

mkdir -p "$REPO/logs"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }
RESULT="started"
checked() { printf '{"at": %s, "result": "%s"}\n' "$(date +%s)" "$RESULT" > "$CHECK" 2>/dev/null; chmod 644 "$CHECK" 2>/dev/null || true; }
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +$LOCK_STALE_MIN 2>/dev/null)" ]; then
    log "previous run's lock is older than $LOCK_STALE_MIN min (that run died); taking over"
    rmdir "$LOCK" 2>/dev/null; mkdir "$LOCK" 2>/dev/null || exit 0
  elif [ -n "$(find "$LOCK" -maxdepth 0 -mmin +15 2>/dev/null)" ]; then
    log "previous run has been going for over 15 min (since $(date -r "$LOCK" '+%H:%M:%S' 2>/dev/null)); stuck?"
    exit 1                                  # raise: the task shows "異常終了" (and can e-mail)
  else
    log "previous run still going (since $(date -r "$LOCK" '+%H:%M:%S' 2>/dev/null)); skipping this time"
    exit 0                                  # normal while a build runs
  fi
fi
trap 'checked; rmdir "$LOCK"' EXIT
# Canceled (Ctrl+C, task stopped, NAS shutting down): say so, and do not leave "反映中" on the dashboard.
trap 'RESULT="interrupted"; log "interrupted"; [ -n "${START:-}" ] && state failed "$LAST"; exit 1' INT TERM HUP

# Run git as the owner of the checkout so pulled files stay theirs (the task runs as root for docker).
# sudo resets PATH (secure_path), so git is called by its full path, with the owner's HOME for its settings.
GIT=$(command -v git || true)
[ -n "$GIT" ] || { log "git not found (PATH=$PATH)"; exit 0; }
OWNER=$(ls -ld "$REPO" | awk '{print $3}')
OWNER_HOME=$(eval echo "~$OWNER")
[ -d "$OWNER_HOME" ] || OWNER_HOME=/tmp
g() {
  if [ "$(id -u)" = 0 ] && [ "$OWNER" != root ]; then
    sudo -n -u "$OWNER" env HOME="$OWNER_HOME" PATH="$PATH" GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=30 \
      $TO ${TO:+180} "$GIT" -C "$REPO" "$@"
  else GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=30 $TO ${TO:+180} "$GIT" -C "$REPO" "$@"; fi
}
# The last lines of a failed command, on one log line (so the log says why).
why() { printf '%s' "$1" | tail -n 3 | tr '\n' ' ' | cut -c1-300; }

# Synology drops "@eaDir" metadata folders (…@SynoEAStream) even inside .git; git then reads them as broken
# refs ("fatal: bad object refs/remotes/origin/@eaDir/…") and every fetch fails. They are never git's own: remove.
if [ -n "$(find "$REPO/.git" -name '@eaDir' -type d -print -quit 2>/dev/null)" ]; then
  find "$REPO/.git" -name '@eaDir' -type d -prune -exec rm -rf {} + && log "removed Synology @eaDir folders from .git"
fi

RESULT="fetch failed"
OUT=$(g fetch -q origin "$BRANCH" 2>&1) || { log "git fetch failed: $(why "$OUT")"; exit 1; }
REMOTE=$(g rev-parse "origin/$BRANCH")
# "Deployed" means rebuilt, not checked out: a run canceled after the fast-forward but before the rebuild
# left the checkout new and the containers old, and comparing HEAD made every later run think it was done.
LOCAL=$(cat "$DEPLOYED" 2>/dev/null || true)
[ -n "$LOCAL" ] || { LOCAL=$(g rev-parse HEAD); echo "$LOCAL" > "$DEPLOYED"; }  # first run: trust the checkout
RESULT="up to date"
[ "$LOCAL" = "$REMOTE" ] && exit 0
RESULT="skipped (failed before)"
[ -f "$SKIP" ] && [ "$(cat "$SKIP")" = "$REMOTE" ] && exit 0   # already raised once for this commit

# The commit must have finished, successful checks on GitHub.
SLUG=$(g remote get-url origin | sed -E 's#^(git@github\.com:|https://([^@/]+@)?github\.com/)##; s#\.git$##')
JSON=$(curl -fsS -m 20 -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$SLUG/commits/$REMOTE/check-runs?per_page=100") || { RESULT="GitHub API not reachable"; log "$RESULT"; exit 1; }
RESULT="waiting for CI"
TOTAL=$(printf '%s' "$JSON" | grep -o '"total_count": *[0-9]*' | head -n1 | grep -o '[0-9]*$' || echo 0)
[ "${TOTAL:-0}" -gt 0 ] || exit 0           # no checks yet (CI not started): try again next time
RUNNING=$(printf '%s' "$JSON" | grep -o '"status": *"[a-z_]*"' | grep -vc '"completed"' || true)
[ "$RUNNING" -eq 0 ] || exit 0              # still running
BAD=$(printf '%s' "$JSON" | grep -o '"conclusion": *"[a-z_]*"' | grep -Evc '"(success|skipped|neutral)"' || true)
if [ "$BAD" -gt 0 ]; then
  RESULT="CI failed"; log "CI did not pass for $REMOTE; not deploying it"
  echo "$REMOTE" > "$SKIP"; exit 1
fi

if ! g diff --quiet || ! g diff --cached --quiet; then
  RESULT="local changes"; log "local changes to tracked files; not deploying (commit or discard them)"; exit 1
fi
RESULT="deploying"
OUT=$(g merge --ff-only -q "origin/$BRANCH" 2>&1) || { RESULT="cannot fast-forward"; log "cannot fast-forward to $REMOTE; not deploying: $(why "$OUT")"; exit 1; }

log "deploying $LOCAL -> $REMOTE"
cd "$REPO"
# How long the last deploy took (seconds) is the estimate for this one.
LAST=$(sed -n 's/.*"seconds": *\([0-9][0-9]*\).*/\1/p' "$STATE" 2>/dev/null | head -n1)
[ -n "$LAST" ] || LAST=180
START=$(date +%s)
state() { printf '{"state": "%s", "to": "%s", "started": %s, "seconds": %s}\n' "$1" "$REMOTE" "$START" "$2" > "$STATE"; chmod 644 "$STATE"; }
state deploying "$LAST"
build_info() {
  GIT_COMMIT=$(g rev-parse HEAD); GIT_COMMIT_DATE=$(g log -1 --format=%cI); GIT_REPO_URL="https://github.com/$SLUG"
  export GIT_COMMIT GIT_COMMIT_DATE GIT_REPO_URL
}
# Health check: the new dashboard must answer inside its container within ~90 s (the login page).
healthy() {
  i=0
  while [ $i -lt "${AUTODEPLOY_HEALTH_TRIES:-18}" ]; do
    sleep "${AUTODEPLOY_HEALTH_WAIT:-5}"; i=$((i + 1))
    docker compose exec -T nakama python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/login', timeout=5)" >/dev/null 2>&1 && return 0
  done
  return 1
}
rollback() {
  log "$1; going back to $LOCAL"
  echo "$REMOTE" > "$SKIP"
  g reset -q --hard "$LOCAL"
  build_info
  docker compose up -d --build >> "$LOG" 2>&1 || log "rebuild of $LOCAL also failed; check the log"
  state failed "$LAST"
  RESULT="rolled back"
}
build_info   # the dashboard footer shows which commit is running
if ! docker compose up -d --build >> "$LOG" 2>&1; then
  rollback "rebuild failed for $REMOTE"
elif ! healthy; then
  rollback "$REMOTE did not answer after the restart (health check)"
else
  log "deployed $REMOTE"
  echo "$REMOTE" > "$DEPLOYED"
  rm -f "$SKIP"
  state done "$(( $(date +%s) - START ))"
  RESULT="deployed"
fi
[ "$RESULT" = "deployed" ] || exit 1          # rolled back: raise
