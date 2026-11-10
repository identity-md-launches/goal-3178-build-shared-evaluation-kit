#!/usr/bin/env bash
# setup_fast.sh - one-command, hash-pinned setup for the IdentityMD Paradigm AMM evaluation kit.
#
# Credit: Taylor (gtmcknight), solved-policy research,
# github.com/gtmcknight/amm-sim @ 4812a6382311624e1506aca131ca35c378f930d9 (research/solved).
# This script replaces Taylor's setup-fast.sh (release sim-6b6921f8) with the same idea, no Rust and
# a prebuilt amm_sim_rs wheel, but with every downloaded byte pinned in bootstrap/manifest.tsv.
#
# Usage:
#   bash setup_fast.sh --platform linux-x86_64 [--replica-only] [--cache PATH] [--offline]
#   bash setup_fast.sh --platform macos-arm64 --replica-only [--cache PATH] [--offline]
#
#   --platform P     required: linux-x86_64 or macos-arm64. It selects the artifacts; the machine
#                    is never inspected. A platform that does not match shows up as exit 3 when
#                    its interpreter fails to run, before anything is installed into the venv.
#   --replica-only   CPython 3.12 + numpy/numba/llvmlite only (the only mode macOS supports)
#   --cache PATH     reusable download cache (default: $HOME/.cache/imd-amm)
#   --offline        never touch the network; fail listing every file the cache lacks
#   --scratch PATH   where all runtime state goes (default: <repo>/scratch)
#   --fetch-only     download and verify into the cache, install nothing (any machine)
#   --smoke          after setup, run the bounded smokes in bootstrap/ (engine: seed 9700000 only)
#   --force          reinstall even if scratch already holds this exact pinned set
#   --deadline SEC   stop cleanly (exit 75) once SEC seconds are used (default 420, max 470);
#                    rerun the same command to resume from the last finished step
#   --manifest FILE  test hook: alternative manifest (file:// URLs are allowed only here)
#
# Exit codes: 0 ok, 2 usage or refused platform/mode, 3 the selected platform's interpreter does
#   not run on this machine, 4 sha256/size mismatch, 5 offline cache miss, 6 download failed,
#   7 install or verify failed, 75 deadline reached (verified downloads and finished steps are
#   kept; rerun the same command), 130 interrupted.
#
# The deadline is total wall clock and covers every path, cached ones included: cache hashing,
# downloads, extraction, venv, pip, verification and the smokes all run as owned children under
# bootstrap/deadline.sh, which signals only its own child's process group when time runs out.
# Progress is recorded atomically per finished step, and no success line is printed after expiry.
#
# Needs only: bash, curl (online runs), tar, and sha256sum or shasum. No Rust, no compiler,
# no package index: wheels are installed from verified cache files by the pip bundled in the
# pinned CPython archive, with --no-index --no-deps --require-hashes.
set -euo pipefail

SETUP_FAST_VERSION=2
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLATFORM=""; REPLICA_ONLY=0; OFFLINE=0; FETCH_ONLY=0; SMOKE=0; FORCE=0
CACHE=""; SCRATCH=""; MANIFEST=""; DEADLINE_S=420
MAX_XFER_S=240      # per-transfer wall clock bound (also capped by the time left)
CONNECT_S=20
ATTEMPTS=3

