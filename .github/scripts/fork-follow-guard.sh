#!/usr/bin/env bash
# Guard job of fork-follow.yml (tests: fork-follow.test.sh). It runs before any job that gets
# FORK_PUSH_TOKEN and refuses unless the token's isolation holds as the repository is set up now:
#
# 1. No repository or organisation secret named FORK_PUSH_TOKEN. A workflow file on any pushed
#    branch can read such a secret, and in a job with an Environment GitHub falls back to it when
#    the Environment has none. The job runs without an Environment, so `secrets.FORK_PUSH_TOKEN`
#    there can only be a repository or organisation secret; the workflow passes only whether it is
#    empty (REPO_SECRET_PRESENT=true|false), never the value. GITHUB_TOKEN has no permission that
#    lists secrets, so this is the check GitHub allows.
# 2. The Environment FORK_PUSH_ENVIRONMENT exists, and only SERIES_BRANCH may deploy to it: its
#    deployment branches are "Selected branches and tags" with exactly one rule, the branch
#    SERIES_BRANCH, written without wildcards. "Protected branches only" is refused, because the
#    API cannot prove that no other branch is protected (or becomes protected later).
#
# `fork-follow-guard.sh --environment-only` makes check 2 alone. The job that moves the series
# branch runs it right before the push, because that can be an hour or more after the Guard job
# looked. Check 1 cannot be made there: inside the Environment, `secrets.FORK_PUSH_TOKEN` is the
# Environment's own secret.
#
# Environment: REPOSITORY (owner/name; GITHUB_REPOSITORY in a run), FORK_PUSH_ENVIRONMENT,
# SERIES_BRANCH (default main), REPO_SECRET_PRESENT, REPORT (receives the reason on refusal). Reads
# the Environment with `gh api` (GH_TOKEN: GITHUB_TOKEN with actions: read is enough).
set -euo pipefail

REPOSITORY="${REPOSITORY:-${GITHUB_REPOSITORY:?REPOSITORY or GITHUB_REPOSITORY}}"
: "${FORK_PUSH_ENVIRONMENT:?FORK_PUSH_ENVIRONMENT}"
SERIES_BRANCH="${SERIES_BRANCH:-main}"
REPORT="${REPORT:-fork-follow-report.md}"
env_name="$FORK_PUSH_ENVIRONMENT"
settings="Settings > Environments > $env_name > Deployment branches and tags"
err="$(mktemp)"
trap 'rm -f "$err"' EXIT

die() {
  printf '%s\n' "$@" > "$REPORT"
  printf '::error::%s\n' "$1" >&2
  [[ $# -lt 2 ]] || printf '%s\n' "${@:2}" >&2
  exit 1
}

case "${1:-}" in
  "") secret_check=true ;;
  --environment-only) secret_check=false ;;
  *) echo "usage: $0 [--environment-only]" >&2; exit 2 ;;
esac

[[ "$secret_check" == false ]] || case "${REPO_SECRET_PRESENT:-}" in
  false) ;;
  true)
    die "A repository or organisation secret named FORK_PUSH_TOKEN exists, so this run does not use the token." \
      "A workflow file on any pushed branch can read a repository or organisation secret, and a job in the Environment $env_name falls back to it when the Environment has none." \
      "Delete it under Settings > Secrets and variables > Actions > Repository secrets (or, for an organisation secret, remove $REPOSITORY from its repository access)." \
      "Keep the token only as an Environment secret of $env_name." ;;
  *)
    die "The run could not tell whether a repository secret FORK_PUSH_TOKEN exists (REPO_SECRET_PRESENT='${REPO_SECRET_PRESENT:-}'), so it does not use the token." \
      "This is a fault in fork-follow.yml: its guard job must pass \`secrets.FORK_PUSH_TOKEN != ''\`." ;;
esac

if ! environment="$(gh api "repos/$REPOSITORY/environments/$env_name" 2> "$err")"; then
  if grep -q "404" "$err"; then
    die "The Environment $env_name does not exist, so this run does not use the token." \
      "Create it under Settings > Environments > New environment, named $env_name." \
      "Under Deployment branches and tags pick \"Selected branches and tags\" and add one rule: branch $SERIES_BRANCH (nothing else)." \
      "Then add the secret FORK_PUSH_TOKEN there: a fine-grained token for $REPOSITORY only, with Contents and Workflows read and write."
  fi
  die "Could not read the Environment $env_name of $REPOSITORY, so this run does not use the token. gh said:" \
    '```' "$(tail -n 5 "$err")" '```'
fi

policy="$(jq -r 'if .deployment_branch_policy == null then "all"
  elif .deployment_branch_policy.protected_branches then "protected"
  elif .deployment_branch_policy.custom_branch_policies then "custom"
  else "unknown" end' <<< "$environment")"
case "$policy" in
  all)
    die "Any branch may deploy to the Environment $env_name, so a workflow file on any pushed branch could get FORK_PUSH_TOKEN; this run does not use it." \
      "Under $settings pick \"Selected branches and tags\" and add one rule: branch $SERIES_BRANCH (nothing else)." ;;
  protected)
    die "The Environment $env_name allows \"Protected branches only\", which the API cannot prove means only $SERIES_BRANCH; this run does not use FORK_PUSH_TOKEN." \
      "Under $settings pick \"Selected branches and tags\" instead and add one rule: branch $SERIES_BRANCH (nothing else)." ;;
  custom) ;;
  *)
    die "The deployment branch rule of the Environment $env_name is not one this guard knows, so this run does not use FORK_PUSH_TOKEN." \
      "Under $settings pick \"Selected branches and tags\" and add one rule: branch $SERIES_BRANCH (nothing else)." ;;
esac

rules="$(gh api "repos/$REPOSITORY/environments/$env_name/deployment-branch-policies?per_page=100" --paginate \
  --jq '.branch_policies[] | "\(.type // "branch") \(.name)"' 2> "$err")" \
  || die "Could not read the deployment branch rules of the Environment $env_name, so this run does not use FORK_PUSH_TOKEN. gh said:" \
    '```' "$(tail -n 5 "$err")" '```'
if [[ "$rules" != "branch $SERIES_BRANCH" ]] || [[ "$SERIES_BRANCH" == *[][*?]* ]]; then
  die "The Environment $env_name must let only the branch $SERIES_BRANCH deploy, so this run does not use FORK_PUSH_TOKEN. Its rules now:" \
    "$(if [[ -n "$rules" ]]; then sed 's/^\([a-z]*\) \(.*\)$/- \1 `\2`/' <<< "$rules"; else echo "- (none)"; fi)" "" \
    "Under $settings keep exactly one rule, branch $SERIES_BRANCH, and delete the others (a tag rule or a pattern lets other refs deploy too)."
fi

if [[ "$secret_check" == true ]]; then
  echo "FORK_PUSH_TOKEN is only an Environment secret, and only $SERIES_BRANCH may deploy to $env_name."
else
  echo "Only $SERIES_BRANCH may deploy to $env_name (the repository secret was checked by the Guard job)."
fi
