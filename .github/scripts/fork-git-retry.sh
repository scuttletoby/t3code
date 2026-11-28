# shellcheck shell=bash
# git_retry ARGS...: `git ARGS`, tried again only after a connection error, so a moment's network trouble between a
# runner and GitHub (a run once waited 132 s for a connection, then failed its Checkout) does not fail the job.
#
# A connection error: git's words say the connection failed (refused, reset, timed out, a name that did not resolve, TLS,
# an HTTP 429 or 5xx, a transfer cut off), or the try reached its time limit. Any other failure (a ref or commit the
# remote does not have, a login, a missing repository, a bad argument) fails at once, with git's exit status: trying it
# again would only say the same later. git's own words reach stderr as they are; its stdout is passed through untouched.
#
# Bounds: 4 tries, waiting 5, 10 and 20 s between them (GIT_RETRY_WAITS, seconds separated by spaces: one try more than
# waits), each try at most GIT_RETRY_TRY seconds (90) and all of them, waits included, at most GIT_RETRY_TOTAL seconds
# (150): no try starts once the total is spent. A caller with a bigger fetch sets bigger bounds for its call
# (`GIT_RETRY_TRY=300 GIT_RETRY_TOTAL=360 git_retry fetch ...`). GIT_RETRY_STDIN: a file each try reads as its stdin
# (for `fetch --stdin`; a stdin redirected to the call itself would be used up by the first try).
#
# One source: this file. A step that runs before the checkout (the checkout itself) or must not run the checkout's code
# (the secret scan) holds a copy of the function instead of sourcing it; fork-git-retry.test.py holds every copy to this
# text, checks that every git fetch in the fork's workflows and scripts goes through it, and runs it against a local
# remote that refuses connections first.
git_retry() {
  local net real waits err code=0 limit try=1 end=$((SECONDS + ${GIT_RETRY_TOTAL:-150}))
  net='Failed to connect|Could(n.t| not) resolve|Connection (refused|reset|timed out)|Operation timed out|Network is unreachable'
  net+='|Temporary failure in name resolution|Recv failure|Send failure|SSL_ERROR_SYSCALL|SSL_(connect|read)|gnutls_handshake'
  net+='|TLS connect error|returned error: (429|5[0-9][0-9])|RPC failed|early EOF|unexpected disconnect|hung up unexpectedly'
  net+='|was not closed cleanly|transfer closed with|Empty reply from server'
  real='not our ref|find remote ref|no such remote ref|unadvertised object|Authentication failed|could not read (Username|Password)'
  real+='|Repository not found|returned error: 4([01][0-9]|2[0-8]|[3-9][0-9])|not appear to be a git repository|invalid refspec'
  read -r -a waits <<< "${GIT_RETRY_WAITS:-5 10 20}"
  while :; do
    limit=$((end - SECONDS))
    ((limit < ${GIT_RETRY_TRY:-90})) || limit=${GIT_RETRY_TRY:-90}
    ((limit > 0)) || limit=1
    code=0
    if [[ -n "${GIT_RETRY_STDIN:-}" ]]; then
      { err="$(LC_ALL=C timeout -k 5 "$limit" git "$@" 2>&1 >&3 3>&- < "$GIT_RETRY_STDIN")" || code=$?; } 3>&1
    else
      { err="$(LC_ALL=C timeout -k 5 "$limit" git "$@" 2>&1 >&3 3>&-)" || code=$?; } 3>&1
    fi
    [[ -z "$err" ]] || printf '%s\n' "$err" >&2
    ((code)) || return 0
    if ((code != 124 && code != 137)) && { ! grep -qE "$net" <<< "$err" || grep -qE "$real" <<< "$err"; }; then
      return "$code"
    fi
    if ((try > ${#waits[@]})) || ((SECONDS + ${waits[try - 1]} + 5 > end)); then
      echo "::error::git $1: a connection error on try $try, the last one (git_retry: no tries or time left)" >&2
      return "$code"
    fi
    echo "::warning::git $1: a connection error on try $try (exit $code); trying again in ${waits[try - 1]} s" >&2
    sleep "${waits[try - 1]}"
    try=$((try + 1))
  done
}