say() { echo "setup_fast: $*"; }
die() { local code=$1; shift; echo "setup_fast: ERROR: $*" >&2; exit "$code"; }
usage() { awk 'NR > 1 && /^set -euo/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; }
fmt_s() { printf '%d.%03d' $(( $1 / 1000 )) $(( $1 % 1000 )); }
abspath() { case "$1" in /*) printf '%s\n' "$1";; *) printf '%s\n' "$PWD/$1";; esac; }

need_arg() { [ $# -ge 2 ] || die 2 "$1 needs a value"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) need_arg "$@"; PLATFORM=$2; shift 2;;
    --platform=*) PLATFORM=${1#*=}; shift;;
    --replica-only) REPLICA_ONLY=1; shift;;
    --cache) need_arg "$@"; CACHE=$2; shift 2;;
    --cache=*) CACHE=${1#*=}; shift;;
    --offline) OFFLINE=1; shift;;
    --scratch) need_arg "$@"; SCRATCH=$2; shift 2;;
    --scratch=*) SCRATCH=${1#*=}; shift;;
    --manifest) need_arg "$@"; MANIFEST=$2; shift 2;;
    --manifest=*) MANIFEST=${1#*=}; shift;;
    --deadline) need_arg "$@"; DEADLINE_S=$2; shift 2;;
    --deadline=*) DEADLINE_S=${1#*=}; shift;;
    --fetch-only) FETCH_ONLY=1; shift;;
    --smoke) SMOKE=1; shift;;
    --force) FORCE=1; shift;;
    -h|--help) usage; exit 0;;
    *) die 2 "unknown argument: $1 (see --help)";;
  esac
done

# ---- platform / mode refusal (pure argument checks: the machine is not inspected) ----------------
case "$PLATFORM" in
  linux-x86_64|macos-arm64) ;;
  "") die 2 "--platform is required: linux-x86_64 or macos-arm64 (it is never guessed)";;
  *) die 2 "unsupported --platform '$PLATFORM': supported are linux-x86_64 and macos-arm64";;
esac
if [ "$PLATFORM" = macos-arm64 ] && [ "$REPLICA_ONLY" = 0 ]; then
  die 2 "macos-arm64 supports replica work only: the amm_sim_rs wheel is Linux x86_64. Rerun with --replica-only"
fi
case "$DEADLINE_S" in ''|*[!0-9]*) die 2 "--deadline needs whole seconds";; esac
[ "$DEADLINE_S" -le 470 ] || die 2 "--deadline may not exceed 470 seconds (no command over 8 minutes)"
MODE=engine; [ "$REPLICA_ONLY" = 1 ] && MODE=replica

# ---- paths ---------------------------------------------------------------------------------------
if [ -z "$CACHE" ]; then
  [ -n "${HOME:-}" ] || die 2 "HOME is unset: pass --cache PATH"
  CACHE="$HOME/.cache/imd-amm"
fi
CACHE=$(abspath "$CACHE")
[ -n "$SCRATCH" ] || SCRATCH="$ROOT/scratch"
SCRATCH=$(abspath "$SCRATCH")
MANIFEST_OVERRIDE=0
if [ -n "$MANIFEST" ]; then MANIFEST=$(abspath "$MANIFEST"); MANIFEST_OVERRIDE=1; else MANIFEST="$ROOT/bootstrap/manifest.tsv"; fi
[ -f "$MANIFEST" ] || die 2 "manifest not found: $MANIFEST"
[ -f "$ROOT/bootstrap/pins.sh" ] || die 2 "missing $ROOT/bootstrap/pins.sh"
[ -f "$ROOT/bootstrap/deadline.sh" ] || die 2 "missing $ROOT/bootstrap/deadline.sh"
case "$SCRATCH$CACHE" in *"'"*) die 2 "paths containing a single quote are not supported";; esac
# shellcheck source=bootstrap/pins.sh
. "$ROOT/bootstrap/pins.sh"
# shellcheck source=bootstrap/deadline.sh
. "$ROOT/bootstrap/deadline.sh"

if command -v sha256sum >/dev/null 2>&1; then
  sha256_of() { sha256sum "$1" | cut -d' ' -f1; }
  sha256_stdin() { sha256sum | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
  sha256_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
  sha256_stdin() { shasum -a 256 | cut -d' ' -f1; }
else
  die 7 "need sha256sum or shasum to verify downloads"
fi

# ---- deadline: one clock for the whole run, every child bounded by what is left ------------------
STATE="$SCRATCH/.setup_fast"
TMP="$SCRATCH/tmp"
mkdir -p "$STATE" "$TMP" "$CACHE/sha256" "$CACHE/tmp"
dl_init "$DEADLINE_S" "$STATE"
elapsed_ms() { dl_elapsed_ms; }
on_deadline() {
  say "deadline of ${DEADLINE_S}s reached during: $1 (after $(fmt_s "$(elapsed_ms)")s). Verified downloads and finished steps stay; nothing was marked complete."
  say "resume: rerun the same command."
  exit 75
}
DL_ON_EXPIRE=on_deadline
PART=""
WORK=""
cleanup() { dl_cleanup; [ -z "$PART" ] || rm -f "$PART"; [ -z "$WORK" ] || rm -rf "$WORK"; }
trap cleanup EXIT
trap dl_abort INT TERM HUP
dl_check "start"
HASHOUT="$TMP/sha256.$$"
hash_to() { sha256_of "$1" > "$2"; }          # run through dl_run: hashing is a bounded child too
# dl_run must never be called inside $(...): its exit 75 has to end this script, not a subshell.

# ---- read the pinned set for this platform and mode ----------------------------------------------
N=0
SEL_KIND=(); SEL_SHA=(); SEL_SIZE=(); SEL_NAME=(); SEL_URL=()
while IFS=$'\t' read -r m_set m_kind m_sha m_size m_name m_url || [ -n "${m_set:-}" ]; do
  case "${m_set:-}" in ''|'#'*) continue;; esac
  want=0
  [ "$m_set" = "$PLATFORM:replica" ] && want=1
  if [ "$REPLICA_ONLY" = 0 ] && [ "$m_set" = "$PLATFORM:engine" ]; then want=1; fi
  [ "$want" = 1 ] || continue
  case "$m_sha" in *[!0-9a-f]*) die 2 "manifest: bad sha256 for ${m_name:-?}";; esac
  [ "${#m_sha}" = 64 ] || die 2 "manifest: sha256 for ${m_name:-?} is not 64 hex digits"
  case "${m_size:-}" in ''|*[!0-9]*) die 2 "manifest: bad size for ${m_name:-?}";; esac
  case "${m_name:-}" in ''|*/*|.*) die 2 "manifest: bad filename '${m_name:-}'";; esac
  case "${m_url:-}" in
    https://*) ;;
    file://*) [ "$MANIFEST_OVERRIDE" = 1 ] || die 2 "manifest: only https:// URLs are allowed ($m_name)";;
    *) die 2 "manifest: only https:// URLs are allowed ($m_name)";;
  esac
  case "$m_kind" in python|wheel|engine-src|harness|solc) ;; *) die 2 "manifest: unknown kind '$m_kind'";; esac
  SEL_KIND[N]=$m_kind; SEL_SHA[N]=$m_sha; SEL_SIZE[N]=$m_size; SEL_NAME[N]=$m_name; SEL_URL[N]=$m_url
  N=$(( N + 1 ))
