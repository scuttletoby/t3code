#!/usr/bin/env bash
# Fork only: starts and stops a fleet host's one-job runners, as root, for `fork-fleet.py dispatch` (a pool's `launch`
# command in .github/fork-fleet.json). Install a root-owned copy of main's file; runbook: .github/fork-fleet.md.
#
#   start NAME   stdin: the lines ACTIONS_RUNNER_INPUT_JITCONFIG=, FORK_FLEET_RUN= and FORK_FLEET_ATTEMPT=, nothing
#                else. Runs a fresh copy of the runner template for this one job, as RUNNER_USER, in a transient
#                systemd unit fork-fleet-NAME. Its environment (the JIT config, the run it is for and the job-started
#                hook) comes from a root-only file, so nothing the runner user can write decides it.
#   active       the NAME of every fork-fleet-* unit that is loaded (starting, running or stopping).
#   fits MB      exit 0 when MemAvailable minus RESERVE_MB leaves MB megabytes.
#   stop NAME    stops that unit (a job on it fails: its runner is gone).
#   prune        removes the copies and environment files whose unit is gone.
#
# Settings: /etc/fork-fleet/launch.conf (or $FORK_FLEET_LAUNCH_CONF), KEY=value lines, owned by this user and
# writable by it alone. RUNNER_USER and TEMPLATE (an unconfigured actions/runner, owned by this user) are required;
# WORK_ROOT, RUN_DIR, HOOK, RESERVE_MB, MAX_SECONDS, MEMINFO, SYSTEMD_RUN and SYSTEMCTL have defaults below.
set -euo pipefail
umask 077

die() {
  printf 'fork-fleet-launch: %s\n' "$*" >&2
  exit 1
}

