#!/usr/bin/env bash
# Set up tapesplit on macOS or Linux: system tools, a virtual environment, the Python
# extras for this platform, and a starter .env. Safe to run again; it skips what's there.
#
#   ./scripts/setup.sh              install what's missing, then run `tapesplit doctor`
#   ./scripts/setup.sh --check      only report what's installed and what's missing
#
# Options:
#   --check          report only; install nothing
#   --yes            don't ask before installing system packages (uses sudo on Linux)
#   --minimal        core package only, without the local AI extras (much smaller)
#   --skip-system    leave system packages alone; just the venv, extras and .env
#   --skip-whisper   don't install or build whisper.cpp
#   --with-model     also download a whisper.cpp model (ggml-large-v3-turbo, about 1.6 GB)
#   --python PATH    use this Python (3.11 or newer) for the virtual environment
#
# macOS uses Homebrew. Linux uses apt (Debian, Ubuntu), dnf (Fedora) or pacman (Arch);
# whisper.cpp is built from source there. Apple Vision is macOS only: on Linux those
# stages fall back to OpenCV.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
WHISPER_DIR="${WHISPER_CPP_DIR:-$HOME/.local/share/whisper.cpp}"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
MIN_PY_MINOR=11
MIN_NODE=20

CHECK=0 YES=0 MINIMAL=0 SKIP_SYSTEM=0 SKIP_WHISPER=0 WITH_MODEL=0 PYTHON=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --yes|-y) YES=1 ;;
    --minimal) MINIMAL=1 ;;
    --skip-system) SKIP_SYSTEM=1 ;;
    --skip-whisper) SKIP_WHISPER=1 ;;
    --with-model) WITH_MODEL=1 ;;
    --python) PYTHON="${2:?--python needs a path}"; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

if [ -t 1 ]; then B=$'\033[1m' G=$'\033[32m' Y=$'\033[33m' R=$'\033[31m' N=$'\033[0m'; else B="" G="" Y="" R="" N=""; fi
say()  { printf '%s\n' "${B}==>${N} $*"; }
ok()   { printf '  %s %s\n' "${G}ok${N}" "$*"; }
miss() { printf '  %s %s\n' "${Y}missing${N}" "$*"; }
die()  { printf '%s %s\n' "${R}error:${N}" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

confirm() {
  [ "$YES" = 1 ] && return 0
  [ -t 0 ] || die "$1 Re-run with --yes to allow it."
  printf '%s [y/N] ' "$1"
  read -r answer
  case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ------------------------------------------------------------------ platform
OS="$(uname -s)"
PM=""
case "$OS" in
  Darwin) PM="brew" ;;
  Linux)
    if have apt-get; then PM="apt"
    elif have dnf; then PM="dnf"
    elif have pacman; then PM="pacman"
    fi ;;
  *) die "tapesplit runs on macOS and Linux; this is $OS." ;;
esac
SUDO=""
if [ "$OS" = Linux ] && [ "$(id -u)" != 0 ]; then SUDO="sudo"; fi

say "tapesplit setup on $OS${PM:+ ($PM)}"

# ------------------------------------------------------------------ checks
py_ok() { "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_PY_MINOR) else 1)" 2>/dev/null; }

find_python() {
  if [ -n "$PYTHON" ]; then py_ok "$PYTHON" && { echo "$PYTHON"; return; }; die "$PYTHON is not Python 3.$MIN_PY_MINOR or newer."; fi
  for candidate in python3.12 python3.13 python3.11 python3; do
    if have "$candidate" && py_ok "$candidate"; then command -v "$candidate"; return; fi
  done
  if [ "$OS" = Darwin ] && have brew; then
    for prefix in "$(brew --prefix python@3.12 2>/dev/null)" "$(brew --prefix python@3.13 2>/dev/null)"; do
      [ -x "$prefix/bin/python3" ] && py_ok "$prefix/bin/python3" && { echo "$prefix/bin/python3"; return; }
    done
  fi
  echo ""
}