done < "$MANIFEST"
[ "$N" -gt 0 ] || die 2 "manifest $MANIFEST has no rows for $PLATFORM ($MODE)"
cache_path() { printf '%s\n' "$CACHE/sha256/${SEL_SHA[$1]}/${SEL_NAME[$1]}"; }

say "platform=$PLATFORM mode=$MODE offline=$OFFLINE files=$N deadline=${DEADLINE_S}s"
say "cache=$CACHE"
say "scratch=$SCRATCH"

# ---- fetch: verify cache hits, download misses (bounded, atomic) ---------------------------------
download() {
  local i=$1 sha=${SEL_SHA[$1]} size=${SEL_SIZE[$1]} name=${SEL_NAME[$1]} url=${SEL_URL[$1]}
  local dest; dest=$(cache_path "$i")
  local attempt=1 rc got_size got_sha left maxt
  command -v curl >/dev/null 2>&1 || die 6 "curl is required to download $name (or pre-fill the cache and use --offline)"
  while :; do
    dl_check "download of $name"
    left=$(( $(dl_left_ms) / 1000 )); maxt=$MAX_XFER_S
    [ "$left" -lt "$maxt" ] && maxt=$left
    [ "$maxt" -ge 1 ] || maxt=1
    PART="$CACHE/tmp/$name.$$.part"
    rc=0
    dl_run "download of $name" curl -fsSL --proto '=https,file' --proto-redir '=https' --connect-timeout "$CONNECT_S" \
      --max-time "$maxt" --max-filesize "$size" -o "$PART" "$url" || rc=$?
    if [ "$rc" = 0 ]; then
      got_size=$(wc -c < "$PART" | tr -d ' ')
      if [ "$got_size" = "$size" ]; then break; fi
      rc="size $got_size != pinned $size"
    fi
    rm -f "$PART"; PART=""
    case "$rc" in
      63|size*) die 4 "size mismatch for $name ($rc; pinned $size bytes) from $url. Nothing was cached.";;
    esac
    [ "$attempt" -lt "$ATTEMPTS" ] || die 6 "download failed after $ATTEMPTS attempts (curl exit $rc): $url"
    say "retrying $name (curl exit $rc, attempt $attempt of $ATTEMPTS)"
    attempt=$(( attempt + 1 )); sleep 1
  done
  dl_run "sha256 of downloaded $name" hash_to "$PART" "$HASHOUT"
  got_sha=$(cat "$HASHOUT")
  if [ "$got_sha" != "$sha" ]; then
    rm -f "$PART"; PART=""
    die 4 "sha256 mismatch for $name: pinned $sha, downloaded $got_sha, from $url. Nothing was cached."
  fi
  mkdir -p "$(dirname "$dest")"
  mv -f "$PART" "$dest"; PART=""      # same filesystem: atomic rename, never a partial cache entry
  DL_COUNT=$(( DL_COUNT + 1 )); DL_BYTES=$(( DL_BYTES + size ))
}

