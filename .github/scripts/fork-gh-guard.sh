# shellcheck shell=bash
# Sourced by every fork script that calls gh (fork-workflows.sh, fork-follow-guard.sh,
# fork-follow-report.sh, fork-release.sh). Those scripts change the live repository (they disable
# workflows, cancel runs, publish releases), so they reach gh only:
#   - in a GitHub Actions run (GITHUB_ACTIONS=true) that is not a test, or
#   - in a test (FORK_TEST_MODE set, by fork-test-gh.sh) whose gh is the tests' stub: the gh on
#     PATH is the file FORK_GH_STUB names and carries the stub's marker line, no GitHub token is
#     set, and GH_HOST is a .invalid host.
# Anything else refuses: the script exits 1 when it sources this file, before any gh call, and
# every later call goes through the gh function below, which checks again (a script may change
# PATH). The marker must match FORK_GH_STUB_MARKER in fork-test-gh.sh. Tests: fork-follow.test.sh
# and fork-release.test.sh.

FORK_GH_STUB_MARKER="# fork-gh-stub: a test stand-in for gh that never reaches GitHub"

fork_gh_refusal() { # prints why gh may not be called here, or nothing
  local stub
  if [[ -z "${FORK_TEST_MODE:-}" ]]; then
    [[ "${GITHUB_ACTIONS:-}" == true ]] \
      || echo "this is neither a GitHub Actions run nor a test under the gh stub (FORK_TEST_MODE). These scripts change the live repository, so they run only in its workflows."
    return 0
  fi
  stub="$(type -P gh || true)"
  if [[ -z "${FORK_GH_STUB:-}" || -z "$stub" || "$(realpath "$stub")" != "$(realpath "$FORK_GH_STUB")" ]]; then
    echo "test mode, but the gh on PATH (${stub:-none}) is not the stub FORK_GH_STUB (${FORK_GH_STUB:-unset})."
  elif ! grep -qxF "$FORK_GH_STUB_MARKER" "$stub"; then
    echo "test mode, but $stub has no stub marker line."
  elif [[ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}${GH_ENTERPRISE_TOKEN:-}${GITHUB_ENTERPRISE_TOKEN:-}" ]]; then
    echo "test mode, but a GitHub token is set (GH_TOKEN, GITHUB_TOKEN or an enterprise token)."
  elif [[ "${GH_HOST:-}" != *.invalid ]]; then
    echo "test mode, but GH_HOST (${GH_HOST:-unset}) is not a .invalid host."
  fi
}

gh() {
  local why
  why="$(fork_gh_refusal)"
  if [[ -n "$why" ]]; then
    printf '::error::Refusing to call gh: %s\n' "$why" >&2
    return 97
  fi
  command gh "$@"
}

fork_gh_why="$(fork_gh_refusal)"
if [[ -n "$fork_gh_why" ]]; then
  printf '::error::Refusing to run %s: %s\n' "$(basename "$0")" "$fork_gh_why" >&2
  [[ -z "${REPORT:-}" ]] || printf 'Refusing to run %s: %s\n' "$(basename "$0")" "$fork_gh_why" > "$REPORT"
  exit 1
fi
unset fork_gh_why
