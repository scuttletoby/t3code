#!/usr/bin/env bash
# Keeps a public repository's interaction limit from lapsing (tests: fork-interaction-limit.test.sh).
# GitHub ends every interaction limit after at most six months; once it ends, anyone can open issues
# and comments again. Commands:
#
#   check  [--repo OWNER/NAME] [--limit LIMIT] [--days N] [--out FILE]
#       Reads the limit. Exit 0 when it is LIMIT and ends in more than N days (default 30); 1 when
#       it is missing, another limit or ends sooner; 2 when it cannot be read. --out writes the
#       result for `report`: first line ok, attention or error, then the message.
#   renew  [--repo OWNER/NAME] [--limit LIMIT] [--if-under N]
#       Sets LIMIT for six months (GitHub's longest), then reads it back and checks it took. With
#       --if-under N it only reads when LIMIT is already set and ends in N days or more. Exit 0
#       when the limit is (now) in place, 2 when it could not be set or read back. A timer that
#       renews once a month should use --if-under 75, the default margin for timers: at least
#       the check's margin (30) plus a month and a few days, so a monthly run never skips at 60
#       days left and lets the weekly check fall under 30 before the next run.
#   report FILE [--repo OWNER/NAME] [--limit LIMIT]
#       Opens one issue, "Interaction limit needs attention", when FILE (from check --out) is not ok,
#       or comments on the open one; closes it again on an ok result. It only ever touches an open
#       issue that GitHub Actions itself opened with exactly that title, never one someone else
#       opened. LIMIT goes into the renew command the issue shows. Exits non-zero when FILE is not
#       ok or the issue could not be handled.
#
# OWNER/NAME: in a GitHub Actions run, --repo or else GITHUB_REPOSITORY; anywhere else --repo is
# required. LIMIT is one of GitHub's existing_users, contributors_only, collaborators_only (default
# collaborators_only) and must match exactly: renew sets exactly that. Reading or setting the
# limit needs a token with the repository's Administration permission (read for check, write for
# renew); GitHub's workflow token has neither. A limit set for a whole user or organization
# (origin user or organization) cannot be changed per repository (GitHub answers 409): change it
# where it was set. Run on a person's machine only with FORK_GH_LOCAL=fork-interaction-limit.sh
# (fork-gh-guard.sh). Needs gh and jq.
set -uo pipefail
# Refuses outside a GitHub Actions run, a local run named by FORK_GH_LOCAL, or a test under the gh
# stub (fork-gh-guard.sh).
. "$(dirname "${BASH_SOURCE[0]}")/fork-gh-guard.sh"

LIMITS="existing_users contributors_only collaborators_only"
FRESH_DAYS=150 # a renewal reads back at least this many days left (six months is 181 or more)
TITLE="Interaction limit needs attention"
# The issue author: `gh issue list --author` needs the bot's user login, while gh's JSON names it
# app/github-actions. Both are checked; a person cannot take a login with brackets.
BOT_AUTHOR="github-actions[bot]"
BOT_LOGIN="app/github-actions"

die() { echo "fork-interaction-limit: $*" >&2; exit 2; }
usage() { sed -n '/^#   check/,/^#$/p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

now() { # tests pin the clock; nothing else may
  if [[ -n "${FORK_TEST_MODE:-}" && -n "${FORK_TEST_NOW:-}" ]]; then echo "$FORK_TEST_NOW"; else date +%s; fi
}

cmd="${1:-}"
shift || true
repo=""
limit=collaborators_only
days=30
if_under=""
out=""
file=""
[[ "$cmd" == report && $# -gt 0 && "$1" != --* ]] && { file="$1"; shift; }
while (( $# )); do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 || usage ;;
    --limit) limit="${2:-}"; shift 2 || usage ;;
    --days) days="${2:-}"; shift 2 || usage ;;
    --if-under) if_under="${2:-}"; shift 2 || usage ;;
    --out) out="${2:-}"; shift 2 || usage ;;
    *) usage ;;
  esac
done
# Outside Actions only --repo names the repository: a stray variable must never aim renew elsewhere.
if [[ -z "$repo" && "${GITHUB_ACTIONS:-}" == true ]]; then
  repo="${GITHUB_REPOSITORY:-}"
