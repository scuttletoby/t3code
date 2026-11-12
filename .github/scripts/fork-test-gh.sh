# shellcheck shell=bash
# Sourced first by every fork shell test (fork-contracts.test.py requires it of each *.test.sh).
# `fork_test_gh DIR < STUB` cuts the test off from GitHub before any script
# runs: no GitHub token, GH_HOST a .invalid host and an empty gh config (so even a real gh has
# nothing to reach and no login), and the stub read from stdin as DIR/gh, first on PATH, with the
# marker line fork-gh-guard.sh looks for. FORK_TEST_MODE then makes the scripts refuse whenever
# that stub is not the gh they would call. The test stops at once if gh still resolves elsewhere.

FORK_GH_STUB_MARKER="# fork-gh-stub: a test stand-in for gh that never reaches GitHub"

fork_test_gh() {
  local dir="$1"
  unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN GITHUB_ACTIONS
  mkdir -p "$dir" "$dir/gh-config"
  export GH_HOST=gh-stub.invalid GH_CONFIG_DIR="$dir/gh-config" GH_PROMPT_DISABLED=1 \
    GH_NO_UPDATE_NOTIFIER=1 FORK_TEST_MODE=1
  {
    echo '#!/usr/bin/env bash'
    echo "$FORK_GH_STUB_MARKER"
    cat
  } > "$dir/gh"
  chmod +x "$dir/gh"
  export PATH="$dir:$PATH" FORK_GH_STUB="$dir/gh"
  if [[ "$(type -P gh)" != "$dir/gh" ]]; then
    echo "fork_test_gh: gh resolves to $(type -P gh), not the stub $dir/gh; stopping before any script runs" >&2
    exit 1
  fi
}
