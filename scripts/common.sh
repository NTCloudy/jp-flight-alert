#!/usr/bin/env bash
# Price data and notification state live on a separate `data` branch, so scheduled runs never
# touch `main` (where the web console edits config.yaml). Each run replaces that branch with a
# single commit, which keeps the repository small.
set -u
export GIT_AUTHOR_NAME="flight-bot" GIT_AUTHOR_EMAIL="flight-bot@users.noreply.github.com"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
DATA_FILES="state.json calendar.json history.json latest.json"

pull_data() {
  mkdir -p data
  if git fetch -q --depth 1 origin data 2>/dev/null; then
    for f in $DATA_FILES; do
      git show "origin/data:$f" > "data/$f" 2>/dev/null || rm -f "data/$f"
    done
  fi
}

push_data() {
  local entries="" f blob tree commit
  for f in $DATA_FILES; do
    [ -s "data/$f" ] || continue
    blob=$(git hash-object -w "data/$f")
    entries+="100644 blob ${blob}	${f}"$'\n'
  done
  [ -n "$entries" ] || return 0
  tree=$(printf '%s' "$entries" | git mktree)
  commit=$(git commit-tree "$tree" -m "Update data $(date -u +%Y-%m-%dT%H:%MZ)")
  git push -q -f origin "$commit:refs/heads/data" || echo "::warning::data push failed"
}

keepalive() {
  # GitHub disables scheduled workflows after 60 days without repository activity.
  local last
  last=$(git log -1 --format=%ct HEAD 2>/dev/null || echo 0)
  if [ $(( $(date +%s) - last )) -gt $(( 50 * 86400 )) ]; then
    git commit -q --allow-empty -m "Keepalive: keep scheduled checks enabled" && git push -q origin HEAD:main || true
  fi
}
