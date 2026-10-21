#!/usr/bin/env bash
# Встановлення Polysim на сервер Ubuntu 22.04/24.04 / Debian 12.
# Запуск (з розпакованої папки polysim, від root):
#     sudo bash deploy/setup.sh
# Повторний запуск оновлює код, не чіпаючи зібраних даних і пароля.
set -euo pipefail

APP_DIR=/opt/polysim
DATA_DIR=/var/lib/polysim
ENV_FILE=/etc/polysim.env
PORT="${PORT:-8080}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Запусти через sudo"; exit 1; }
[ -f "$SRC/polysim/__main__.py" ] || { echo "Не знайдено код у $SRC"; exit 1; }

echo "==> Пакети"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv sqlite3 >/dev/null

echo "==> Користувач і папки"
id polysim >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin polysim
mkdir -p "$APP_DIR" "$DATA_DIR"
rm -rf "$APP_DIR/polysim"
cp -r "$SRC/polysim" "$SRC/requirements.txt" "$SRC/README.md" "$APP_DIR/"
chown -R polysim:polysim "$DATA_DIR"

echo "==> Python-оточення"
[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> Пароль до сторінки"
if [ ! -f "$ENV_FILE" ]; then
  PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  printf 'POLYSIM_PASSWORD=%s\n' "$PASS" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  NEW_PASS=1
else
  PASS="$(sed -n 's/^POLYSIM_PASSWORD=//p' "$ENV_FILE")"
  NEW_PASS=0
fi

echo "==> Перевірка доступу до даних Polymarket"
cd "$DATA_DIR"
if ! sudo -u polysim env PYTHONPATH="$APP_DIR" "$APP_DIR/venv/bin/python" -m polysim --db "$DATA_DIR/bets.sqlite" check; then
  echo
  echo "!! З цього сервера дані Polymarket недоступні. Встановлення зупинено."
  exit 2
fi

echo "==> Служби"
cat > /etc/systemd/system/polysim-bot.service <<EOF
[Unit]
Description=Polysim: збір даних і віртуальні ставки
After=network-online.target
Wants=network-online.target

[Service]
User=polysim
WorkingDirectory=$DATA_DIR
ExecStart=$APP_DIR/venv/bin/python -m polysim --db $DATA_DIR/bets.sqlite run --every 15
Restart=always
RestartSec=30
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$APP_DIR

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/polysim-web.service <<EOF
[Unit]
Description=Polysim: сторінка зі звітом
After=network-online.target

[Service]
User=polysim
WorkingDirectory=$DATA_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONPATH=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -m polysim --db $DATA_DIR/bets.sqlite serve --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable -q polysim-bot polysim-web
systemctl restart polysim-bot polysim-web

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" >/dev/null
fi

IP="$(curl -4 -s --max-time 5 https://ifconfig.me || hostname -I | awk '{print $1}')"
sleep 3
echo
echo "================================================================"
echo " Готово! Бот збирає дані кожні 15 хвилин."
echo
echo " Звіт:    http://$IP:$PORT/"
echo " Логін:   admin"
if [ "$NEW_PASS" = 1 ]; then
echo " Пароль:  $PASS      <- збережи його!"
else
echo " Пароль:  без змін (подивитись: sudo cat $ENV_FILE)"
fi
echo
echo " Журнал бота:   journalctl -u polysim-bot -f"
echo " Зупинити:      systemctl stop polysim-bot"
echo "================================================================"
systemctl --no-pager --lines=0 status polysim-bot polysim-web | grep -E "Active:|●" || true
