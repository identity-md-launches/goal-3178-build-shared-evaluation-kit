#!/usr/bin/env bash
# bootstrap/deadline.sh - overall deadline and owned-child watchdog for setup_fast.sh.
#
# Credit: Taylor (gtmcknight), solved-policy research,
# github.com/gtmcknight/amm-sim @ 4812a6382311624e1506aca131ca35c378f930d9 (research/solved).
#
# Portable bash 3.2+ (Linux, macOS): only `kill`, `wait`, `sleep` and job control. No GNU
# `timeout`, no `setsid`, no `ps`/`pgrep` and no process discovery of any kind: the only
# processes ever signalled are the child this library started and its process group.
#
# Sourced API (setup_fast.sh does `. bootstrap/deadline.sh`):
#   dl_init SECONDS DIR        start the clock; DIR holds the small flag files (created by caller)
#   dl_elapsed_ms              milliseconds since dl_init
#   dl_left_ms                 milliseconds left (0 when expired)
#   dl_expired                 true if the budget is used up or a watchdog has fired
#   dl_check LABEL             call the expiry hook (default: exit 75) if no time is left
#   dl_run LABEL CMD [ARGS..]  run CMD as a child in its own process group, bounded by the time
#                              left; the whole group is sent TERM then KILL on expiry, reaped,
#                              and the expiry hook runs. Returns CMD's exit status otherwise.
#   DL_ON_EXPIRE=func          hook called as `func LABEL` on expiry (must not return; default
#                              dl_default_expire prints a resume hint and exits 75)
#   dl_abort                   INT/TERM/HUP handler: kill the owned group and exit 130
#   dl_cleanup                 EXIT hook: kill a still-running owned group, remove the flag files
#
# Command line (used by tests/test_setup_deadline.py):
#   bash bootstrap/deadline.sh --seconds N -- CMD [ARGS..]
# runs CMD under an N second deadline; exits 75 when it expires, else with CMD's status.
#
# How dl_run works: job control (`set -m`) gives the child its own process group. A watchdog
# subshell, with stdout and stderr closed so it can never hold a caller's pipe open, polls that
# run's "live" token file every 0.05 s; when the absolute deadline passes while the token exists
# it writes the expiry flag and signals the group. When the child is reaped the token is removed
# and the watchdog ends by itself within one poll, so no sleeper is ever orphaned and nothing is
# signalled after the child is gone.

DL_T0_MS=""; DL_BUDGET_MS=0; DL_END_MS=0; DL_FLAG=""; DL_LIVE=""; DL_LIVE_NOW=""; DL_PID=""; DL_WD=""

dl_now_ms() {
  local t
  if [ -n "${EPOCHREALTIME:-}" ]; then          # bash 5: microseconds, no fork, portable across OSes
    t=${EPOCHREALTIME/./}; echo $(( ${t%???} ))
    return
  fi
  t=$(date +%s%N 2>/dev/null || true)             # GNU date: nanoseconds
  case "$t" in ''|*[!0-9]*) echo $(( $(date +%s) * 1000 ));; *) echo $(( t / 1000000 ));; esac
}
dl_nap() { sleep 0.05 2>/dev/null || sleep 1; }   # a sleep without fractional support still works

dl_init() {
  DL_BUDGET_MS=$(( $1 * 1000 ))
  DL_T0_MS=$(dl_now_ms)
  DL_END_MS=$(( DL_T0_MS + DL_BUDGET_MS ))
  DL_FLAG="$2/deadline.expired.$$"
  DL_LIVE="$2/deadline.live.$$"
  rm -f "$DL_FLAG" "$DL_LIVE".*
}
dl_elapsed_ms() { echo $(( $(dl_now_ms) - DL_T0_MS )); }
dl_left_ms() {
  local left=$(( DL_END_MS - $(dl_now_ms) ))
  [ "$left" -gt 0 ] || left=0
  echo "$left"
}
dl_expired() { [ -e "$DL_FLAG" ] || [ "$(dl_left_ms)" -le 0 ]; }

dl_default_expire() {
  echo "deadline: ${1:-?}: budget of $(( DL_BUDGET_MS / 1000 ))s used up; rerun the same command to resume" >&2
  exit 75
}
dl_expire() { "${DL_ON_EXPIRE:-dl_default_expire}" "$1"; exit 75; }
dl_check() { if dl_expired; then dl_expire "$1"; fi; }

dl_fmt_s() { printf '%d.%03d' $(( $1 / 1000 )) $(( $1 % 1000 )); }

