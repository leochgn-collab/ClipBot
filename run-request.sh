#!/usr/bin/env bash
# Lancé par clipbot-run.path quand l'interface web dépose une demande.
REQ=/opt/clipbot/trigger/request
MODE=$(cat "$REQ" 2>/dev/null); rm -f "$REQ"
case "$MODE" in
  one:*) ID="${MODE#one:}"
         # identifiant de clip : lettres, chiffres, : _ - uniquement
         if [[ "$ID" =~ ^[A-Za-z0-9:_-]{1,120}$ ]]; then ARGS=(--only "$ID"); else exit 1; fi ;;
  dry)   ARGS=(--dry-run -v) ;;
  check) ARGS=(--check-browser) ;;
  tiktokqr) ARGS=(--tiktok-qr) ;;
  *)     ARGS=(--force) ;;   # bouton « Lancer maintenant »
esac
exec >>/opt/clipbot/logs/dernier_lancement.log 2>&1
exec xvfb-run -a /opt/clipbot/venv/bin/python /opt/clipbot/clipbot.py -c /opt/clipbot/config.yaml "${ARGS[@]}"
