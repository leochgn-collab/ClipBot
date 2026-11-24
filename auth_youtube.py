#!/usr/bin/env python3
"""À lancer UNE fois : autorise ClipBot à publier sur ta chaîne YouTube.

    python auth_youtube.py client_secret.json

Sur le serveur (sans navigateur), ouvre d'abord un tunnel depuis ton PC :
    ssh -L 8765:localhost:8765 root@IP_DU_CONTENEUR
puis ouvre dans ton navigateur l'URL affichée.
"""
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
out = Path(__file__).resolve().parent / "secrets" / "youtube_token.json"

if len(sys.argv) < 2:
    sys.exit(__doc__)

flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
creds = flow.run_local_server(host="localhost", port=8765, open_browser=False,
                              access_type="offline", prompt="consent")
out.parent.mkdir(exist_ok=True)
out.write_text(creds.to_json())
out.chmod(0o600)
print(f"✅ Jeton YouTube enregistré dans {out}")
