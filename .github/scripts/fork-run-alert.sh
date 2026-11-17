#!/usr/bin/env bash
# One issue for a scheduled job's result (tests: fork-run-alert.test.sh). A failed schedule run
# alerts no one (GitHub mails only the user who last edited the cron line), so a workflow's last
# job runs this with the result of the job it watches:
#
#   fork-run-alert.sh --title TITLE --result RESULT [--message TEXT] [--repo OWNER/NAME]
#
# RESULT is a job result as `needs.<job>.result` gives it. success: closes the open issue named
# TITLE, if any, and exits 0. Anything else (failure, cancelled, skipped, empty): opens that issue,
# or comments on it when it is open already (one issue, a comment per failed run), and exits 1, so
# the run stays red. It only ever touches an open issue GitHub Actions itself opened with exactly
# that title, never one someone else opened. RUN_URL (optional) goes into the issue. The issue
# body is TEXT, then the result and the run. OWNER/NAME: --repo, or else GITHUB_REPOSITORY.
# Needs gh (with a token that has issues: write) and jq. fork-interaction-limit.sh's `report` does
# the same for its own check.
set -uo pipefail
# Refuses outside a GitHub Actions run or a test under the gh stub (fork-gh-guard.sh).
. "$(dirname "${BASH_SOURCE[0]}")/fork-gh-guard.sh"
# Without -e a missing guard file is only bash's error, and the script would go on to call gh
# unguarded (a checkout that leaves it out, #239): its absence is a refusal of its own.
if ! declare -F fork_gh_refusal > /dev/null; then
  echo "::error::fork-run-alert: fork-gh-guard.sh is missing beside this script, so no gh call is made (check it out with it)" >&2
  exit 2
fi

# The issue author: the bot's user login, as the REST list's creator filter and user.login give it.
# A person cannot take a login with brackets.
BOT_LOGIN="github-actions[bot]"

die() { echo "fork-run-alert: $*" >&2; exit 2; }
title="" result="" message="" repo="${GITHUB_REPOSITORY:-}"
result_given=""
while (( $# )); do
  case "$1" in
    --title) title="${2-}"; shift ;;
    --result) result="${2-}"; result_given=1; shift ;;
    --message) message="${2-}"; shift ;;
    --repo) repo="${2-}"; shift ;;
    *) die "unknown argument: $1 (see the header of $0)" ;;
  esac
  shift || die "a value is missing after the last option"
done
[[ -n "$title" && -n "$result_given" ]] || die "--title and --result are required (see the header of $0)"
[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "no repository: pass --repo OWNER/NAME"

failed=true
[[ "$result" == success ]] && failed=false
body="$(mktemp)"
trap 'rm -f "$body"' EXIT
{
  if [[ "$failed" == true ]]; then
    echo "**${message:-The watched job did not succeed.}**"
    echo
    echo "Result: \`${result:-none}\`."
  else
    echo "The watched job succeeded again (result: \`success\`)."
  fi
  [[ -z "${RUN_URL:-}" ]] || { echo; echo "Run: $RUN_URL"; }
} > "$body"

note=""
issue=""
# The list itself, not search (its index can lag), and only issues GitHub Actions opened. Every
# page, filtered by title on each: a first page (`--limit 100`) of other open bot issues would
# hide the alert's own, and the next failure would open a second one (#239). The REST list holds
# pull requests too; they are left out.
if ! issue="$(gh api --paginate "repos/$repo/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100" \
  --jq ".[] | select(.pull_request == null and .title == $(jq -Rn --arg t "$title" '$t') and .user.login == \"$BOT_LOGIN\") | .number" | sed -n 1p)"; then
  note="Could not list the issues of $repo, so no issue was opened, updated or closed."
  failed=true
elif [[ "$failed" == true && -n "$issue" ]]; then
  gh issue comment "$issue" --repo "$repo" --body-file "$body" > /dev/null \
    && note="Commented on issue #$issue." || note="Could not comment on issue #$issue."
elif [[ "$failed" == true ]]; then
  if url="$(gh issue create --repo "$repo" --title "$title" --body-file "$body")"; then
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
if [[ "$failed" == true ]]; then
  if [[ "$result" == success ]]; then echo "::error::$note" >&2
  else echo "::error::${message:-The watched job did not succeed} (result: ${result:-none}). $note" >&2; fi
  exit 1
fi
echo "$title: the watched job succeeded. $note"
exit 0
