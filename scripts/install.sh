#!/bin/sh
# Zero Dev Telegram — one-command installer (POSIX sh).
# Centralized download base so the public URL can move without editing logic.
INSTALL_URL_BASE="${ZERO_INSTALL_BASE:-https://raw.githubusercontent.com/mhrsdev/zero-agent-dev-telegram/main}"
REPO_URL="${ZERO_REPO_URL:-https://github.com/mhrsdev/zero-agent-dev-telegram.git}"
APP_USER="zero"
APP_ROOT="/opt/zero"
DATA_HOME="/var/lib/zero"
SERVICE="zero"

set -u

log()  { printf '[install] %s\n' "$*"; }
fail() { printf '[install] ERROR: %s\n' "$*" >&2; printf '%s\n' \
"Recovery: re-run this script (it is idempotent). State: $DATA_HOME/install-state.json"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---- preflight -------------------------------------------------------------
[ "$(id -u)" = "0" ] || fail "run as root (sudo sh $0)"
ARCH=$(uname -m); case "$ARCH" in x86_64|aarch64|arm64) ;; *) fail "unsupported arch $ARCH";; esac
case "$(uname -s)" in Linux) ;; *) fail "installer supports Linux; on other OS use manual install docs";; esac

PKGMGR=""; for m in apt-get dnf yum pacman zypper apk; do have "$m" && PKGMGR="$m" && break; done
[ -n "$PKGMGR" ] || fail "no known package manager found"
log "arch=$ARCH pkgmgr=$PKGMGR"

free_kb=$(df -Pk / | awk 'NR==2{print $4}')
[ "${free_kb:-0}" -gt 1048576 ] || fail "need >=1GB free on /"
ram_kb=$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
[ "$ram_kb" -eq 0 ] || [ "$ram_kb" -gt 400000 ] || log "WARN: low RAM ($((ram_kb/1024))MB)"

mkdir -p "$DATA_HOME"
STATE="$DATA_HOME/install-state.json"
if [ -f "$APP_ROOT/venv/bin/python" ]; then
  log "existing installation detected at $APP_ROOT"
  printf '{"phase":"detected_existing","action":"upgrade"}\n' > "$STATE"
  MODE="upgrade"; MODE_NOTE="(upgrade path: venv reused, deps refreshed)"
else
  printf '{"phase":"fresh"}\n' > "$STATE"
  MODE="fresh"; MODE_NOTE=""
fi
log "mode=$MODE $MODE_NOTE"

# ---- packages --------------------------------------------------------------
case "$PKGMGR" in
  apt-get) export DEBIAN_FRONTEND=noninteractive
     apt-get update -y || fail "apt update failed"
     apt-get install -y python3 python3-venv python3-pip git curl ca-certificates \
       || fail "apt install failed" ;;
  dnf)  dnf install -y python3 python3-pip git curl || fail "dnf failed" ;;
  yum)  yum install -y python3 python3-pip git curl || fail "yum failed" ;;
  pacman) pacman -Sy --noconfirm python python-pip git curl || fail "pacman failed" ;;
  zypper) zypper --non-interactive install python3 python3-pip git curl || fail "zypper failed" ;;
  apk)  apk add --no-cache python3 py3-pip git curl || fail "apk failed" ;;
esac
have python3 || fail "python3 missing after package step"

PYVER=$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')
case "$PYVER" in 3.11|3.12|3.13) ;; *) fail "python 3.11-3.13 required, found $PYVER";; esac

# ---- dedicated user --------------------------------------------------------
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_ROOT" --shell /usr/sbin/nologin "$APP_USER" || fail "useradd failed"

# ---- fetch source ----------------------------------------------------------
printf '{"phase":"source"}\n' > "$STATE"
if [ ! -d "$APP_ROOT/src" ]; then
  git clone --depth 1 "$REPO_URL" "$APP_ROOT" || fail "clone failed"
else
  git -C "$APP_ROOT" fetch --depth 1 origin main || true
  git -C "$APP_ROOT" reset --hard origin/main || true
fi
cd "$APP_ROOT" || fail "cd failed"

# ---- venv + deps -----------------------------------------------------------
printf '{"phase":"deps"}\n' > "$STATE"
python3 -m venv "$APP_ROOT/venv" || fail "venv failed"
"$APP_ROOT/venv/bin/pip" install --upgrade pip >/dev/null || fail "pip upgrade failed"
"$APP_ROOT/venv/bin/pip" install -e . || fail "dependency install failed"

# ---- data dirs + config home ----------------------------------------------
mkdir -p "$DATA_HOME/backups" "$DATA_HOME/state"
chown -R "$APP_USER:$APP_USER" "$DATA_HOME" "$APP_ROOT"
chmod 700 "$DATA_HOME"

# ---- systemd unit ----------------------------------------------------------
printf '{"phase":"systemd"}\n' > "$STATE"
UNIT="/etc/systemd/system/$SERVICE.service"
cat > "$UNIT" <<EOF
[Unit]
Description=Zero Dev Telegram control plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_ROOT
Environment=ZERO_ENV=production
Environment=ZERO_HOME=$DATA_HOME
EnvironmentFile=-$DATA_HOME/env
ExecStart=$APP_ROOT/venv/bin/zero-develop serve --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=3
StartLimitIntervalSec=300
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
EOF
if have systemctl; then
  systemctl daemon-reload || true
  systemctl enable "$SERVICE" >/dev/null 2>&1 || true
fi

# ---- secrets bootstrap (never printed) ------------------------------------
ENVF="$DATA_HOME/env"
touch "$ENVF"; chmod 600 "$ENVF"
grep -q '^ZERO_SECRET_KEY=' "$ENVF" || {
  SK=$(head -c 48 /dev/urandom | base64 | tr -d '=+/' | cut -c1-48)
  echo "ZERO_SECRET_KEY=$SK" >> "$ENVF"
}
grep -q '^ZERO_BOOTSTRAP_TOKEN=' "$ENVF" || {
  BT=$(head -c 48 /dev/urandom | base64 | tr -d '=+/' | cut -c1-48)
  echo "ZERO_BOOTSTRAP_TOKEN=$BT" >> "$ENVF"
}
grep -q '^ZERO_DATABASE_URL=' "$ENVF" || \
  echo "ZERO_DATABASE_URL=sqlite:///$DATA_HOME/zero.db" >> "$ENVF"

# ---- migrations + health ---------------------------------------------------
printf '{"phase":"migrate"}\n' > "$STATE"
sudo_db() { sudo -u "$APP_USER" env ZERO_ENV=production ZERO_HOME="$DATA_HOME" "$@"; }
sudo_db "$APP_ROOT/venv/bin/zero-develop" migrate || fail "migrations failed"

printf '{"phase":"health"}\n' > "$STATE"
if have systemctl && systemctl start "$SERVICE"; then
  sleep 2
  curl -fsS http://127.0.0.1:8000/healthz >/dev/null || fail "healthz not ready; check: journalctl -u $SERVICE -n 50"
fi

printf '{"phase":"done","ok":true}\n' > "$STATE"
cat <<NEXT

Zero is ready.
Thank you for helping build an open, practical AI development tool.

Next step (interactive setup):
  sudo -u $APP_USER env ZERO_HOME=$DATA_HOME $APP_ROOT/venv/bin/zero setup

Useful commands:
  zero status | zero logs -n 50 | zero doctor
Admin token for first GUI login:
  grep ZERO_BOOTSTRAP_TOKEN $ENVF   # keep private
NEXT