# A path this user owns and no one else may write (not a symlink).
owned_safe() {
  local uid mode
  [[ -e $1 && ! -L $1 ]] || die "$1 is missing or a symlink"
  read -r uid mode < <(stat -c '%u %a' -- "$1")
  [[ $uid == "$EUID" ]] || die "$1 must belong to uid $EUID"
  (((8#$mode & 8#022) == 0)) || die "$1 must be writable by its owner only"
}

conf=${FORK_FLEET_LAUNCH_CONF:-/etc/fork-fleet/launch.conf}
owned_safe "$conf"
RUNNER_USER='' TEMPLATE='' WORK_ROOT=/var/lib/fork-fleet/runners RUN_DIR=/run/fork-fleet HOOK=/opt/fork-fleet/hook.sh
RESERVE_MB=2048 MAX_SECONDS=3600 MEMINFO=/proc/meminfo SYSTEMD_RUN=systemd-run SYSTEMCTL=systemctl
setting='^(RUNNER_USER|TEMPLATE|WORK_ROOT|RUN_DIR|HOOK|RESERVE_MB|MAX_SECONDS|MEMINFO|SYSTEMD_RUN|SYSTEMCTL)=([^[:space:]]*)$'
while IFS= read -r line || [[ -n $line ]]; do
  [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
  [[ $line =~ $setting ]] || die "$conf: not a setting: ${line%%=*}"
  printf -v "${BASH_REMATCH[1]}" '%s' "${BASH_REMATCH[2]}"
done <"$conf"
[[ -n $RUNNER_USER && -n $TEMPLATE ]] || die "$conf: RUNNER_USER and TEMPLATE are required"
[[ $RESERVE_MB =~ ^[0-9]{1,7}$ && $MAX_SECONDS =~ ^[1-9][0-9]{0,6}$ ]] || die "$conf: RESERVE_MB and MAX_SECONDS are numbers"

NAME='^[a-z0-9][a-z0-9-]{0,62}-[0-9]{1,20}$'
name_ok() { [[ $1 =~ $NAME ]] || die "not a runner name: $1"; }

active() {
  local unit rest
  "$SYSTEMCTL" list-units --all --plain --no-legend --type=service 'fork-fleet-*' | while read -r unit rest; do
    unit=${unit%.service}
    unit=${unit#fork-fleet-}
    if [[ $unit =~ $NAME ]]; then printf '%s\n' "$unit"; fi
  done
}

fits() {
  local kb
  [[ $1 =~ ^[0-9]{1,7}$ ]] || die "fits: not a number of megabytes: $1"
  kb=$(awk '$1 == "MemAvailable:" { print $2 }' "$MEMINFO")
  [[ $kb =~ ^[0-9]+$ ]] || die "no MemAvailable in $MEMINFO"
  ((kb / 1024 - RESERVE_MB >= $1))
}

start() {
  local name=$1 dir env_file line key value uid
  local -A got=()
  name_ok "$name"
  while IFS= read -r line; do
    [[ $line =~ ^(ACTIONS_RUNNER_INPUT_JITCONFIG|FORK_FLEET_RUN|FORK_FLEET_ATTEMPT)=(.*)$ ]] || die "start: an unexpected input line"
    key=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
    [[ -z ${got[$key]+set} ]] || die "start: $key twice"
    case $key in
      # A length check of its own: bash's regex refuses a bound above 255 (it would refuse every config).
      ACTIONS_RUNNER_INPUT_JITCONFIG) [[ $value =~ ^[A-Za-z0-9+/=]+$ ]] && ((${#value} <= 65536)) ;;
      *) [[ $value =~ ^[1-9][0-9]{0,19}$ ]] ;;
    esac || die "start: $key is not well formed"
    got[$key]=$value
  done
  ((${#got[@]} == 3)) || die "start: needs ACTIONS_RUNNER_INPUT_JITCONFIG, FORK_FLEET_RUN and FORK_FLEET_ATTEMPT"

  # The template: an unconfigured runner nobody but this user can change (each copy hard-links its files).
  owned_safe "$TEMPLATE"
  [[ -x $TEMPLATE/run.sh ]] || die "$TEMPLATE has no run.sh"
  for f in .env .path .runner .credentials .credentials_rsaparams; do
    [[ ! -e $TEMPLATE/$f ]] || die "$TEMPLATE/$f exists: the template must be an unconfigured runner (.github/fork-fleet.md)"
  done
  [[ -z $(find "$TEMPLATE" \( ! -uid "$EUID" -o \( ! -type l -perm /022 \) \) -print -quit) ]] ||
    die "$TEMPLATE has a file that is not this user's or that others may write"
  owned_safe "$HOOK"
  uid=$(id -u -- "$RUNNER_USER" 2>/dev/null) || die "no user $RUNNER_USER"
  [[ $uid != 0 && $uid != "$EUID" ]] || die "RUNNER_USER must be a user of its own, not $RUNNER_USER"

  # WORK_ROOT: RUNNER_USER passes through it to its own copy (0711: it cannot list the others). RUN_DIR: root only.
  (umask 022 && mkdir -p -- "$WORK_ROOT")
  mkdir -p -- "$RUN_DIR"
  owned_safe "$WORK_ROOT"
  owned_safe "$RUN_DIR"
  chmod 0711 -- "$WORK_ROOT"
  chmod 0700 -- "$RUN_DIR"
  dir=$WORK_ROOT/$name
  env_file=$RUN_DIR/$name.env
  [[ ! -e $dir ]] || die "$dir exists: a runner of that name ran already (prune removes it once its unit is gone)"
  # Expanded now: the trap runs at exit, after this function's locals are gone (set -u would fail it, file left).
  trap "rm -f -- $(printf '%q' "$env_file")" EXIT
  cp -al -- "$TEMPLATE" "$dir"
  chown -- "$RUNNER_USER:" "$dir"
  chmod 0700 -- "$dir"
  {
    printf 'ACTIONS_RUNNER_INPUT_JITCONFIG=%s\n' "${got[ACTIONS_RUNNER_INPUT_JITCONFIG]}"
    printf 'FORK_FLEET_RUN=%s\nFORK_FLEET_ATTEMPT=%s\n' "${got[FORK_FLEET_RUN]}" "${got[FORK_FLEET_ATTEMPT]}"
    printf 'ACTIONS_RUNNER_HOOK_JOB_STARTED=%s\n' "$HOOK"
  } >"$env_file"
  # Type=exec: systemd-run returns once run.sh started, so the environment file has been read and can go.
  "$SYSTEMD_RUN" --quiet --unit "fork-fleet-$name" --collect --uid "$RUNNER_USER" --working-directory "$dir" \
    -p Type=exec -p KillMode=control-group -p "EnvironmentFile=$env_file" -p NoNewPrivileges=yes \
    -p PrivateTmp=yes -p "RuntimeMaxSec=$MAX_SECONDS" -- "$dir/run.sh"
}

prune() {
  local live entry name
  live=$(active)
  for entry in "$WORK_ROOT"/* "$RUN_DIR"/*.env; do
    [[ -e $entry && ! -L $entry ]] || continue
    name=${entry##*/}
    name=${name%.env}
    [[ $name =~ $NAME ]] || continue
    if grep -qxF -- "$name" <<<"$live"; then continue; fi
    rm -rf --one-file-system -- "$entry"
  done
}

case "${1-}:$#" in
  start:2) start "$2" ;;
  active:1) active ;;
  fits:2) fits "$2" ;;
  stop:2)
    name_ok "$2"
    "$SYSTEMCTL" stop -- "fork-fleet-$2.service"
    ;;
  prune:1) prune ;;
  *) die "usage: start NAME | active | fits MB | stop NAME | prune" ;;
esac
