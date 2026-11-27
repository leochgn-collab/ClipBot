#!/usr/bin/env bash
# Met à jour ClipBot depuis le dépôt GitHub.
#   Première fois :  apt install -y git
#                    git clone https://github.com/TON-COMPTE/ClipBot.git /opt/clipbot-src
#                    bash /opt/clipbot-src/update.sh
#   Ensuite :        bash /opt/clipbot-src/update.sh
#   Depuis un zip :  unzip ClipBot-depot.zip && bash ClipBot-depot/update.sh
# Tes réglages (config.yaml), cookies, mot de passe et historique ne sont jamais touchés.
set -euo pipefail
DIR=/opt/clipbot
SRC="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" = 0 ] || { echo "Lance ce script en root."; exit 1; }
[ -d "$DIR/venv" ] || { echo "ClipBot n'est pas installé : lance d'abord bash $SRC/install.sh"; exit 1; }

if [ -d "$SRC/.git" ]; then
  echo "→ Récupération de la dernière version…"
  git -C "$SRC" pull --ff-only
fi

echo "→ Copie des fichiers…"
cp "$SRC"/{clipbot.py,analyse.py,web.py,browser_upload.py,run-request.sh,auth_youtube.py,auth_tiktok.py,requirements.txt,config.example.yaml,README.md} "$DIR"/
cp "$SRC"/systemd/*.service "$SRC"/systemd/*.timer "$SRC"/systemd/*.path /etc/systemd/system/
mkdir -p "$DIR"/{assets/fonts,trigger,logs,screenshots,preview,work,secrets}

echo "→ Dépendances Python…"
"$DIR/venv/bin/pip" install -q --only-binary=av -r "$DIR/requirements.txt"
# YouTube change souvent : toujours la dernière version, avec ses composants JavaScript (yt-dlp-ejs)
echo "  (yt-dlp et Deno : 1 à 5 min la première fois, c'est normal)"
"$DIR/venv/bin/pip" install --progress-bar on --upgrade "yt-dlp[default]" | grep -E "^(Collecting|Successfully)" || true
# Deno (moteur JavaScript) : obligatoire pour télécharger depuis YouTube depuis fin 2025
"$DIR/venv/bin/pip" install --upgrade deno 2>/dev/null | grep -E "^(Collecting|Downloading|Successfully)" || true
if [ ! -x "$DIR/venv/bin/deno" ] && ! command -v deno >/dev/null 2>&1; then
  echo "Installation de Deno…"
  apt-get install -y -qq unzip curl >/dev/null 2>&1 || true
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y </dev/null >/dev/null 2>&1 || true
fi
if [ -x "$DIR/venv/bin/deno" ]; then echo "Deno : $("$DIR/venv/bin/deno" --version | head -1)";
elif command -v deno >/dev/null 2>&1; then echo "Deno : $(deno --version | head -1)";
else echo "⚠️  Deno non installé : les téléchargements YouTube risquent d'échouer"; fi

if [ ! -s "$DIR/assets/fonts/RubikMonoOne-Regular.ttf" ]; then
  curl -fsSL -o "$DIR/assets/fonts/RubikMonoOne-Regular.ttf" \
    https://github.com/google/fonts/raw/main/ofl/rubikmonoone/RubikMonoOne-Regular.ttf \
    || echo "⚠️ Police non téléchargée : police de secours utilisée."
fi

echo "→ Nouveaux réglages (les tiens sont conservés)…"
"$DIR/venv/bin/python" - <<'PY'
import yaml, shutil
p = "/opt/clipbot/config.yaml"
c = yaml.safe_load(open(p, encoding="utf-8"))
ex = yaml.safe_load(open("/opt/clipbot/config.example.yaml", encoding="utf-8"))
def merge(dst, src):               # ajoute seulement les réglages absents
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            merge(dst[k], v)
merge(c, ex)
v = c.setdefault("video", {})
if v.get("quality") == "haute" and not v.get("quality_choisie"):
    v["quality"] = "max"            # meilleure qualité par défaut (tant que tu n'as pas choisi toi-même)
shutil.copy(p, p + ".avant-maj")
open(p, "w", encoding="utf-8").write(yaml.safe_dump(c, allow_unicode=True, sort_keys=False))
PY

echo "→ Droits et services…"
chown -R clipbot:clipbot "$DIR"
chown -R root:root "$DIR"/venv "$DIR"/*.py "$DIR"/run-request.sh "$DIR"/requirements.txt "$DIR"/config.example.yaml
[ -d "$DIR/ms-playwright" ] && chown -R root:root "$DIR/ms-playwright"
chmod 644 "$DIR"/*.py "$DIR"/run-request.sh
chmod 700 "$DIR/secrets"; chmod 600 "$DIR"/config.yaml*
systemctl daemon-reload
systemctl enable --now clipbot-run.path clipbot.timer clipbot-web
systemctl restart clipbot-web clipbot.timer
echo "✅ ClipBot est à jour."
