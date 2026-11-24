#!/usr/bin/env python3
"""À lancer UNE fois : autorise ClipBot à publier sur ton compte TikTok.

    python auth_tiktok.py [config.yaml]

1. Ouvre l'URL affichée et connecte-toi à TikTok.
2. TikTok te redirige vers ton redirect_uri (la page peut être une 404, ce n'est pas grave).
3. Copie l'URL complète de la barre d'adresse et colle-la ici.
"""
import json
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import yaml

base = Path(__file__).resolve().parent
cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else base / "config.yaml"
t = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["publish"]["tiktok"]

state = secrets.token_urlsafe(16)
params = {"client_key": t["client_key"], "response_type": "code", "state": state,
          "scope": "user.info.basic,video.publish,video.upload",
          "redirect_uri": t["redirect_uri"]}
print("\nOuvre cette URL :\n")
print("https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params) + "\n")

q = parse_qs(urlparse(input("URL de redirection : ").strip()).query)
if q.get("state", [""])[0] != state:
    sys.exit("❌ Paramètre state invalide, recommence.")
if "code" not in q:
    sys.exit(f"❌ Pas de code dans l'URL : {q}")

r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", timeout=30,
                  headers={"Content-Type": "application/x-www-form-urlencoded"},
                  data={"client_key": t["client_key"], "client_secret": t["client_secret"],
                        "code": q["code"][0], "grant_type": "authorization_code",
                        "redirect_uri": t["redirect_uri"]})
data = r.json()
if "refresh_token" not in data:
    sys.exit(f"❌ Échec : {data}")

out = base / t["token_file"]
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(data))
out.chmod(0o600)
print(f"✅ Jeton TikTok enregistré dans {out}")
