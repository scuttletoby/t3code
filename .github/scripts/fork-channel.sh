# Sourced by fork-follow.sh and fork-release.sh: which upstream tags the fork follows, how its own
# releases are named, and the version order both use. Tests: fork-follow.test.sh, fork-release.test.sh.
#
# FORK_FOLLOW_CHANNEL (the repository variable of that name; default nightly):
#   nightly  upstream tags vX.Y.Z-nightly.YYYYMMDD.N; the fork's release of one is <tag>.fork.<n>
#   stable   upstream tags vX.Y.Z;                     the fork's release of one is <tag>-fork.<n>
# Both fork forms end in something other than -nightly.YYYYMMDD.N, so the app's updaters read every
# fork release as their stable track (packages/shared/src/cliRelease.ts cliReleaseChannelOf):
# the fork has one track, and its builds keep the stable update feed and branding.

fork_re='^v[0-9]+\.[0-9]+\.[0-9]+(-fork|-nightly\.[0-9]{8}\.[0-9]+\.fork)\.[0-9]+$'

fork_channel_init() { # sets channel, channel_re and channel_form; fails on an unknown channel
  channel="${FORK_FOLLOW_CHANNEL:-nightly}"
  case "$channel" in
    nightly) channel_re='^v[0-9]+\.[0-9]+\.[0-9]+-nightly\.[0-9]{8}\.[0-9]+$' channel_form=vX.Y.Z-nightly.YYYYMMDD.N ;;
    stable) channel_re='^v[0-9]+\.[0-9]+\.[0-9]+$' channel_form=vX.Y.Z ;;
    *) return 1 ;;
  esac
}

# The separator before fork.<n> for an upstream tag: "." after a nightly tag (which already has a
# pre-release part, so the fork number extends it), "-" after a stable one.
fork_sep() { if [[ "$1" == *-nightly.* ]]; then printf .; else printf -- -; fi; }

# version_key VERSION: "X Y Z KIND DATE RUN FORK", numbers only, for vX.Y.Z, vX.Y.Z-fork.N,
# vX.Y.Z-nightly.D.R and vX.Y.Z-nightly.D.R.fork.N (leading v optional); fails on any other form.
# KIND: 1 stable fork, 2 nightly, 3 plain. Within one X.Y.Z this is semver's order, which the app's
# updaters use (packages/shared/src/semver.ts compareSemverVersions): every pre-release sorts below
# X.Y.Z itself, and a nightly sorts above a stable fork release ("nightly" > "fork"). Numbers
# compare as numbers: run 1000 is above run 999 (a plain sort of the names gets that wrong).
# A version is not an age: upstream names a nightly after the release it leads up to, so
# vX.Y.Z-nightly.D.N.fork.M sorts above X.Y.Z-fork.N although its code can be older
# (fork-release.sh refuse_older checks that pair by ancestry).
version_key() {
  local re='^v?([0-9]+)\.([0-9]+)\.([0-9]+)(-fork\.([0-9]+)|-nightly\.([0-9]{8})\.([0-9]+)(\.fork\.([0-9]+))?)?$'
  [[ "$1" =~ $re ]] || return 1
  local m=("${BASH_REMATCH[@]}")
  if [[ -n "${m[5]}" ]]; then
    echo "${m[1]} ${m[2]} ${m[3]} 1 0 0 ${m[5]}"
  elif [[ -n "${m[6]}" ]]; then
    echo "${m[1]} ${m[2]} ${m[3]} 2 ${m[6]} ${m[7]} ${m[9]:-0}"
  else
    echo "${m[1]} ${m[2]} ${m[3]} 3 0 0 0"
  fi
}

# newest_first: the versions on stdin (forms above only), newest first by version_key, never by
# name or by when a tag was made. A line of another form fails it.
newest_first() {
  local t key
  while IFS= read -r t; do
    [[ -n "$t" ]] || continue
    key="$(version_key "$t")" || return 1
    printf '%s %s\n' "$key" "$t"
  done | sort -k1,1nr -k2,2nr -k3,3nr -k4,4nr -k5,5nr -k6,6nr -k7,7nr | cut -d' ' -f8
}

# version_gt A B: A sorts above B by version_key. Fails (exit 2) on a form it cannot order.
version_gt() {
  local -a x y
  local i
  read -r -a x <<< "$(version_key "$1")" && read -r -a y <<< "$(version_key "$2")" || return 2
  ((${#x[@]} == 7 && ${#y[@]} == 7)) || return 2
  for i in 0 1 2 3 4 5 6; do
    if ((10#${x[i]} != 10#${y[i]})); then
      ((10#${x[i]} > 10#${y[i]}))
      return
    fi
  done
  return 1
}
