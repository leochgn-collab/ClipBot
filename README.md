# ClipBot

Récupère automatiquement des clips de créateurs **qui t'ont donné leur autorisation**,
les monte en vertical (fond flou + sous-titres) et les publie sur **YouTube Shorts** et **TikTok**.
Une notification ntfy est envoyée à chaque publication ou erreur.

## Installation et mises à jour depuis GitHub

```bash
apt install -y git
git clone https://github.com/TON-COMPTE/ClipBot.git /opt/clipbot-src
cd /opt/clipbot-src && bash install.sh      # première installation
bash /opt/clipbot-src/update.sh             # chaque mise à jour ensuite
```

`update.sh` récupère la dernière version, garde tes réglages, cookies, mot de passe et historique,
et ajoute seulement les nouveaux réglages.

## Interface web (http://IP-DU-CT:8080, réseau local uniquement)

Sur téléphone, l'interface passe en mode appli : barre de menu en bas, listes en cartes.
iPhone : Safari → Partager → « Sur l'écran d'accueil » (l'icône est ton logo).


- **Tableau de bord** : clips publiés, erreurs, boutons Lancer / Essai sans publier / Vérifier les connexions.
- **Historique** : chaque clip, avec Réessayer (erreur) ou Oublier.
- **Chaînes** : streamers autorisés (pseudo Twitch + vues minimum).
- **Réglages** : rythme, clés Twitch, **jeux autorisés** (ex. `Grand Theft Auto VI`), titre de secours,
  textes, **logo dans la vidéo** (envoi du PNG, position haut/bas, taille), ntfy.
- **Cookies** : fichiers cookies.txt TikTok / YouTube (importés une seule fois dans le navigateur de ClipBot).
- **Captures** et **Logs** : aperçus vidéo, captures d'erreur, journal.

Mot de passe : `runuser -u clipbot -- /opt/clipbot/venv/bin/python /opt/clipbot/web.py --set-password`
(double authentification facultative : `--setup-2fa`).

## Fonctionnement

1. **Twitch** : clips les plus vus des dernières 48 h (API officielle Helix).
   **Rediffusions Twitch** : plusieurs moments forts par live, trouvés de trois façons :
   passages repris par des **chaînes de clippers YouTube** (retrouvés dans le live par le son, prioritaires),
   passages clippés par les viewers, et pics de son. Un passage déjà publié n'est jamais repris.
   **YouTube** : moments les plus revus des vidéos récentes (heatmap « Most replayed »).
2. Téléchargement avec yt-dlp (seulement l'extrait utile pour YouTube).
3. Montage ffmpeg 1080×1920, 30 i/s, ≤ 59 s, sous-titres auto (faster-whisper).
4. Publication YouTube (Data API v3) et TikTok (Content Posting API).
5. Base SQLite : un clip n'est jamais posté deux fois ; limites par passage et par 24 h.

## Installation (conteneur Debian sur Proxmox)

Prévois un CT Debian 13 avec 2 cœurs, 4 Go de RAM (Whisper) et 15 Go de disque.

```bash
cd clipbot
bash install.sh
nano /opt/clipbot/config.yaml
```

## Mode navigateur (publication publique sans audit) — par défaut

ClipBot ouvre un vrai Chromium (dans un écran virtuel Xvfb) connecté à **ton** compte,
puis publie depuis TikTok Studio et YouTube Studio comme si tu le faisais à la main.

⚠️ C'est contraire aux conditions d'utilisation de TikTok et YouTube : risque de blocage
du compte, et ça peut casser quand ils modifient leur site. Garde un rythme raisonnable
(`max_posts_per_day`), évite d'y lier un compte que tu ne veux surtout pas perdre.

1. Sur ton PC, dans Chrome, connecte-toi à **tiktok.com** et **youtube.com** (bonne chaîne).
2. Fais-le dans une **fenêtre de navigation privée** (autorise l'extension **Get cookies.txt LOCALLY** en navigation privée),
   exporte les cookies, puis **ferme la fenêtre sans te déconnecter** : la session ne servira qu'à ClipBot.
3. Copie-les dans `/opt/clipbot/secrets/` puis `chown clipbot:clipbot /opt/clipbot/secrets/*`.
4. Vérifie la connexion :
   ```bash
   cd /opt/clipbot
   runuser -u clipbot -- env PLAYWRIGHT_BROWSERS_PATH=/opt/clipbot/ms-playwright \
     xvfb-run -a venv/bin/python clipbot.py --check-browser
   ```
   Les captures d'écran sont dans `screenshots/`.
5. En cas d'erreur pendant une publication, une capture est enregistrée dans `screenshots/`
   et tu reçois une notification ntfy. Si TikTok/YouTube changent un bouton, modifie le
   sélecteur dans `publish.browser.selectors` de la config.
6. Les cookies finissent par expirer (quelques semaines à quelques mois) : réexporte-les
   quand `--check-browser` affiche ❌. Ne les partage jamais, ils donnent accès à tes comptes.

Avec `method: browser`, les sections Twitch (source) restent nécessaires, mais tu n'as
pas besoin des clés API YouTube / TikTok ci-dessous.

## Clés à créer (une seule fois, mode `api` uniquement pour YouTube/TikTok)

### Twitch
dev.twitch.tv/console/apps → nouvelle appli → récupère `client_id` / `client_secret`.

### YouTube
1. console.cloud.google.com → nouveau projet → active **YouTube Data API v3**.
2. Écran de consentement OAuth : type *Externe*, ajoute ton compte, puis **passe-le en « Production »**.
   En mode « Test », le jeton expire au bout de 7 jours.
3. Identifiants → *ID client OAuth* → type **Application de bureau** → télécharge `client_secret.json`.
4. `runuser -u clipbot -- /opt/clipbot/venv/bin/python /opt/clipbot/auth_youtube.py client_secret.json`
   (ouvre d'abord un tunnel depuis ton PC : `ssh -L 8765:localhost:8765 root@IP_DU_CT`).
5. ⚠️ Tant que ton projet Google n'a pas passé l'**audit de l'API YouTube**, les vidéos envoyées
   par l'API restent **privées**. Demande-le via le formulaire « YouTube API Services – Audit and Quota Extension ».

### TikTok
1. developers.tiktok.com → crée une appli → ajoute **Login Kit** et **Content Posting API**
   (active « Direct Post » si tu veux le mode `direct`).
2. Scopes : `user.info.basic`, `video.upload`, `video.publish`.
3. Déclare un *Redirect URI* en HTTPS que tu contrôles (ex. `https://ton-domaine.fr/tiktok-callback`).
4. `runuser -u clipbot -- /opt/clipbot/venv/bin/python /opt/clipbot/auth_tiktok.py`
5. Modes de publication :
   - `inbox` (par défaut) : la vidéo arrive en **brouillon** dans l'appli TikTok, tu publies d'un tap.
   - `direct` : publication directe, mais **en privé** tant que ton appli n'a pas passé l'audit TikTok.

## Test puis automatisation

```bash
cd /opt/clipbot
runuser -u clipbot -- venv/bin/python clipbot.py --dry-run -v   # vidéos dans preview/, rien n'est publié
systemctl enable --now clipbot.timer                       # 12h, 17h, 19h, 21h
journalctl -u clipbot -f                                   # logs
```

## Bon à savoir
- Garde une preuve écrite de chaque autorisation (message, mail, contrat de clipping).
- La description crédite automatiquement le créateur avec un lien vers sa chaîne.
- Si YouTube bloque yt-dlp (« Sign in to confirm you're not a bot »), exporte un `cookies.txt`
  et renseigne `ytdlp_cookies` dans la config.
- Mets à jour yt-dlp régulièrement : `/opt/clipbot/venv/bin/pip install -U yt-dlp`.