# Signal the owned child's whole process group (TERM, a grace period, then KILL) and reap it.
dl_kill_group() {
  [ -n "$DL_PID" ] || return 0
  kill -TERM -- "-$DL_PID" 2>/dev/null || kill -TERM "$DL_PID" 2>/dev/null || true
  local n=0
  while kill -0 "$DL_PID" 2>/dev/null && [ "$n" -lt 20 ]; do dl_nap; n=$(( n + 1 )); done
  kill -KILL -- "-$DL_PID" 2>/dev/null || true
  wait "$DL_PID" 2>/dev/null || true
  DL_PID=""
}
# The watchdog ends on its own once the live token is gone; wait for it so nothing is left behind.
dl_stop_watchdog() {
  [ -n "$DL_WD" ] || return 0
  [ -z "${DL_LIVE_NOW:-}" ] || rm -f "$DL_LIVE_NOW"
  wait "$DL_WD" 2>/dev/null || true
  DL_WD=""
}

dl_run() {
  local label=$1; shift
  local rc=0 live
  dl_check "$label"
  # Job control gives the child its own process group, so every descendant it starts can be
  # signalled together. stdin is closed: a background group must never wait on the terminal.
  set -m
  "$@" </dev/null &
  DL_PID=$!
  set +m
  live="$DL_LIVE.$DL_PID"            # one token per run: an older watchdog never sees a newer child
  DL_LIVE_NOW=$live
  : > "$live"
  ( exec </dev/null >/dev/null 2>&1
    trap 'exit 0' TERM INT HUP
    while [ -e "$live" ]; do
      if [ "$(dl_now_ms)" -ge "$DL_END_MS" ]; then
        : > "$DL_FLAG"
        kill -TERM -- "-$DL_PID" 2>/dev/null || kill -TERM "$DL_PID" 2>/dev/null
        n=0
        while [ -e "$live" ] && [ "$n" -lt 40 ]; do dl_nap; n=$(( n + 1 )); done
        kill -KILL -- "-$DL_PID" 2>/dev/null; kill -KILL "$DL_PID" 2>/dev/null
        exit 0
      fi
      dl_nap
    done
    exit 0 ) &
  DL_WD=$!
  wait "$DL_PID" 2>/dev/null || rc=$?
  rm -f "$live"
  DL_PID=""
  # The watchdog holds no descriptor and ends by itself within one poll; it is reaped by the next
  # dl_stop_watchdog (abort or exit) rather than waited for here, so a finished child returns at once.
  if [ -e "$DL_FLAG" ]; then
    dl_expire "$label"
  fi
  return "$rc"
}

dl_abort() {
  trap - INT TERM HUP
  [ -z "$DL_WD" ] || kill -TERM "$DL_WD" 2>/dev/null || true
  dl_stop_watchdog
  dl_kill_group
  echo "deadline: interrupted; rerun the same command to resume" >&2
  exit 130
}
dl_cleanup() {
  dl_stop_watchdog
  dl_kill_group
  [ -z "$DL_FLAG" ] || rm -f "$DL_FLAG" "$DL_LIVE".*
}

# ---- command line mode ---------------------------------------------------------------------------
dl_main() {
  local secs="" dir=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --seconds) [ $# -ge 2 ] || { echo "deadline.sh: --seconds needs a value" >&2; exit 2; }; secs=$2; shift 2;;
      --flag-dir) [ $# -ge 2 ] || { echo "deadline.sh: --flag-dir needs a value" >&2; exit 2; }; dir=$2; shift 2;;
      --) shift; break;;
      -h|--help) sed -n '2,35p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0;;
      *) echo "deadline.sh: unknown argument $1" >&2; exit 2;;
    esac
  done
  case "$secs" in ''|*[!0-9]*) echo "deadline.sh: --seconds N (whole seconds) is required" >&2; exit 2;; esac
  [ $# -gt 0 ] || { echo "deadline.sh: no command after --" >&2; exit 2; }
  if [ -z "$dir" ]; then dir="${TMPDIR:-/tmp}"; fi
  [ -d "$dir" ] || { echo "deadline.sh: flag dir $dir does not exist" >&2; exit 2; }
  dl_init "$secs" "$dir"
  trap dl_abort INT TERM HUP
  trap dl_cleanup EXIT
  local rc=0
  dl_run "command" "$@" || rc=$?
  exit "$rc"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -u
  dl_main "$@"
fi
