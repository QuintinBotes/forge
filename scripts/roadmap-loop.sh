#!/usr/bin/env bash
# Implements open `roadmap` issues one at a time with Orbit: one issue, one run, one PR.
# A person starts this (in tmux). It stops on the first run that does not succeed.
#
#   scripts/roadmap-loop.sh             run the queue
#   scripts/roadmap-loop.sh --dry-run   print the issue it would take next, then exit
#
# AUTO_MERGE=1 turns on GitHub auto-merge for each PR; by default a person merges.
set -euo pipefail

ORBIT=${ORBIT:-orbit}
SKIP_LABELS='owner-decision needs-credentials blocked'
POLL_SECONDS=${POLL_SECONDS:-120}
# Milestones in execution order (roadmap section 4); within one, lowest issue number first.
MILESTONE_ORDER='["M0","M1","M6","M3","M2","M4","M5","M7","M9","M8","M10"]'

# First open roadmap issue, in milestone order, with no skip label and no open dependency.
# Dependencies are the #N references on the issue's "Depends on:" line.
next_issue() {
  local n labels deps d
  while read -r n labels; do
    for l in $SKIP_LABELS; do [[ ",$labels," == *",$l,"* ]] && continue 2; done
    deps=$(gh issue view "$n" --json body --jq '.body' | grep -i '^- Depends on:' | grep -o '#[0-9]\+' | tr -d '#' || true)
    for d in $deps; do
      [[ $(gh issue view "$d" --json state --jq '.state') == CLOSED ]] || continue 2
    done
    echo "$n"; return 0
  done < <(gh issue list --label roadmap --state open --limit 200 --json number,labels,milestone \
            --jq "$MILESTONE_ORDER as \$o | sort_by([((.milestone.title // \"\" | split(\" \")[0]) as \$m | \$o | index(\$m)) // 99, .number])[] | \"\(.number) \([.labels[].name] | join(\",\"))\"")
  return 1
}

if [[ ${1:-} == --dry-run ]]; then
  next_issue || echo "no runnable roadmap issue"
  exit 0
fi

while n=$(next_issue); do
  echo "== roadmap issue #$n"
  git switch -q main && git pull -q --ff-only
  [[ -z $(git status --porcelain) ]] || { echo "working tree is not clean"; exit 1; }

  before=$(gh pr list --state all --limit 1 --json number --jq '.[0].number // 0')
  if ! gh issue view "$n" --json title,body --jq '"# \(.title)\n\n\(.body)"' \
       | "$ORBIT" run --foreground --goal -; then
    gh issue edit "$n" --add-label blocked >/dev/null
    gh issue comment "$n" --body "The roadmap loop stopped here: the Orbit run did not succeed. See \`orbit status\` and \`orbit report\` on the machine that ran it." >/dev/null
    echo "run for #$n did not succeed; labelled blocked and stopping"; exit 1
  fi

  # Orbit's PR body carries no issue link, so add it here.
  pr=$(gh pr list --state open --limit 20 --json number,headRefName \
        --jq "[.[] | select(.headRefName | startswith(\"orbit/\")) | select(.number > $before)] | max_by(.number) | .number // empty")
  [[ -n $pr ]] || { echo "run for #$n succeeded but no new orbit/ PR was found; stopping"; exit 1; }
  gh pr edit "$pr" --body "$(gh pr view "$pr" --json body --jq '.body')

Closes #$n" >/dev/null
  [[ ${AUTO_MERGE:-0} == 1 ]] && gh pr merge "$pr" --auto --squash

  echo "PR #$pr delivers #$n; waiting for it to be merged"
  until [[ $(gh pr view "$pr" --json state --jq '.state') != OPEN ]]; do sleep "$POLL_SECONDS"; done
  [[ $(gh pr view "$pr" --json state --jq '.state') == MERGED ]] || { echo "PR #$pr was closed unmerged; stopping"; exit 1; }
done
echo "no runnable roadmap issue left"