DL_COUNT=0; DL_BYTES=0; HIT_COUNT=0; MISSES=""; MISS_COUNT=0
i=0
while [ "$i" -lt "$N" ]; do
  dest=$(cache_path "$i")
  dl_check "cache check of ${SEL_NAME[$i]}"
  if [ -f "$dest" ]; then
    dl_run "cache check of ${SEL_NAME[$i]}" hash_to "$dest" "$HASHOUT"
    got=$(cat "$HASHOUT")
    if [ "$got" = "${SEL_SHA[$i]}" ]; then
      HIT_COUNT=$(( HIT_COUNT + 1 )); i=$(( i + 1 )); continue
    fi
    if [ "$OFFLINE" = 1 ]; then
      die 4 "cache entry is corrupt: $dest has sha256 $got, pinned ${SEL_SHA[$i]}. --offline cannot replace it: delete that file and rerun online once."
    fi
    say "cache entry corrupt, re-downloading: $dest"
    rm -f "$dest"
  fi
  if [ "$OFFLINE" = 1 ]; then
    MISS_COUNT=$(( MISS_COUNT + 1 ))
    MISSES="$MISSES
  missing: ${SEL_NAME[$i]}  sha256=${SEL_SHA[$i]}  bytes=${SEL_SIZE[$i]}
           url=${SEL_URL[$i]}
           expected at $dest"
  else
    download "$i"
  fi
  i=$(( i + 1 ))
done
rm -f "$HASHOUT"
if [ "$MISS_COUNT" -gt 0 ]; then
  echo "setup_fast: ERROR: --offline and $MISS_COUNT of $N pinned files are not in the cache $CACHE:$MISSES" >&2
  echo "setup_fast: fix: run once without --offline using the same --platform, mode and --cache, or copy the files to the paths above." >&2
  exit 5
fi
T_FETCH=$(elapsed_ms)
SETUP_KIND=cached; [ "$DL_COUNT" -gt 0 ] && SETUP_KIND=cold

