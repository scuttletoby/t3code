#!/usr/bin/env bash
# Tests for fork-release.sh against local git repositories and a fake `gh`; no network.
#   bash .github/scripts/fork-release.test.sh            (FORK_RELEASE_SH picks another script)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$(realpath "${FORK_RELEASE_SH:-$here/fork-release.sh}")"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT

# Upstream "up/t3code" and this fork "me/t3code" live under $root/gh; git reaches them through the
# github.com URLs the workflow uses.
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$root/gitconfig"
git config --global user.name test
git config --global user.email test@example.invalid
git config --global init.defaultBranch main
git config --global advice.detachedHead false
git config --global url."$root/gh/".insteadOf https://github.com/
export REPOSITORY=me/t3code GITHUB_REPOSITORY=me/t3code
unset GITHUB_OUTPUT GITHUB_STEP_SUMMARY UPSTREAM UPSTREAM_URL ORIGIN

# Fake gh: `gh api [-X M] PATH [--paginate] [--jq EXPR] [-f/-F k=v]...` served from $root/api.
# fork_test_gh (fork-test-gh.sh) installs it first on PATH, with no token, a .invalid GH_HOST and
# FORK_TEST_MODE, so the script refuses to run if any other gh would answer.
. "$here/fork-test-gh.sh"
mkdir -p "$root/bin" "$root/api"
fork_test_gh "$root/bin" <<'GH'
set -euo pipefail
api="${FAKE_API:?the gh stub needs FAKE_API}"
echo "gh $*" >> "$api/calls"
[[ "$1" == api ]] || { echo "fake gh: only api" >&2; exit 2; }
shift
method=GET path="" jq="" fields=()
while (( $# )); do
  case "$1" in
    -X) method="$2"; shift 2 ;;
    --jq) jq="$2"; shift 2 ;;
    --paginate) shift ;;
    -f|-F) fields+=("$2"); shift 2 ;;
    *) path="$1"; shift ;;
  esac
done
case "$method $path" in
  "GET repos/me/t3code") body='{"parent":{"full_name":"up/t3code"}}' ;;
  "GET repos/up/t3code/releases/latest") body="{\"tag_name\":\"$(cat "$api/upstream_latest")\"}" ;;
  "GET repos/me/t3code/releases")
    [[ ! -e "$api/fail_list" ]] || { echo "HTTP 502: Bad Gateway" >&2; exit 1; }
    body="$(cat "$api/releases.json")" ;;
  "DELETE repos/me/t3code/releases/"*)
    id="${path##*/}"
    jq --argjson id "$id" 'map(select(.id != $id))' "$api/releases.json" > "$api/r.tmp"
    mv "$api/r.tmp" "$api/releases.json"
    echo "deleted $id" >> "$api/writes"; exit 0 ;;
  "PATCH repos/me/t3code/releases/"*)
    echo "patched ${path##*/} ${fields[*]}" >> "$api/writes"; exit 0 ;;
  *) echo "fake gh: no route for $method $path" >&2; exit 1 ;;
esac
if [[ -n "$jq" ]]; then jq -r "$jq" <<< "$body"; else echo "$body"; fi
GH
export FAKE_API="$root/api"

failures=0
pass() { echo "ok   $1"; }
fail() {
  echo "FAIL $1"
  sed 's/^/     | /' "$root/out" 2> /dev/null || true
  failures=$((failures + 1))
}
run() { (cd "$1" && shift && "$script" "$@") > "$root/out" 2>&1; }

