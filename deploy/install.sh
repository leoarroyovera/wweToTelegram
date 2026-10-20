#!/usr/bin/env bash
# Instala wweToTelegram en un VPS Linux (Ubuntu/Debian u Oracle Linux) como
# servicio systemd. Correr desde la carpeta del proyecto, con el usuario
# normal (no root):   bash deploy/install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
cd "$DIR"

echo "==> Paquetes del sistema"
if command -v apt-get >/dev/null; then
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-venv python3-pip
elif command -v dnf >/dev/null; then
    sudo dnf install -y python3 python3-pip
fi

echo "==> Entorno virtual (.venv)"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
# Opcional: cifrado en C, acelera bastante las subidas de Telethon.
.venv/bin/pip install cryptg || echo "   (cryptg no disponible; se sigue sin el)"

echo "==> Comprobaciones"
falta=0
[ -f .env ] || { echo "   FALTA .env (copia el de tu PC)"; falta=1; }
if [ -f .env ]; then
    for v in TG_API_ID TG_API_HASH WWE_BOT_TOKEN WWE_BOT_ADMINS; do
        grep -Eq "^$v=.+" .env || { echo "   FALTA $v en .env"; falta=1; }
    done
fi
[ -f wwe_session.session ] || {
    echo "   FALTA wwe_session.session: copiala de tu PC o haz el login:"
    echo "       .venv/bin/python wwe_telethon.py --login"; falta=1; }
[ -f wwe_seen.sqlite3 ] || {
    echo "   AVISO: no hay wwe_seen.sqlite3. Sin el, el script no sabe lo ya"
    echo "   publicado y volveria a subir galerias y superstars. Copialo de tu PC."; falta=1; }

echo "==> Servicio systemd"
sed -e "s#__USER__#$USER_NAME#g" -e "s#__DIR__#$DIR#g" deploy/wwe-bot.service \
    | sudo tee /etc/systemd/system/wwe-bot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable wwe-bot.service

if [ "$falta" = 1 ]; then
    echo
    echo "Servicio instalado pero NO arrancado: completa lo que falta y luego"
    echo "    sudo systemctl start wwe-bot"
    exit 1
fi
sudo systemctl restart wwe-bot.service
sleep 3
systemctl --no-pager status wwe-bot.service | head -n 12
echo
echo "Listo. Logs:  journalctl -u wwe-bot -f   |   tail -f wwe_telethon.log"