summary() {
  dl_check "final summary"       # a run that used its budget never reports success
  local total; total=$(elapsed_ms)
  local line="OK platform=$PLATFORM mode=$MODE setup=$SETUP_KIND downloads=$DL_COUNT download_bytes=$DL_BYTES cache_hits=$HIT_COUNT install=$1 fetch_s=$(fmt_s "$T_FETCH") install_s=$(fmt_s "${T_INSTALL:-0}") verify_s=$(fmt_s "${T_VERIFY:-0}") smoke_s=$(fmt_s "${T_SMOKE:-0}") total_s=$(fmt_s "$total") deadline_s=$DEADLINE_S"
  say "$line"
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line" >> "$STATE/timings.log"
}

if [ "$FETCH_ONLY" = 1 ]; then
  dl_check "fetch-only summary"
  summary not-requested
  exit 0
fi

# ---- install into scratch (checkpointed per step; a matching stamp means nothing is redone) ------
idx_of_kind() { local k=0; while [ "$k" -lt "$N" ]; do [ "${SEL_KIND[$k]}" = "$1" ] && { echo "$k"; return 0; }; k=$(( k + 1 )); done; return 1; }
PY_I=$(idx_of_kind python) || die 2 "manifest has no python archive for $PLATFORM"
PYHOME="$SCRATCH/python/${SEL_SHA[$PY_I]:0:16}"
BASEPY="$PYHOME/python/bin/python3"
VENV="$SCRATCH/venv"
VPY="$VENV/bin/python"
ENGINE_DIR="$SCRATCH/engine"
ENGINE_ROOT="$ENGINE_DIR/amm-challenge"
SOLCX_DIR="$SCRATCH/solcx"
KEY=$( { echo "v$SETUP_FAST_VERSION $PLATFORM $MODE $SCRATCH"; cat "$ROOT/bootstrap/pins.sh"; i=0; while [ "$i" -lt "$N" ]; do echo "${SEL_KIND[$i]} ${SEL_SHA[$i]} ${SEL_NAME[$i]}"; i=$(( i + 1 )); done; } | sha256_stdin )
STAMP="$STATE/installed.key"
PROGRESS="$STATE/progress"       # one "<key> <step>" line per finished step, rewritten atomically
step_done() { [ -f "$PROGRESS" ] && grep -qxF "$KEY $1" "$PROGRESS"; }
mark_step() {
  { [ -f "$PROGRESS" ] && cat "$PROGRESS"; echo "$KEY $1"; } > "$PROGRESS.$$"
  mv -f "$PROGRESS.$$" "$PROGRESS"
}
probe_py() { "$BASEPY" -I -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' > "$1"; }
site_py() { "$VPY" -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' > "$1"; }
in_env() { . "$SCRATCH/env.sh"; exec "$@"; }

write_env() {
  local f="$SCRATCH/env.sh.$$"
  {
    echo "# scratch/env.sh - generated by setup_fast.sh; source it: . scratch/env.sh"
    echo "# Credit: Taylor (gtmcknight), solved-policy research,"
    echo "# github.com/gtmcknight/amm-sim @ 4812a6382311624e1506aca131ca35c378f930d9 (research/solved)."
    echo "export IMD_AMM_PLATFORM='$PLATFORM' IMD_AMM_MODE='$MODE'"
    echo "export IMD_AMM_SCRATCH='$SCRATCH'"
    echo "export IMD_AMM_PYTHON='$VPY'"
    echo "export VIRTUAL_ENV='$VENV'"
    echo "export TMPDIR='$TMP' XDG_CACHE_HOME='$SCRATCH/xc' NUMBA_CACHE_DIR='$SCRATCH/numba_cache'"
    echo "export PYTHONNOUSERSITE=1 PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1"
    if [ "$MODE" = engine ]; then
      echo "export IMD_AMM_ENGINE='$ENGINE_ROOT'"
      echo "export IMD_AMM_HARNESS='$ENGINE_DIR/harness.py'"
      echo "export SOLCX_BINARY_PATH='$SOLCX_DIR'"
    else
      echo "# replica mode has no engine: drop engine settings a previously sourced env.sh may have left"
      echo "unset IMD_AMM_ENGINE IMD_AMM_HARNESS SOLCX_BINARY_PATH"
    fi
    echo "export PATH='$VENV/bin':\"\$PATH\""
  } > "$f"
  mv -f "$f" "$SCRATCH/env.sh"
}

INSTALL=reused
T_I0=$(elapsed_ms)
if [ "$FORCE" = 1 ] || [ ! -f "$STAMP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$KEY" ] || [ ! -x "$VPY" ]; then
  INSTALL=fresh
  rm -f "$STAMP"
  [ "$FORCE" = 0 ] || rm -f "$PROGRESS"
  if step_done wheels; then INSTALL=resumed; fi
  mkdir -p "$SCRATCH/xc" "$SCRATCH/numba_cache"
  export TMPDIR="$TMP"
  # 1. interpreter (kept per archive hash, so a resume or re-install reuses the extraction)
  if [ ! -f "$PYHOME/.ok" ] || [ ! -x "$BASEPY" ]; then
    rm -rf "$PYHOME"
    WORK="$TMP/py.$$"; rm -rf "$WORK"; mkdir -p "$WORK"
    dl_run "interpreter extraction" tar --no-same-owner -xzf "$(cache_path "$PY_I")" -C "$WORK" || die 7 "could not extract ${SEL_NAME[$PY_I]}"
    [ -x "$WORK/python/bin/python3" ] || die 7 "python archive has no python/bin/python3"
    mkdir -p "$SCRATCH/python"; mv "$WORK" "$PYHOME"; WORK=""
    : > "$PYHOME/.ok"
  fi
  # The selected platform's interpreter must run here. If it cannot, --platform named a platform
  # this machine is not (or its libc is too old): refuse before anything reaches the venv.
  PROBE="$TMP/probe.$$"; rc=0
  dl_run "interpreter probe" probe_py "$PROBE" || rc=$?
  if [ "$rc" != 0 ]; then
    rm -rf "$PYHOME" "$PROBE"
    die 3 "the interpreter selected by --platform $PLATFORM does not run on this machine (exit $rc): ${SEL_NAME[$PY_I]}. Nothing was installed. Pass the --platform of this machine, or use --fetch-only to only download and verify that platform's files."
  fi
  PYV=$(cat "$PROBE"); rm -f "$PROBE"
  [ "$PYV" = "$IMD_PY_VERSION" ] || die 7 "interpreter is $PYV, pinned $IMD_PY_VERSION"
  # 2+3. venv without pip, then wheels: offline, no dependency resolution, hashes re-checked by pip
  if step_done wheels && [ -x "$VPY" ]; then
    say "resume: venv and wheels already installed for this pinned set"
  else
    rm -f "$PROGRESS"            # every later step depends on this venv
    rm -rf "$VENV"
    dl_run "venv creation" "$BASEPY" -I -m venv --without-pip "$VENV" || die 7 "venv creation failed"
    REQ="$TMP/requirements.$$.lock"; : > "$REQ"
    i=0
    while [ "$i" -lt "$N" ]; do
      if [ "${SEL_KIND[$i]}" = wheel ]; then
        printf '%s --hash=sha256:%s\n' "$(cache_path "$i")" "${SEL_SHA[$i]}" >> "$REQ"
      fi
      i=$(( i + 1 ))
    done
    dl_run "wheel installation" "$BASEPY" -I -m pip --isolated --python "$VPY" install --quiet --no-index --no-deps --require-hashes \
      --no-cache-dir --disable-pip-version-check --no-compile -r "$REQ" || die 7 "wheel installation failed"
    rm -f "$REQ"
    mark_step wheels
  fi
  # 4. engine: source checkout, harness, solc (Linux x86_64 only)
  if [ "$MODE" = engine ]; then
    if step_done engine && [ -f "$ENGINE_DIR/harness.py" ] && [ -d "$ENGINE_ROOT/amm_competition" ]; then
      say "resume: engine checkout already installed for this pinned set"
    else
      rm -rf "$ENGINE_DIR" "$SOLCX_DIR"
      SRC_I=$(idx_of_kind engine-src) || die 2 "manifest has no engine-src row"
      HAR_I=$(idx_of_kind harness) || die 2 "manifest has no harness row"
      SOLC_I=$(idx_of_kind solc) || die 2 "manifest has no solc row"
      WORK="$TMP/eng.$$"; rm -rf "$WORK"; mkdir -p "$WORK" "$ENGINE_DIR" "$SOLCX_DIR"
      dl_run "engine checkout" tar --no-same-owner -xzf "$(cache_path "$SRC_I")" -C "$WORK" || die 7 "could not extract engine source"
      [ -d "$WORK/amm-challenge-$IMD_ENGINE_COMMIT/amm_competition" ] || die 7 "engine archive is not amm-challenge@$IMD_ENGINE_COMMIT"
      mv "$WORK/amm-challenge-$IMD_ENGINE_COMMIT" "$ENGINE_ROOT"
      rm -rf "$WORK"; WORK=""
      cp "$(cache_path "$HAR_I")" "$ENGINE_DIR/harness.py"
      cp "$(cache_path "$SOLC_I")" "$SOLCX_DIR/solc-v$IMD_SOLC_VERSION"
      chmod 755 "$SOLCX_DIR/solc-v$IMD_SOLC_VERSION"
      # put the harness package on sys.path without a build backend (no setuptools download)
      PROBE="$TMP/probe.$$"
      dl_run "site-packages probe" site_py "$PROBE" || die 7 "venv interpreter does not run"
      SITE=$(cat "$PROBE"); rm -f "$PROBE"
      printf '%s\n' "$ENGINE_ROOT" > "$SITE/imd_amm_engine.pth"
      mark_step engine
    fi
  else
    rm -rf "$ENGINE_DIR" "$SOLCX_DIR"
  fi
fi
write_env
T_INSTALL=$(( $(elapsed_ms) - T_I0 ))

# ---- verify: real imports, pinned versions, engine binary hash ----------------------------------
T_V0=$(elapsed_ms)
dl_run "verification" in_env "$VPY" -I "$ROOT/bootstrap/verify_env.py" --mode "$MODE" --python "$IMD_PY_VERSION" \
    --numpy "$IMD_NUMPY_VERSION" --numba "$IMD_NUMBA_VERSION" --llvmlite "$IMD_LLVMLITE_VERSION" \
    --pyrevm "$IMD_PYREVM_VERSION" --solcx "$IMD_PYSOLCX_VERSION" --solc "$IMD_SOLC_VERSION" \
    --engine-root "$ENGINE_ROOT" --engine-wheel "$IMD_ENGINE_WHEEL_VERSION" --so-sha256 "$IMD_ENGINE_SO_SHA256" \
  || die 7 "environment verification failed (see above). Rerun with --force to reinstall from the cache."
dl_check "install stamp"
printf '%s\n' "$KEY" > "$STAMP.$$"; mv -f "$STAMP.$$" "$STAMP"
T_VERIFY=$(( $(elapsed_ms) - T_V0 ))

# ---- optional bounded smokes ---------------------------------------------------------------------
T_SMOKE=0
if [ "$SMOKE" = 1 ]; then
  T_S0=$(elapsed_ms)
  dl_run "replica smoke" in_env "$VPY" -I "$ROOT/bootstrap/smoke_replica.py" --out "$STATE/smoke_replica.json" || die 7 "replica smoke failed"
  if [ "$MODE" = engine ]; then
    dl_run "engine smoke" in_env "$VPY" -I "$ROOT/bootstrap/smoke_engine.py" --seed "$IMD_SMOKE_ENGINE_SEED" --out "$STATE/smoke_engine.json" || die 7 "engine smoke failed"
  fi
  T_SMOKE=$(( $(elapsed_ms) - T_S0 ))
fi

say "interpreter: $VPY"
[ "$MODE" = engine ] && say "engine checkout: $ENGINE_ROOT (amm-challenge@$IMD_ENGINE_COMMIT), harness: $ENGINE_DIR/harness.py"
say "environment: . $SCRATCH/env.sh"
summary "$INSTALL"