reset_api() {
  rm -f "$root/api"/*
  echo '[]' > "$root/api/releases.json"
  echo "${1:-v0.1.9}" > "$root/api/upstream_latest"
}

# Upstream history: u1 (v0.1.8) - u2 (v0.1.9) - u3 (unreleased, upstream main).
new_upstream() {
  rm -rf "$root/gh" "$root/upwork"
  mkdir -p "$root/gh/up" "$root/gh/me"
  git init -q --bare "$root/gh/up/t3code.git"
  git init -q --bare "$root/gh/me/t3code.git"
  git init -q "$root/upwork"
  (
    cd "$root/upwork"
    git commit -q --allow-empty -m u1 && git tag v0.1.8
    git commit -q --allow-empty -m u2 && git tag -a -m v0.1.9 v0.1.9
    git commit -q --allow-empty -m u3
    git push -q https://github.com/up/t3code.git main --tags
  )
}

# The fork's checkout: $1 (an upstream rev) plus one fork commit, pushed as the fork's main.
new_fork() {
  rm -rf "$root/work"
  git clone -q https://github.com/up/t3code.git "$root/work"
  (
    cd "$root/work"
    git remote set-url origin https://github.com/me/t3code.git
    git checkout -q -B main "$1"
    [[ "${2:-}" == bare ]] || git commit -q --allow-empty -m "fork: own change"
    git push -q --force origin main
  )
}

fork_tag() { (cd "$root/work" && git tag "$1" && git push -q origin "refs/tags/$1"); }

expect_resolved() {
  local name="$1" want="$2"
  if run "$root/work" resolve && grep -qx "version=$want" "$root/out"; then pass "$name"; else fail "$name"; fi
}
expect_refused() {
  local name="$1" pattern="$2"
  shift 2
  if ! run "$@" && grep -q "::error::.*$pattern" "$root/out"; then pass "$name"; else fail "$name"; fi
}

# --- resolve -----------------------------------------------------------------------------------
new_upstream
reset_api
new_fork v0.1.9
expect_resolved "resolve: v0.1.9 plus a fork commit is 0.1.9-fork.1" 0.1.9-fork.1
fork_tag v0.1.9-fork.1
fork_tag v0.1.9-fork.x
expect_resolved "resolve: the next build counts the fork's numbered tags" 0.1.9-fork.2

new_fork main bare
expect_refused "resolve: refuses unreleased upstream commits (upstream main)" "unreleased" "$root/work" resolve
new_fork main
expect_refused "resolve: refuses unreleased upstream commits under fork commits" "unreleased" "$root/work" resolve

(cd "$root/upwork" && git tag v1.0.0 v0.1.8 && git push -q https://github.com/up/t3code.git v1.0.0)
new_fork v0.1.9
expect_refused "resolve: refuses a stray higher tag on an old commit" "not the newest" "$root/work" resolve

new_upstream
reset_api v0.1.9
(cd "$root/upwork" && git tag v0.1.10 main && git push -q https://github.com/up/t3code.git v0.1.10)
new_fork v0.1.10
expect_refused "resolve: refuses a base above upstream's latest release" "above up/t3code's latest" "$root/work" resolve
echo v0.1.10 > "$root/api/upstream_latest"
expect_resolved "resolve: accepts it once upstream marks it latest" 0.1.10-fork.1

reset_api v0.1.10
echo '[{"id":5,"draft":false,"prerelease":false,"tag_name":"v0.1.10-fork.3"}]' > "$root/api/releases.json"
new_fork v0.1.9
expect_refused "resolve: refuses a build below a published release (older ref after a newer one)" \
  "v0.1.10-fork.3 is published and not below 0.1.9-fork.1" "$root/work" resolve
echo '[{"id":5,"draft":true,"prerelease":false,"tag_name":"v0.1.10-fork.3"}]' > "$root/api/releases.json"
expect_resolved "resolve: a higher draft does not block (updaters skip drafts)" 0.1.9-fork.1

# --- remove-drafts -----------------------------------------------------------------------------
new_upstream
reset_api
new_fork v0.1.9
echo '[{"id":7,"draft":true,"prerelease":false,"tag_name":"v0.1.9-fork.1"},
       {"id":8,"draft":true,"prerelease":false,"tag_name":"v0.1.9-fork.9"}]' > "$root/api/releases.json"
if run "$root/work" remove-drafts v0.1.9-fork.1 && grep -qx "deleted 7" "$root/api/writes" \
  && ! grep -q "deleted 8" "$root/api/writes" && grep -q "Removed leftover draft 7" "$root/out"; then
  pass "remove-drafts: deletes only that tag's draft and says so"
else
  fail "remove-drafts: deletes only that tag's draft and says so"
fi

reset_api
touch "$root/api/fail_list"
expect_refused "remove-drafts: a failed release listing fails the step" "Could not list the releases" "$root/work" remove-drafts v0.1.9-fork.1
[[ ! -e "$root/api/writes" ]] || fail "remove-drafts: nothing written after a failed listing"

reset_api
fork_tag v0.1.9-fork.1
expect_refused "remove-drafts: refuses a version whose tag exists (re-run failed jobs)" "already exists" \
  "$root/work" remove-drafts v0.1.9-fork.1

reset_api
echo '[{"id":5,"draft":false,"prerelease":false,"tag_name":"v0.1.9-fork.2"}]' > "$root/api/releases.json"
expect_refused "remove-drafts: refuses a version with a published release" "published release" \
  "$root/work" remove-drafts v0.1.9-fork.2

# --- publish -----------------------------------------------------------------------------------
reset_api
echo '[{"id":5,"draft":false,"prerelease":false,"tag_name":"v0.1.9-fork.1"},
       {"id":9,"draft":true,"prerelease":false,"tag_name":"v0.1.9-fork.2"}]' > "$root/api/releases.json"
if run "$root/work" publish 9 v0.1.9-fork.2 0.1.9-fork.2 && grep -qx "patched 9 draft=false make_latest=true" "$root/api/writes" \
  && grep -qx "published=true" "$root/out"; then
  pass "publish: a version above every release is marked latest"
else
  fail "publish: a version above every release is marked latest"
fi

reset_api
echo '[{"id":5,"draft":false,"prerelease":false,"tag_name":"v0.1.10-fork.1"},
       {"id":9,"draft":true,"prerelease":false,"tag_name":"v0.1.9-fork.2"}]' > "$root/api/releases.json"
expect_refused "publish: refuses a version below a published release (server takes the newest created)" \
  "v0.1.10-fork.1 is published and not below 0.1.9-fork.2" "$root/work" publish 9 v0.1.9-fork.2 0.1.9-fork.2
[[ ! -e "$root/api/writes" ]] || fail "publish: nothing written for an older version"

reset_api
echo '[{"id":5,"draft":false,"prerelease":true,"tag_name":"v0.1.10-fork.1"},
       {"id":9,"draft":true,"prerelease":false,"tag_name":"v0.1.9-fork.2"}]' > "$root/api/releases.json"
expect_refused "publish: a higher prerelease also blocks (the server ignores the prerelease flag)" \
  "not below" "$root/work" publish 9 v0.1.9-fork.2 0.1.9-fork.2

reset_api
echo '[{"id":4,"draft":false,"prerelease":false,"tag_name":"v0.2.0-nightly.20261101.3"},
       {"id":6,"draft":false,"prerelease":false,"tag_name":"notes"},
       {"id":9,"draft":true,"prerelease":false,"tag_name":"v0.1.9-fork.2"}]' > "$root/api/releases.json"
if run "$root/work" publish 9 v0.1.9-fork.2 0.1.9-fork.2 && grep -qx "patched 9 draft=false make_latest=true" "$root/api/writes"; then
  pass "publish: nightly and non-version tags are other channels and do not block"
else
  fail "publish: nightly and non-version tags are other channels and do not block"
fi

reset_api
echo '[{"id":4,"draft":false,"prerelease":false,"tag_name":"v0.1.8-rc.1"}]' > "$root/api/releases.json"
expect_refused "publish: a stable-channel tag it cannot order fails loudly" "cannot be ordered" \
  "$root/work" publish 9 v0.1.9-fork.2 0.1.9-fork.2

reset_api
echo '[{"id":9,"draft":true,"prerelease":false,"tag_name":"v0.1.9-fork.1"}]' > "$root/api/releases.json"
expect_refused "publish: re-checks the tag right before un-drafting" "already exists" "$root/work" publish 9 v0.1.9-fork.1 0.1.9-fork.1
[[ ! -e "$root/api/writes" ]] || fail "publish: nothing written when the tag exists"

reset_api
touch "$root/api/fail_list"
expect_refused "publish: a failed release listing fails the step" "Could not list the releases" "$root/work" publish 9 v0.1.9-fork.2 0.1.9-fork.2
[[ ! -e "$root/api/writes" ]] || fail "publish: nothing written after a failed listing"

# --- check-archives ----------------------------------------------------------------------------
# Layout of scripts/build-cli-archive.ts: <stem>/t3 (t3.exe in the Windows zip) plus <stem>/client/.
# The web client names this fork on every Actions build (upstream's apps/web/vite.config.ts), so it
# must not count; only the program does.
stage() { # stage DIR STEM PROGRAM PROGRAM_REPO CLIENT_REPO
  mkdir -p "$1/$2/client/assets"
  printf 'x\0const repo = "%s";\0' "$4" > "$1/$2/$3"
  printf 'const u = "%s";' "$5" > "$1/$2/client/assets/index.js"
}
pack() { # pack DIR STEM OUT
  case "$3" in
    *.tar.gz) tar -czf "$3" -C "$1" "$2" ;;
    *.zip) (cd "$1" && python3 -I -m zipfile -c "$3" "$2") ;;
  esac
}
lin=t3-0.1.9-fork.1-linux-x64 win=t3-0.1.9-fork.1-win-x64
mkdir -p "$root/assets-good" "$root/assets-bad" "$root/assets-repro" "$root/assets-noprog" "$root/assets-none"
stage "$root/arch/good" "$lin" t3 me/t3code me/t3code
stage "$root/arch/good" "$win" t3.exe me/t3code me/t3code
pack "$root/arch/good" "$lin" "$root/assets-good/$lin.tar.gz"
pack "$root/arch/good" "$win" "$root/assets-good/$win.zip"
cp "$root/assets-good/$lin.tar.gz" "$root/assets-bad/"
stage "$root/arch/bad" "$win" t3.exe up/t3code up/t3code
pack "$root/arch/bad" "$win" "$root/assets-bad/$win.zip"
# The review's repro: t3 names upstream, the web client names the fork.
stage "$root/arch/repro" "$lin" t3 up/t3code me/t3code
pack "$root/arch/repro" "$lin" "$root/assets-repro/$lin.tar.gz"
# No program at all, but the client names the fork.
mkdir -p "$root/arch/noprog/$lin/client" && printf '"me/t3code"' > "$root/arch/noprog/$lin/client/x.js"
printf '"me/t3code"' > "$root/arch/noprog/$lin/t3.sh"
pack "$root/arch/noprog" "$lin" "$root/assets-noprog/$lin.tar.gz"
if run "$root" check-archives "$root/assets-good"; then pass "check-archives: archives whose program names this fork pass"; else fail "check-archives: archives whose program names this fork pass"; fi
expect_refused "check-archives: an archive naming upstream fails" "$win/t3.exe in $win.zip does not name me/t3code" "$root" check-archives "$root/assets-bad"
expect_refused "check-archives: t3 names upstream, client/ names the fork -> fails" "$lin/t3 in $lin.tar.gz does not name me/t3code" "$root" check-archives "$root/assets-repro"
expect_refused "check-archives: a missing program fails" "has no $lin/t3" "$root" check-archives "$root/assets-noprog"
expect_refused "check-archives: no archive at all fails" "No CLI archive" "$root" check-archives "$root/assets-none"

# --- check-bundle (apps/server/dist with the web client in dist/client) ------------------------
bundle() { # bundle NAME SERVER_REPO CLIENT_REPO
  mkdir -p "$root/dist-$1/client/assets"
  printf 'const repo = "%s";' "$2" > "$root/dist-$1/bin.mjs"
  printf 'const u = "%s";' "$3" > "$root/dist-$1/client/assets/index.js"
}
bundle good me/t3code me/t3code
bundle repro up/t3code me/t3code
mkdir -p "$root/dist-clientonly/client" && printf '"me/t3code"' > "$root/dist-clientonly/client/x.js"
if run "$root" check-bundle "$root/dist-good"; then pass "check-bundle: a server bundle naming this fork passes"; else fail "check-bundle: a server bundle naming this fork passes"; fi
expect_refused "check-bundle: server names upstream, client/ names the fork -> fails" "outside client/) does not name me/t3code" "$root" check-bundle "$root/dist-repro"
expect_refused "check-bundle: only client/ files -> fails" "no files outside client/" "$root" check-bundle "$root/dist-clientonly"
expect_refused "check-bundle: a missing bundle fails" "No server bundle" "$root" check-bundle "$root/dist-missing"

# --- version order -----------------------------------------------------------------------------
check_gt() {
  if (cd "$root" && "$script" version-gt "$1" "$2") 2> /dev/null; then got=gt; else got=not; fi
  if [[ "$got" == "$3" ]]; then pass "version-gt $1 $2 is $3"; else fail "version-gt $1 $2 is $3"; fi
}
check_gt 0.1.9-fork.10 0.1.9-fork.9 gt
check_gt v0.1.10-fork.1 v0.1.9-fork.12 gt
check_gt 0.1.9-fork.2 0.1.9-fork.2 not
check_gt 0.1.9-fork.2 0.2.0-fork.1 not
check_gt v1.0.0 v0.1.10 gt

# --- gh: never GitHub outside a run (fork-gh-guard.sh) ------------------------------------------
# A gh that is not the stub answers like GitHub and records every call; the script must refuse
# before it calls any gh: neither this one nor the stub.
mkdir -p "$root/decoy"
printf '#!/usr/bin/env bash\necho "gh $*" >> "%s"\necho "[]"\n' "$root/decoy.calls" > "$root/decoy/gh"
chmod +x "$root/decoy/gh"
new_upstream
new_fork v0.1.9
reset_api v0.1.9
refuses() { # refuses NAME ENV...: resolve and remove-drafts, run with ENV, fail, say why, call no gh
  local name="$1" sub ok=true
  shift
  for sub in resolve "remove-drafts v0.1.9-fork.1"; do
    rm -f "$root/decoy.calls" "$root/api/calls"
    # shellcheck disable=SC2086
    if (cd "$root/work" && env "$@" "$script" $sub) > "$root/out" 2>&1; then ok=false; fi
    [[ ! -e "$root/decoy.calls" && ! -e "$root/api/calls" ]] || ok=false
    grep -q "Refusing to run fork-release.sh" "$root/out" || ok=false
  done
  if $ok; then pass "$name"; else fail "$name"; fi
}
refuses "gh: a test run without the stub first on PATH fails closed and never calls gh" PATH="$root/decoy:$PATH"
refuses "gh: run by hand (no test mode, not in a GitHub Actions run) refuses" FORK_TEST_MODE= PATH="$root/decoy:$PATH"
refuses "gh: test mode with a GitHub token set refuses, even with the stub" GH_TOKEN=x
refuses "gh: test mode with GH_HOST github.com refuses" GH_HOST=github.com

if (( failures )); then
  echo "$failures failed"
  exit 1
fi
echo "all passed"