node_ok() { have node && [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -ge "$MIN_NODE" ]; }
whisper_ok() { have whisper-cli || have whisper-cpp || [ -x "$WHISPER_DIR/build/bin/whisper-cli" ]; }

report() {
  local py; py="$(find_python)"
  [ -n "$py" ] && ok "Python $("$py" -c 'import platform; print(platform.python_version())') ($py)" || miss "Python 3.$MIN_PY_MINOR or newer"
  have ffmpeg && ok "ffmpeg" || miss "ffmpeg"
  whisper_ok && ok "whisper.cpp" || miss "whisper.cpp (transcription)"
  node_ok && ok "Node $(node -v) (review app)" || miss "Node $MIN_NODE or newer (review app)"
  [ -x "$VENV/bin/tapesplit" ] && ok "virtual environment with tapesplit ($VENV)" || miss "virtual environment ($VENV)"
  [ -f "$ROOT/.env" ] && ok ".env" || miss ".env (copied from .env.example)"
}

if [ "$CHECK" = 1 ]; then
  report
  [ -x "$VENV/bin/tapesplit" ] && { echo; "$VENV/bin/tapesplit" doctor || true; }
  exit 0
fi

# ------------------------------------------------------------------ system packages
pkg_install() {
  local log; log="$(mktemp)"
  echo "  installing $* (log: $log)"
  if ! case "$PM" in
    brew) brew install "$@" ;;
    apt) $SUDO apt-get update -qq && $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o Dpkg::Use-Pty=0 "$@" ;;
    dnf) $SUDO dnf install -y -q "$@" ;;
    pacman) $SUDO pacman -S --needed --noconfirm "$@" ;;
  esac >"$log" 2>&1; then
    tail -n 20 "$log" >&2
    return 1
  fi
}

