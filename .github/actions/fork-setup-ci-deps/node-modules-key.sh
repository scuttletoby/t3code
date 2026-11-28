#!/usr/bin/env bash
# Fork only: the key and paths of fork-setup-ci-deps's node_modules cache (action.yml says why).
# Writes `key` and `paths` to $GITHUB_OUTPUT, or nothing when node_modules cannot be cached here
# (then the action installs from the pnpm store, as upstream's does). Run from the workspace root.
# Env: NAMESPACE (full or filtered-<cksum of the install args>), VP_VERSION, NODE_VERSION, PNPM,
# STORE_PATH, RUNNER_OS, RUNNER_ARCH.
set -euo pipefail
out="${GITHUB_OUTPUT:?GITHUB_OUTPUT is not set}"

skip() {
  echo "node_modules is not cached: $1"
  exit 0
}

[[ -f pnpm-lock.yaml ]] || skip "no pnpm-lock.yaml"

# Every project pnpm installs into: the lockfile's importers (a lockfile can hold more than one
# document, each with its own `importers:`).
mapfile -t importers < <(
  sed -n '/^importers:$/,/^[^ ]/s/^  \([^ #].*\):$/\1/p' pnpm-lock.yaml |
    sed -E "s/^'(.*)'\$/\\1/; s/^\"(.*)\"\$/\\1/" | LC_ALL=C sort -u
)
((${#importers[@]})) || skip "pnpm-lock.yaml names no importers"

paths=()
inputs=(pnpm-lock.yaml pnpm-workspace.yaml package.json .npmrc .pnpmfile.cjs .pnpmfile.mjs)
for dir in "${importers[@]}"; do
  case "$dir" in
    "" | /* | .. | ../* | */.. | */../* | *$'\n'*) skip "unexpected importer path in pnpm-lock.yaml: $dir" ;;
    .) paths+=(node_modules) ;;
    *)
      paths+=("$dir/node_modules")
      inputs+=("$dir/package.json")
      ;;
  esac
done

# Patch files (pnpm-workspace.yaml `patchedDependencies`) are applied at install time.
if [[ -f pnpm-workspace.yaml ]]; then
  while IFS= read -r patch; do
    [[ -n "$patch" ]] && inputs+=("$patch")
  done < <(
    sed -n '/^patchedDependencies:$/,/^[^ #]/s/^  [^ #].*: *\([^ ]*\) *$/\1/p' pnpm-workspace.yaml |
      sed -E "s/^'(.*)'\$/\\1/; s/^\"(.*)\"\$/\\1/"
  )
fi

# .bin shims and .modules.yaml hold absolute paths (the workspace and the store), and builds of
# native packages depend on the OS release, so each of these is part of the key.
os_release="$(. /etc/os-release 2>/dev/null && echo "${ID:-}-${VERSION_ID:-}" || uname -sr)"
hash="$(
  {
    printf 'fork-node-modules v1\nos %s\nworkspace %s\nstore %s\nvp %s\nnode %s\npnpm %s\nnamespace %s\n' \
      "$os_release" "$PWD" "${STORE_PATH:-}" "${VP_VERSION:-}" "${NODE_VERSION:-}" "${PNPM:-}" "${NAMESPACE:-}"
    printf '%s\n' "${inputs[@]}" | LC_ALL=C sort -u | while IFS= read -r file; do
      if [[ -f "$file" ]]; then
        printf '%s %s\n' "$(sha256sum <"$file" | cut -d' ' -f1)" "$file"
      else
        printf 'absent %s\n' "$file"
      fi
    done
  } | sha256sum | cut -d' ' -f1
)"

{
  echo "key=fork-node-modules-v1-${NAMESPACE:-full}-${RUNNER_OS:-}-${RUNNER_ARCH:-}-node${NODE_VERSION:-}-${PNPM:-}-${hash}"
  echo "paths<<FORK_NODE_MODULES_PATHS"
  printf '%s\n' "${paths[@]}"
  echo "FORK_NODE_MODULES_PATHS"
} >>"$out"