fi
[[ -n "$repo" ]] || die "no repository: give --repo OWNER/NAME (only a GitHub Actions run falls back to GITHUB_REPOSITORY)"
[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "the repository is OWNER/NAME, not '$repo'"
[[ " $LIMITS " == *" $limit "* ]] || die "--limit is one of $LIMITS, not '$limit'"
[[ "$days" =~ ^[0-9]+$ ]] || die "--days is a whole number of days, not '$days'"
[[ -z "$if_under" || "$if_under" =~ ^[0-9]+$ ]] || die "--if-under is a whole number of days, not '$if_under'"
days=$((10#$days)) # base 10: a leading zero is not octal
[[ -z "$if_under" ]] || if_under=$((10#$if_under))
command -v jq > /dev/null || die "jq is missing"

# read_limit: sets cur_limit, cur_origin, cur_expires (ISO time) and cur_days (whole days left,
# negative once past); all empty when no limit is set. Returns 1 with read_error set when the
# limit cannot be read. GitHub answers {} when no limit is set; a 403 means the token lacks
# Administration read (GitHub's workflow token always does). Never GraphQL: its interactionAbility
# reads null both for "no limit" and for "you may not see it".
read_limit() {
  local body err
  cur_limit="" cur_origin="" cur_expires="" cur_days="" read_error=""
  err="$(mktemp)"
  if ! body="$(gh api "repos/$repo/interaction-limits" 2> "$err")"; then
    read_error="could not read the interaction limit of $repo: $(tr '\n' ' ' < "$err" | cut -c1-300)"
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
  [[ -n "${body//[[:space:]]/}" ]] || body='{}'
  local parsed fields
  # One field per line, read by position with mapfile: an empty field (no origin) stays in place.
  # The last line is "." so that $(...) cannot strip trailing empty fields.
  if ! parsed="$(jq -r --argjson now "$(now)" '
      if type != "object" then error("not an object")
      elif (.limit // "") == "" then ["", "", "", ""]
      else [.limit, (.origin // ""), (.expires_at // ""),
            (if (.expires_at // "") == "" then "" else
               (((.expires_at | fromdateiso8601) - $now) / 86400 | floor) end)]
      end | . + ["."] | .[] | tostring | gsub("[\\r\\n]"; " ")' <<< "$body" 2> /dev/null)"; then
    read_error="GitHub's answer for the interaction limit of $repo is not one this script knows: $(head -c 200 <<< "$body")"
    return 1
  fi
  mapfile -t fields <<< "$parsed"
  if (( ${#fields[@]} != 5 )) || [[ "${fields[4]}" != . ]]; then
    read_error="GitHub's answer for the interaction limit of $repo is not one this script knows: $(head -c 200 <<< "$body")"
    return 1
  fi
  cur_limit="${fields[0]}" cur_origin="${fields[1]}" cur_expires="${fields[2]}" cur_days="${fields[3]}"
  if [[ -n "$cur_limit" && ! "$cur_days" =~ ^-?[0-9]+$ ]]; then
    read_error="the interaction limit of $repo has no readable end date ('$cur_expires')"
    return 1
  fi
  return 0
}

describe() { # the limit as read, in words
  if [[ -z "$cur_limit" ]]; then
    echo "no interaction limit is set"
  else
    echo "the limit is $cur_limit (set on the ${cur_origin:-?}), ending $cur_expires, $cur_days days from now"
  fi
}

elsewhere() { # a hint when the limit was set for a whole user or organization, else nothing
  [[ -z "$cur_origin" || "$cur_origin" == repository ]] \
    || echo " It was set at $cur_origin level, which a repository renew cannot change (GitHub answers 409): change it there, in that $cur_origin's moderation settings."
}

write_out() { # write_out STATUS MESSAGE
  [[ -z "$out" ]] || printf '%s\n%s\n' "$1" "$2" > "$out" || die "could not write $out"
}

case "$cmd" in
  check)
    if ! read_limit; then
      write_out error "$read_error"
      echo "::error::$read_error" >&2
      exit 2
    fi
    state="$(describe)"
    if [[ "$cur_limit" == "$limit" && "$cur_days" -gt "$days" ]]; then
      msg="ok: $repo: $state."
      write_out ok "$msg"
      echo "$msg"
      exit 0
    fi
    if [[ -z "$cur_limit" ]]; then
      why="$repo has no interaction limit: anyone can open issues and comment. Wanted: $limit."
    elif [[ "$cur_limit" != "$limit" ]]; then
      why="$repo: $state; wanted $limit.$(elsewhere)"
    else
      why="$repo: $state, $days or fewer: renew it.$(elsewhere)"
    fi
    write_out attention "$why"
    echo "::error::$why" >&2
    exit 1
    ;;

  renew)
    if [[ -n "$if_under" ]]; then
      read_limit || die "$read_error"
      if [[ "$cur_limit" == "$limit" && "$cur_days" -ge "$if_under" ]]; then
        echo "no renewal needed: $repo: $(describe) (renews under $if_under days)."
        exit 0
      fi
      echo "renewing: $repo: $(describe)."
    fi
    err="$(mktemp)"
    if ! gh api -X PUT "repos/$repo/interaction-limits" -f "limit=$limit" -f expiry=six_months > /dev/null 2> "$err"; then
      msg="could not set the interaction limit of $repo: $(tr '\n' ' ' < "$err" | cut -c1-300)"
      if grep -q "HTTP 409" "$err"; then
        msg+=" GitHub answers 409 when the limit is set for the whole user or organization: change it there, in that account's moderation settings."
      fi
      rm -f "$err"
      die "$msg"
    fi
    rm -f "$err"
    # Read back: the answer to the PUT is not proof, the stored limit is.
    read_limit || die "set the limit, but could not read it back: $read_error"
    if [[ "$cur_limit" != "$limit" || "$cur_days" -lt "$FRESH_DAYS" ]]; then
      die "set $limit for six months, but read back: $(describe)"
    fi
    echo "renewed: $repo: $(describe)."
    exit 0
    ;;

  report)
    [[ -n "$file" ]] || usage
    status="" message=""
    if [[ -r "$file" ]]; then
      status="$(head -n1 "$file")"
      message="$(tail -n +2 "$file")"
    fi
    case "$status" in
      ok) failed=false ;;
      attention | error) failed=true ;;
      *) failed=true; status=error; message="The check left no result (${file}): it did not run to the end." ;;
    esac
    body="$(mktemp)"
    {
      echo "**$message**"
      echo
      if [[ "$failed" == true ]]; then
        echo "GitHub ends an interaction limit after six months at most. To set it again for six months, either:"
        echo
        echo "- run \`FORK_GH_LOCAL=fork-interaction-limit.sh bash .github/scripts/fork-interaction-limit.sh renew --repo $repo --limit $limit\` where \`gh\` is logged in as an administrator of $repo, or"
        echo "- Settings, Moderation options, Interaction limits on GitHub."
        echo
        echo "A check that could not read the limit needs a token with the repository's Administration read permission (see the workflow file). A limit set for a whole user or organization cannot be renewed per repository (GitHub answers 409): change it where it was set."
      else
        echo "The limit is in place again."
      fi
      [[ -z "${RUN_URL:-}" ]] || { echo; echo "Run: $RUN_URL"; }
    } > "$body"
    note=""
    if ! has_issues="$(gh api "repos/$repo" --jq .has_issues 2> /dev/null)"; then
      note="Could not read whether $repo has Issues, so no issue was opened or closed."
      failed=true
    elif [[ "$has_issues" != true ]]; then
      note="Issues are disabled on $repo, so no issue was opened or closed; the run's status is the report."
    # The list itself, not search (its index can lag), and only issues GitHub Actions opened: the
    # issue appears exactly when strangers may post, and one of theirs with the same title must
    # never be commented on or closed.
    elif ! issue="$(gh issue list --repo "$repo" --state open --author "$BOT_AUTHOR" --limit 100 \
      --json number,title,author \
      --jq ".[] | select(.title == \"$TITLE\" and .author.login == \"$BOT_LOGIN\") | .number" | head -n1)"; then
      note="Could not list the issues of $repo, so no issue was opened or closed."
      failed=true
    elif [[ "$failed" == true && -n "$issue" ]]; then
      gh issue comment "$issue" --repo "$repo" --body-file "$body" > /dev/null \
        && note="Commented on issue #$issue." || note="Could not comment on issue #$issue."
    elif [[ "$failed" == true ]]; then
      if url="$(gh issue create --repo "$repo" --title "$TITLE" --body-file "$body")"; then
        note="Opened $url."
      else
        note="Could not open an issue."
      fi
    elif [[ -n "$issue" ]]; then
      gh issue close "$issue" --repo "$repo" --comment "$(cat "$body")" > /dev/null \
        && note="Closed issue #$issue." || { note="Could not close issue #$issue."; failed=true; }
    fi
    {
      cat "$body"
      [[ -z "$note" ]] || printf '\n%s\n' "$note"
    } >> "${GITHUB_STEP_SUMMARY:-/dev/stdout}"
    rm -f "$body"
    if [[ "$failed" == true ]]; then
      # An ok check whose issue could not be handled fails on the note, not on the ok message.
      [[ "$status" == ok ]] && echo "::error::$note" >&2 || echo "::error::$message" >&2
      exit 1
    fi
    exit 0
    ;;

  *) usage ;;
esac