if [ "$SKIP_SYSTEM" = 0 ]; then
  say "System tools"
  if [ "$OS" = Darwin ] && ! have brew; then
    die "Homebrew is needed on macOS. Install it from https://brew.sh, then run this again."
  fi
  [ -n "$PM" ] || die "No supported package manager (apt, dnf, pacman). Install Python 3.$MIN_PY_MINOR+, ffmpeg, cmake, a C++ compiler and Node $MIN_NODE+, then run with --skip-system."

  want=()
  if [ -z "$(find_python)" ]; then
    case "$PM" in
      brew) want+=(python@3.12) ;;
      apt) want+=(python3 python3-venv python3-dev) ;;
      dnf) want+=(python3 python3-devel) ;;
      pacman) want+=(python) ;;
    esac
  elif [ "$PM" = apt ] && ! "$(find_python)" -c "import venv, ensurepip" 2>/dev/null; then
    want+=(python3-venv)
  fi
  have ffmpeg || case "$PM" in dnf) want+=(ffmpeg-free) ;; *) want+=(ffmpeg) ;; esac
  if [ "$SKIP_WHISPER" = 0 ] && ! whisper_ok; then
    case "$PM" in
      brew) want+=(whisper-cpp) ;;
      apt) have cmake || want+=(cmake); have g++ || want+=(build-essential); have git || want+=(git) ;;
      dnf) have cmake || want+=(cmake); have g++ || want+=(gcc-c++ make); have git || want+=(git) ;;
      pacman) have cmake || want+=(cmake); have g++ || want+=(base-devel); have git || want+=(git) ;;
    esac
  fi
  node_ok || case "$PM" in brew) want+=(node) ;; apt) want+=(nodejs npm) ;; dnf) want+=(nodejs npm) ;; pacman) want+=(nodejs npm) ;; esac
  have curl || want+=(curl)

  if [ ${#want[@]} -gt 0 ]; then
    if confirm "Install ${want[*]} with $PM${SUDO:+ (sudo)}?"; then
      pkg_install "${want[@]}" || die "Installing ${want[*]} failed."
    else
      echo "  skipped; tapesplit doctor will show what's missing."
    fi
  else
    ok "nothing to install"
  fi

  if [ "$SKIP_WHISPER" = 0 ] && [ "$OS" = Linux ] && ! whisper_ok && have cmake && have git; then
    say "Building whisper.cpp in $WHISPER_DIR"
    if [ ! -d "$WHISPER_DIR/.git" ]; then
      mkdir -p "$(dirname "$WHISPER_DIR")"
      git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$WHISPER_DIR"
    fi
    cmake -S "$WHISPER_DIR" -B "$WHISPER_DIR/build" -DCMAKE_BUILD_TYPE=Release >/dev/null
    cmake --build "$WHISPER_DIR/build" -j "$(nproc 2>/dev/null || echo 4)" --target whisper-cli >/dev/null
    mkdir -p "$HOME/.local/bin"
    ln -sf "$WHISPER_DIR/build/bin/whisper-cli" "$HOME/.local/bin/whisper-cli"
    ok "whisper-cli linked into ~/.local/bin"
  fi
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) if [ -x "$HOME/.local/bin/whisper-cli" ]; then
         export PATH="$HOME/.local/bin:$PATH"
         echo "  ${Y}note${N} add ~/.local/bin to your PATH (for example in ~/.bashrc) so tapesplit finds whisper-cli"
       fi ;;
  esac

  node_ok || echo "  ${Y}note${N} Node $MIN_NODE+ is needed for the review app; your package manager's may be older. https://nodejs.org or nvm."
fi

# ------------------------------------------------------------------ Python
say "Python environment"
PY="$(find_python)"
[ -n "$PY" ] || die "Python 3.$MIN_PY_MINOR or newer is needed. On Ubuntu 22.04, add it with the deadsnakes PPA or pyenv, then pass --python."
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV" || die "Couldn't create $VENV. On Debian and Ubuntu, install python3-venv."
fi
"$VENV/bin/python" -m pip install -q --upgrade pip

extras="local-ai,vision,visual-ai,face-ai,speaker-ai"
[ "$OS" = Darwin ] && extras="$extras,macos"
if [ "$MINIMAL" = 1 ]; then
  spec="$ROOT"
  echo "  core package only (--minimal); local AI stages will report themselves unavailable"
else
  spec="${ROOT}[${extras}]"
  echo "  extras: $extras (this downloads the local models' libraries, a few GB)"
fi
"$VENV/bin/python" -m pip install -q -e "$spec"
ok "tapesplit $("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("tapesplit"))') in $VENV"

# ------------------------------------------------------------------ .env and the model
if [ ! -f "$ROOT/.env" ] && [ -f "$ROOT/.env.example" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  ok ".env created from .env.example (every key in it is optional)"
fi

if [ "$WITH_MODEL" = 1 ]; then
  say "whisper.cpp model"
  model="$WHISPER_DIR/models/ggml-large-v3-turbo.bin"
  if [ ! -s "$model" ]; then
    mkdir -p "$(dirname "$model")"
    curl -L --fail --progress-bar -o "$model.part" "$MODEL_URL" && mv "$model.part" "$model"
  fi
  ok "$model"
  if [ -f "$ROOT/.env" ] && ! grep -q '^WHISPER_CPP_MODEL=.' "$ROOT/.env"; then
    printf '\nWHISPER_CPP_MODEL=%s\n' "$model" >> "$ROOT/.env"
    ok "WHISPER_CPP_MODEL set in .env"
  fi
elif [ -f "$ROOT/.env" ] && ! grep -q '^WHISPER_CPP_MODEL=.' "$ROOT/.env"; then
  echo "  For transcription, set WHISPER_CPP_MODEL in .env, or run again with --with-model."
fi

# ------------------------------------------------------------------ done
say "What this machine can run"
"$VENV/bin/tapesplit" doctor || true
echo
say "Next"
echo "  .venv/bin/tapesplit auto ~/Tapes --plan    # what would run"
echo "  .venv/bin/tapesplit auto ~/Tapes           # tapes in, archive out"
echo "  .venv/bin/tapesplit ui ~/Tapes.tapesplit   # browse and review"
