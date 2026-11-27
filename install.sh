#!/usr/bin/env bash
# Installation de ClipBot sur Debian 12/13 (conteneur Proxmox), en root.
set -euo pipefail

DIR=/opt/clipbot
apt-get update
apt-get install -y ffmpeg python3-venv python3-pip fonts-dejavu-core unzip curl xvfb

# yt-dlp a besoin d'un moteur JavaScript (Deno) pour YouTube
if ! command -v deno >/dev/null; then
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y
fi

id clipbot >/dev/null 2>&1 || useradd --system --home "$DIR" --shell /usr/sbin/nologin clipbot
mkdir -p "$DIR/secrets"
cp clipbot.py analyse.py browser_upload.py web.py run-request.sh auth_youtube.py auth_tiktok.py requirements.txt config.example.yaml "$DIR"/
mkdir -p "$DIR"/{assets,trigger}
[ -f "$DIR/config.yaml" ] || cp "$DIR/config.example.yaml" "$DIR/config.yaml"

python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --upgrade pip
# av : version précompilée obligatoire (la compiler demande des outils absents)
"$DIR/venv/bin/pip" install --only-binary=av -r "$DIR/requirements.txt"
"$DIR/venv/bin/pip" install --upgrade "yt-dlp[default]" deno || true
PLAYWRIGHT_BROWSERS_PATH="$DIR/ms-playwright" "$DIR/venv/bin/python" -m playwright install --with-deps chromium

cp systemd/clipbot.service systemd/clipbot.timer systemd/clipbot-web.service systemd/clipbot-run.service systemd/clipbot-run.path /etc/systemd/system/
# Droits : le code et Python appartiennent à root (le service ne peut pas les modifier),
# les données appartiennent à clipbot, les secrets ne sont lisibles que par lui.
mkdir -p "$DIR"/{logs,screenshots,preview,work}
chown -R clipbot:clipbot "$DIR"
chown -R root:root "$DIR"/venv "$DIR"/ms-playwright "$DIR"/*.py "$DIR"/run-request.sh "$DIR"/requirements.txt "$DIR"/config.example.yaml
chmod 644 "$DIR"/*.py "$DIR"/run-request.sh
chmod 700 "$DIR/secrets"
chmod 600 "$DIR/config.yaml"
systemctl daemon-reload
systemctl enable clipbot-web.service clipbot-run.path
systemctl start clipbot-run.path

echo
echo "✅ Installé dans $DIR"
echo "1) Mot de passe de l'interface : runuser -u clipbot -- $DIR/venv/bin/python $DIR/web.py --set-password"
echo "   puis : systemctl start clipbot-web  →  http://$(hostname -I | awk '{print $1}'):8080"
echo "2) Dans l'interface : Réglages (clés Twitch), Chaînes, Cookies, logo"
echo "3) Essai : bouton « Essai sans publier » dans l'interface"
echo "4) Automatique : systemctl enable --now clipbot.timer"
