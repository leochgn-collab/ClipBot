#!/usr/bin/env python3
"""ClipBot : récupère des clips de créateurs AUTORISÉS (Twitch / YouTube),
les monte en vertical 9:16 (fond flou + sous-titres) et les publie
automatiquement sur YouTube Shorts et TikTok. Notifications via ntfy.

Usage :
    python clipbot.py -c config.yaml            # passage normal
    python clipbot.py -c config.yaml --dry-run  # monte les vidéos sans publier
"""

import argparse
import json
import logging
import math
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
import yt_dlp

log = logging.getLogger("clipbot")
BASE = Path(__file__).resolve().parent
MB = 1024 * 1024


# --------------------------------------------------------------------------
# Config et état
# --------------------------------------------------------------------------
def load_config(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve(p):
    p = Path(p)
    return p if p.is_absolute() else BASE / p


class State:
    """Base SQLite : évite de reposter deux fois le même clip."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS clips (
                source_id TEXT PRIMARY KEY, platform TEXT, channel TEXT,
                title TEXT, status TEXT, youtube_id TEXT, tiktok_id TEXT,
                tries INTEGER DEFAULT 0, error TEXT, updated TEXT)"""
        )
        try:
            self.db.execute("ALTER TABLE clips ADD COLUMN kind TEXT")   # type de passage (stats)
        except sqlite3.OperationalError:
            pass
        self.db.commit()

    def get(self, sid):
        row = self.db.execute("SELECT * FROM clips WHERE source_id=?", (sid,)).fetchone()
        return dict(row) if row else None

    def is_done(self, sid, max_tries):
        row = self.get(sid)
        return bool(row) and (row["status"] == "done" or row["tries"] >= max_tries)

    def save(self, c, **fields):
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO clips (source_id, platform, channel, title, updated, kind)
               VALUES (?,?,?,?,?,?) ON CONFLICT(source_id) DO NOTHING""",
            (c["source_id"], c["platform"], c["channel"], c["title"], now, c.get("kind")),
        )
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(
                f"UPDATE clips SET {cols}, updated=? WHERE source_id=?",
                (*fields.values(), now, c["source_id"]),
            )
        self.db.commit()

    def fail(self, c, error):
        self.save(c)
        self.db.execute(
            "UPDATE clips SET status='error', tries=tries+1, error=? WHERE source_id=?",
            (error[:500], c["source_id"]),
        )
        self.db.commit()

    def last_post_time(self):
        row = self.db.execute("SELECT MAX(updated) FROM clips WHERE status='done'").fetchone()
        if not row or not row[0]:
            return None
        t = datetime.fromisoformat(row[0])
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    def last_post_by_channel(self):
        return {r[0].lower(): r[1] for r in self.db.execute(
            "SELECT channel, MAX(updated) FROM clips WHERE status='done' GROUP BY channel") if r[0]}

    def posted_last_24h(self):
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        return self.db.execute(
            "SELECT COUNT(*) FROM clips WHERE status='done' AND updated>=?", (since,)
        ).fetchone()[0]


# --------------------------------------------------------------------------
# Notifications ntfy
# --------------------------------------------------------------------------
def notify(cfg, title, message, priority=3, tags=None, click=None):
    n = cfg.get("ntfy") or {}
    if not n.get("url") or not n.get("topic"):
        return
    headers = {}
    if n.get("token"):
        headers["Authorization"] = "Bearer " + n["token"]
    try:
        requests.post(
            (n["url"] if "://" in n["url"] else "https://" + n["url"]).rstrip("/"),
            json={"topic": n["topic"], "title": title, "message": message,
                  "priority": priority, "tags": tags or [], **({"click": click} if click else {})},
            headers=headers, timeout=15,
        )
    except requests.RequestException as e:
        log.warning("ntfy injoignable : %s", e)


# --------------------------------------------------------------------------
# Sources : Twitch
# --------------------------------------------------------------------------
class Twitch:
    API = "https://api.twitch.tv/helix"

    def __init__(self, client_id, client_secret):
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={"client_id": client_id, "client_secret": client_secret,
                  "grant_type": "client_credentials"},
            timeout=30,
        )
        r.raise_for_status()
        self.headers = {"Client-ID": client_id,
                        "Authorization": "Bearer " + r.json()["access_token"]}

    def _get(self, path, **params):
        r = requests.get(self.API + path, headers=self.headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()["data"]

    def game_ids(self, names):
        """Noms de jeux Twitch -> identifiants (ex. « Grand Theft Auto V » -> 32982)."""
        ids = {}
        for i in range(0, len(names), 100):
            r = requests.get(self.API + "/games", headers=self.headers,
                             params=[("name", n) for n in names[i:i + 100]], timeout=30)
            r.raise_for_status()
            ids.update({g["id"]: g["name"] for g in r.json()["data"]})
        missing = set(names) - set(ids.values())
        if missing:
            log.warning("Jeux introuvables sur Twitch (vérifie l'orthographe) : %s", ", ".join(missing))
        return ids

    def user_id(self, login):
        if not hasattr(self, "_uids"):
            self._uids = {}
        if login not in self._uids:
            users = self._get("/users", login=login)
            self._uids[login] = users[0]["id"] if users else None
        return self._uids[login]

    def videos(self, login, first=3):
        """Rediffusions (VOD) récentes de la chaîne."""
        uid = self.user_id(login)
        return self._get("/videos", user_id=uid, type="archive", first=first) if uid else []

    def clips(self, login, since, until):
        uid = self.user_id(login)
        if not uid:
            log.warning("Twitch : chaîne introuvable '%s'", login)
            return []
        return self._get(
            "/clips", broadcaster_id=uid, first=50,
            started_at=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ended_at=until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )


def twitch_candidates(cfg, state=None, scan_vods=True):
    src = cfg["sources"].get("twitch") or {}
    if not src.get("enabled"):
        return []
    max_d = cfg["video"]["max_duration"]
    now = datetime.now(timezone.utc)
    # L'API Twitch limite la fenêtre à 7 jours
    since = now - timedelta(hours=min(src.get("lookback_hours", 48), 168))
    tw = Twitch(src["client_id"], src["client_secret"])
    games = [g.strip() for g in src.get("games") or [] if str(g).strip()]
    allowed_games = tw.game_ids(games) if games else None
    if allowed_games is not None:
        log.info("Filtre jeux : %s", ", ".join(allowed_games.values()) or "aucun jeu reconnu")
    out = []
    per_channel, all_vods = [], {}
    vods_on = (src.get("vods") or {}).get("enabled")
    if vods_on and scan_vods:
        clean_vodcache(cfg)
    for ch in src.get("channels", []):
        login = ch["login"] if isinstance(ch, dict) else ch
        min_views = (ch.get("min_views") if isinstance(ch, dict) else None) or src.get("min_views", 0)
        try:
            clips = tw.clips(login, since, now)
        except requests.RequestException as e:
            log.error("Twitch %s : %s", login, e)
            continue
        vods = {}
        if vods_on:
            try:
                vods = collect_vods(cfg, tw, login, scan_vods)
            except Exception as e:  # une rediffusion illisible ne bloque pas les clips
                log.error("Rediffusions de %s : %s", login, e)
            rediffs = ch.get("rediffs") if isinstance(ch, dict) else None
            if not vods and not rediffs:  # pas de lien donné : on cherche la chaîne de rediffs tout seul
                try:
                    rediffs = discover_rediffs(cfg, login, scan_vods)
                except Exception as e:
                    log.warning("Recherche des rediffs de %s : %s", login, e)
            if rediffs and rediffs != "non" and not vods:      # rien de lisible sur Twitch -> rediffs YouTube
                try:
                    vods = youtube_rediffs(cfg, rediffs, login, scan_vods)
                except Exception as e:
                    log.error("Rediffs YouTube de %s : %s", login, e)
        all_vods.update(vods)
        per_channel.append((login, min_views, clips, vods))
    # Les comptes de clippers sont comparés à TOUS les lives (Michou, Squeezie, Inoxtag…) en une fois
    moments = {}
    _deep_budget["left"] = int(((src.get("vods") or {}).get("deep_max_per_run", 12)))
    if all_vods:
        try:
            moments = clipper_moments(cfg, all_vods, scan_vods)
        except Exception as e:
            log.error("Clippers : %s", e)
    model = load_model(cfg) if all_vods else None
    for login, min_views, clips, vods in per_channel:
        eligible = []
        for cl in clips:
            if cl["view_count"] < min_views or cl["duration"] > max_d + 5:
                continue
            if allowed_games is not None and cl.get("game_id") not in allowed_games:
                log.debug("Ignoré (hors jeux choisis) : %s", cl["title"])
                continue
            eligible.append(cl)
        if vods:
            try:
                out += vod_candidates(cfg, login, vods, clips, state, moments, model, scan_vods,
                                      own_clips={cl["id"] for cl in eligible})
            except Exception as e:
                log.error("Moments forts de %s : %s", login, e)
        for cl in eligible:
            out.append({
                "source_id": "tw:" + cl["id"], "platform": "twitch", "kind": "viewer",
                "channel": cl["broadcaster_name"], "title": cl["title"],
                "url": cl["url"], "views": cl["view_count"],
                "credit_url": f"https://twitch.tv/{login}",
                "start": None, "end": None, "rank": round(2 + min(1.0, math.log10(cl["view_count"] + 1) / 5), 3),
            })
    log.info("Twitch : %d clip(s) candidat(s)", len(out))
    return out


# --------------------------------------------------------------------------
# Sources : rediffusions Twitch (moments forts détectés au son)
# --------------------------------------------------------------------------
def _twitch_duration(text):
    """« 4h3m10s » -> secondes."""
    return sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in re.findall(r"(\d+)([hms])", text or ""))


def vod_analysis(cfg, vod_id, vod_url):
    """Analyse du son d'une rediffusion (volume + empreinte), gardée en cache."""
    import numpy as np
    import analyse
    cache = resolve(cfg.get("work_dir", "work")) / "vodcache"
    lv, ft = cache / f"{vod_id}.json", cache / f"{vod_id}.s60.npy"
    if lv.exists() and ft.exists():
        return json.loads(lv.read_text()), np.load(ft).astype(np.float32)
    log.info("Analyse du son de la rediffusion %s (quelques minutes la première fois)…", vod_id)
    progress("Analyse d'un live", vod_id)
    t0 = time.time()
    if vod_id.startswith("yt-") and youtube_blocked(cfg):
        raise RuntimeError("YouTube en pause (vérification anti-robot), réessai plus tard")
    if vod_id.startswith("yt-"):
        # YouTube bride la lecture directe (≈ temps réel : 4 h pour un live de 4 h).
        # On télécharge d'abord le son seul en petite qualité (≈ 20 Mo/h), par morceaux, puis on l'analyse.
        tmp = resolve(cfg.get("work_dir", "work")) / "vodtmp"
        tmp.mkdir(parents=True, exist_ok=True)
        for f in tmp.glob(f"{vod_id}.*"):
            f.unlink(missing_ok=True)
        opts = {**ydl_base_opts(cfg), "format": "139/249/250/worstaudio/bestaudio",
                "outtmpl": str(tmp / f"{vod_id}.%(ext)s"), "http_chunk_size": 10 * 1024 * 1024,
                "concurrent_fragment_downloads": 4, "retries": 10, "fragment_retries": 10}
        try:
            try:
                with yt_dlp.YoutubeDL(opts) as y:
                    y.extract_info(vod_url, download=True)
            except Exception as e:
                if is_bot_error(e):
                    mark_youtube_blocked(cfg)
                raise
            files = [f for f in tmp.glob(f"{vod_id}.*") if not f.name.endswith((".part", ".ytdl"))]
            if not files:
                raise RuntimeError("son du live non téléchargé")
            log.info("Son du live téléchargé en %d s (%.0f Mo), analyse…", time.time() - t0,
                     files[0].stat().st_size / 1e6)
            levels, feats = analyse.features(str(files[0]))
        finally:
            for f in tmp.glob(f"{vod_id}.*"):
                f.unlink(missing_ok=True)
    else:
        with yt_dlp.YoutubeDL({**ydl_base_opts(cfg), "format": "Audio_Only/audio_only/bestaudio/worst"}) as y:
            info = y.extract_info(vod_url, download=False)
        stream = info.get("url") or info["requested_formats"][0]["url"]
        levels, feats = analyse.features(
            stream, lambda n: log.info("… %d min de live analysées (%d s écoulées)", n // 60, time.time() - t0))
    log.info("Rediffusion %s analysée : %d min de live en %d s", vod_id, len(levels) // 60, time.time() - t0)
    cache.mkdir(parents=True, exist_ok=True)
    lv.write_text(json.dumps(levels))
    np.save(ft, feats.astype(np.float16))
    return levels, feats


def vod_chat(cfg, vod_id, vod_url, scan=True):
    """Activité du chat par seconde, si on peut la connaître :
    1) replay du chat YouTube (live rediffusé avec son chat) ;
    2) sinon, chat incrusté dans l'image de la rediff (détecté en regardant la vidéo en 144p).
    Renvoie un tableau numpy ou None. Gardé en cache."""
    import numpy as np
    import analyse
    cache = resolve(cfg.get("work_dir", "work")) / "vodcache"
    f, none = cache / f"{vod_id}.chat.npy", cache / f"{vod_id}.sanschat"
    if f.exists():
        return np.load(f)
    if none.exists() or not scan or not vod_id.startswith("yt-") or youtube_blocked(cfg):
        return None                     # Twitch : chat non incrusté et replay non accessible simplement
    tmp = resolve(cfg.get("work_dir", "work")) / "vodtmp"
    tmp.mkdir(parents=True, exist_ok=True)
    act = None
    try:
        opts = {**ydl_base_opts(cfg), "skip_download": True, "writesubtitles": True,
                "subtitleslangs": ["live_chat"], "outtmpl": str(tmp / f"{vod_id}.%(ext)s")}
        with yt_dlp.YoutubeDL(opts) as y:
            y.extract_info(vod_url, download=True)
        chats = list(tmp.glob(f"{vod_id}*.live_chat.json"))
        if chats:
            msgs = analyse.parse_youtube_live_chat(chats[0])
            if len(msgs) > 50:
                act = analyse.chat_from_messages(msgs, int(max(t for t, _ in msgs)) + 1)
                log.info("Chat : replay trouvé pour %s (%d messages)", vod_id, len(msgs))
    except Exception as e:
        log.debug("Pas de replay du chat pour %s : %s", vod_id, e)
    if act is None:
        t0 = time.time()
        opts = {**ydl_base_opts(cfg), "format": "160/278/394/worstvideo[height>=100]/worstvideo",
                "outtmpl": str(tmp / f"{vod_id}.video.%(ext)s"), "http_chunk_size": 10 * 1024 * 1024,
                "retries": 10, "fragment_retries": 10}
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                y.extract_info(vod_url, download=True)
            vids = [x for x in tmp.glob(f"{vod_id}.video.*") if not x.name.endswith((".part", ".ytdl"))]
            if vids:
                act = analyse.video_chat_activity(str(vids[0]))
                log.info("Chat : %s pour %s (%d s)", "chat incrusté détecté dans la vidéo" if act is not None
                         else "pas de chat dans la vidéo", vod_id, time.time() - t0)
        except Exception as e:
            if is_bot_error(e):
                mark_youtube_blocked(cfg)
                return None            # pas de marque « sans chat » : on réessaiera après la pause
            log.warning("Chat incrusté de %s non analysé : %s", vod_id, str(e)[:200])
    for x in tmp.glob(f"{vod_id}*"):
        x.unlink(missing_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    if act is None:
        none.touch()
    else:
        np.save(f, act.astype(np.float32))
    return act


def clean_vodcache(cfg, keep_days=10):
    """Supprime les analyses de lives trop vieux (les exemples d'apprentissage, eux, sont gardés)."""
    cache = resolve(cfg.get("work_dir", "work")) / "vodcache"
    if not cache.exists():
        return
    limit = time.time() - keep_days * 86400
    for f in cache.iterdir():
        old_format = f.suffix == ".npy" and not f.name.endswith(".s60.npy")
        if old_format or (not f.name.endswith(".train.npz") and f.stat().st_mtime < limit):
            f.unlink(missing_ok=True)


def _clipper_url(url):
    url = url.strip().rstrip("/")
    if "tiktok.com" in url:
        return url
    if "/shorts" not in url and "/videos" not in url:
        url += "/shorts"
    return url


def clipper_moments(cfg, vods_by_id, scan=True):
    """Shorts / TikTok récents des comptes de clippers -> moments retrouvés dans les lives.
    Renvoie {vod_id: [(seconde, durée, vues)]}. Chaque vidéo n'est analysée qu'une fois (cache)."""
    import analyse
    ccfg = (cfg.get("sources") or {}).get("clippers") or {}
    if not ccfg.get("enabled") or not ccfg.get("channels") or not vods_by_id:
        return {}
    cache_file = resolve(cfg.get("work_dir", "work")) / "clippercache.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    found = {}
    for entry in cache.values():
        if entry.get("vod") in vods_by_id:
            found.setdefault(entry["vod"], []).append((entry["offset"], entry["duration"], entry.get("views", 0)))
    if not scan or youtube_blocked(cfg):
        return found
    max_age = timedelta(hours=ccfg.get("lookback_hours", 72))
    now = datetime.now(timezone.utc)
    work = resolve(cfg.get("work_dir", "work")) / "clippers"
    work.mkdir(parents=True, exist_ok=True)
    import numpy as np
    index = {vid: analyse.VodIndex(v["feats"]) for vid, v in vods_by_id.items()}
    created = {vid: datetime.fromisoformat(v["created_at"].replace("Z", "+00:00")) for vid, v in vods_by_id.items()}
    fdir = resolve(cfg.get("work_dir", "work")) / "clipperfeats"
    fdir.mkdir(parents=True, exist_ok=True)
    stats = [0, 0]

    def eligible(ts):
        return {vid: ix for vid, ix in index.items()
                if not ts or datetime.fromtimestamp(ts, timezone.utc) >= created[vid]}

    def record(sid, entry, res, clip_len, ts, checked):
        if res:
            stats[0] += 1
            dur = int(clip_len * res["rate"])
            entry.update(vod=res["vod"], offset=res["start"], duration=min(dur, 90), score=res["score"],
                         rate=res["rate"], streamer=vods_by_id.get(res["vod"], {}).get("login"))
            entry.pop("checked", None)
            (fdir / f"{sid}.npy").unlink(missing_ok=True)
            log.info("Clipper : « %s » retrouvé dans le live %s à %ds (vitesse ×%s, score %.2f)",
                     entry.get("title", sid)[:60], res["vod"], res["start"], res["rate"], res["score"])
            found.setdefault(res["vod"], []).append((res["start"], entry["duration"], entry.get("views", 0)))
        else:
            entry["checked"] = sorted(set(entry.get("checked", [])) | set(checked))
        cache[sid] = entry

    # Vidéos déjà vues mais pas retrouvées : on réessaie seulement sur les lives nouvellement analysés
    for sid, entry in list(cache.items()):
        if entry.get("vod") or entry.get("old") or not (fdir / f"{sid}.npy").exists():
            continue
        ts = entry.get("ts")
        if ts and now - datetime.fromtimestamp(ts, timezone.utc) > max_age:
            entry["old"] = True
            (fdir / f"{sid}.npy").unlink(missing_ok=True)
            continue
        new = {vid: ix for vid, ix in eligible(ts).items() if vid not in entry.get("checked", [])}
        if new:
            clip_feats = np.load(fdir / f"{sid}.npy").astype(np.float32)
            record(sid, entry, analyse.locate(clip_feats, new), entry.get("len", 30), ts, new)
    for url in ccfg["channels"]:
        url = _clipper_url(url)
        try:
            with yt_dlp.YoutubeDL({**ydl_base_opts(cfg), "extract_flat": True,
                                   "playlistend": int(ccfg.get("per_channel", 10))}) as y:
                listing = y.extract_info(url, download=False)
        except Exception as e:
            log.warning("Clippers : %s illisible (%s)", url, e)
            continue
        for e in listing.get("entries") or []:
            vid_url = e.get("url") or e.get("webpage_url")
            sid = e.get("id")
            if not sid or sid in cache:
                continue
            if not vid_url or not vid_url.startswith("http"):
                vid_url = f"https://www.youtube.com/watch?v={sid}"
            try:
                with yt_dlp.YoutubeDL({**ydl_base_opts(cfg), "format": "bestaudio/best",
                                       "outtmpl": str(work / f"{sid}.%(ext)s")}) as y:
                    info = y.extract_info(vid_url, download=False)
                    ts = info.get("timestamp")
                    if ts and now - datetime.fromtimestamp(ts, timezone.utc) > max_age:
                        # trop vieux pour retrouver le live, mais utile pour les tendances (ce qui marche)
                        cache[sid] = {"vod": None, "old": True, "title": info.get("title", ""), "ts": ts,
                                      "len": info.get("duration"), "views": info.get("view_count") or 0,
                                      "likes": info.get("like_count") or 0, "seen_t": time.time(),
                                      "clipper": info.get("uploader") or info.get("channel") or url}
                        continue
                    y.process_info(info)
                files = list(work.glob(f"{sid}.*"))
                if not files:
                    continue
                progress("Recherche des passages des clippers", (info.get("title") or sid)[:70])
                _, clip_feats = analyse.features(str(files[0]))
                cands = eligible(ts)          # seulement les lives commencés avant la vidéo
                res = analyse.locate(clip_feats, cands) if cands else None
                stats[1] += 1
                clip_len = info.get("duration") or len(clip_feats) / analyse.FPS
                entry = {"vod": None, "title": info.get("title", ""), "from": vid_url, "ts": ts,
                         "len": clip_len, "views": info.get("view_count") or 0,
                         "likes": info.get("like_count") or 0, "seen_t": time.time(),
                         "clipper": info.get("uploader") or info.get("channel") or url}
                if not res:   # gardée (70 Ko) pour réessayer quand d'autres lives seront analysés
                    np.save(fdir / f"{sid}.npy", clip_feats.astype(np.float16))
                record(sid, entry, res, clip_len, ts, cands)
            except Exception as ex:
                log.warning("Clippers : vidéo %s ignorée (%s)", sid, ex)
            finally:
                for f in work.glob(f"{sid}.*"):
                    f.unlink(missing_ok=True)
    if stats[1]:
        log.info("Clippers : %d vidéo(s) analysée(s), %d retrouvée(s) dans les lives", stats[1], stats[0])
    cache_file.write_text(json.dumps(cache))
    try:
        viral = social_trends(cfg, cache).get("viral") or {}
    except Exception as e:
        log.warning("Tendances non calculées : %s", e)
        viral = {}
    if viral:
        # « vues utiles » : une vidéo qui fait 3x mieux que d'habitude pour SON clipper compte 3x plus
        found = {}
        for sid, e in cache.items():
            if e.get("vod") in vods_by_id:
                boost = 10 ** max(-1.0, min(1.0, viral.get(sid, 0.0)))
                found.setdefault(e["vod"], []).append((e["offset"], e["duration"],
                                                       int((e.get("views") or 0) * boost)))
    return found


# --------------------------------------------------------------------------
# Tendances : ce qui marche chez les clippers (YouTube Shorts / TikTok)
# --------------------------------------------------------------------------
def _title_traits(t):
    letters = [ch for ch in t if ch.isalpha()]
    caps = sum(ch.isupper() for ch in letters) / max(1, len(letters))
    return {
        "point d'exclamation « ! »": "!" in t,
        "question « ? »": "?" in t,
        "en MAJUSCULES": caps > 0.6,
        "avec emoji": any(ord(ch) > 0x2600 for ch in t),
        "titre court (moins de 40 caractères)": len(t) < 40,
        "avec un chiffre": any(ch.isdigit() for ch in t),
        "avec des guillemets (phrase dite)": any(q in t for q in ('"', "«", "“")),
    }


def social_trends(cfg, cache=None):
    """Analyse les vidéos des clippers (vues corrigées de l'âge, comparées à la moyenne de chaque clipper)
    et en déduit ce qui marche : durée, style de titre, mots, streamers. Sauvé dans tendances.json."""
    import numpy as np
    work = resolve(cfg.get("work_dir", "work"))
    if cache is None:
        f = work / "clippercache.json"
        cache = json.loads(f.read_text()) if f.exists() else {}
    rows = []
    for sid, e in cache.items():
        if not e.get("views") or not e.get("ts") or not e.get("title"):
            continue
        age_h = max(1.0, ((e.get("seen_t") or time.time()) - e["ts"]) / 3600)
        rows.append({"id": sid, "title": e["title"], "views": int(e["views"]), "likes": int(e.get("likes") or 0),
                     "len": e.get("len") or 0, "clipper": e.get("clipper") or "?", "streamer": e.get("streamer"),
                     "from": e.get("from") or "",
                     "raw": float(np.log10(e["views"] + 1) - 0.4 * np.log10(age_h / 24 + 1))})
    res = {"t": time.time(), "n": len(rows), "tables": {}, "top": [], "best_len": None}
    if len(rows) >= 5:
        # chaque clipper a son public : on compare une vidéo à la médiane de SON clipper
        by_c = {}
        for r in rows:
            by_c.setdefault(r["clipper"], []).append(r["raw"])
        med = {c: float(np.median(v)) for c, v in by_c.items()}
        for r in rows:
            r["perf"] = r["raw"] - med[r["clipper"]] if len(by_c[r["clipper"]]) >= 3 else r["raw"] - float(
                np.median([x["raw"] for x in rows]))

        def table(groups):
            out = []
            for name, rs in groups.items():
                if len(rs) < 2:
                    continue
                n = len(rs)
                eff = float(np.mean([r["perf"] for r in rs])) * n / (n + 3)
                out.append({"name": name, "n": n, "effect": round(eff, 3),
                            "views": int(np.median([r["views"] for r in rs]))})
            return sorted(out, key=lambda x: -x["effect"])

        def bucket(L):
            return ("moins de 20 s" if L < 20 else "20-30 s" if L < 30 else "30-45 s" if L < 45
                    else "45-60 s" if L <= 60 else "plus d'1 min")
        dur = {}
        for r in rows:
            if r["len"]:
                dur.setdefault(bucket(r["len"]), []).append(r)
        res["tables"]["Durée"] = table(dur)
        traits = {}
        for r in rows:
            for k, on in _title_traits(r["title"]).items():
                traits.setdefault(k + (" — oui" if on else " — non"), []).append(r)
        res["tables"]["Style de titre"] = [x for x in table(traits) if x["name"].endswith("oui")]
        words = {}
        for r in rows:
            for w in set(re.findall(r"[a-zà-ÿ0-9']{4,}", r["title"].lower())):
                words.setdefault(w, []).append(r)
        res["tables"]["Mots qui reviennent dans les titres"] = [x for x in table(
            {w: rs for w, rs in words.items() if len(rs) >= 3})][:15]
        st = {}
        for r in rows:
            if r["streamer"]:
                st.setdefault(r["streamer"], []).append(r)
        res["tables"]["Streamer"] = table(st)
        res["tables"]["Clipper"] = table({c: [r for r in rows if r["clipper"] == c] for c in by_c})
        res["top"] = [{k: r[k] for k in ("title", "views", "likes", "len", "clipper", "from")}
                      for r in sorted(rows, key=lambda r: -r["perf"])[:10]]
        # durée qui marche le mieux (si assez de données et un écart net)
        d = [x for x in res["tables"]["Durée"] if x["n"] >= 5]
        if len(d) >= 2 and d[0]["effect"] - d[-1]["effect"] > 0.1:
            res["best_len"] = {"moins de 20 s": 25, "20-30 s": 30, "30-45 s": 45, "45-60 s": 59}.get(d[0]["name"])
        res["viral"] = {r["id"]: round(r["perf"], 3) for r in rows}
    (work / "tendances.json").write_text(json.dumps(res, ensure_ascii=False))
    if rows:
        log.info("Tendances : %d vidéo(s) de clippers analysée(s)%s", len(rows),
                 f", durée qui marche le mieux : {res['tables']['Durée'][0]['name']}"
                 if res["tables"].get("Durée") else "")
    return res


def load_trends(cfg):
    f = resolve(cfg.get("work_dir", "work")) / "tendances.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------
# Apprentissage des moments forts
# --------------------------------------------------------------------------
MODEL_MIN_EXAMPLES = 12


def model_file(cfg):
    return resolve(cfg.get("work_dir", "work")) / "highlight_model.json"


def load_model(cfg):
    f = model_file(cfg)
    if not f.exists():
        return None
    m = json.loads(f.read_text())
    return m if m.get("n_pos", 0) >= MODEL_MIN_EXAMPLES else None


def update_training(cfg, vid, P, positives, length):
    """Range les exemples de ce live (passages clippés = moments forts) et réentraîne le modèle si besoin."""
    import numpy as np
    import analyse
    cache = resolve(cfg.get("work_dir", "work")) / "vodcache"
    f = cache / f"{vid}.train.npz"
    pos = sorted({(int(a), int(b)) for a, b in positives})
    if f.exists():
        old = np.load(f)
        if sorted(map(tuple, old["pos"].tolist())) == pos:
            return
    if not pos:
        return
    X, y = analyse.training_rows(P, pos, length)
    if not len(X):
        return
    np.savez(f, X=X.astype(np.float32), y=y.astype(np.int8), pos=np.array(pos))
    Xs, ys, n_pos, n_lives = [], [], 0, 0
    for g in cache.glob("*.train.npz"):
        d = np.load(g)
        Xs.append(d["X"]); ys.append(d["y"]); n_pos += len(d["pos"]); n_lives += 1
    model = analyse.train(np.vstack(Xs), np.concatenate(ys))
    model.update(n_pos=n_pos, n_lives=n_lives, date=datetime.now(timezone.utc).isoformat(timespec="minutes"),
                 names=[f"{n} (moy.)" for n in analyse.NAMES] + [f"{n} (max)" for n in analyse.NAMES])
    model_file(cfg).write_text(json.dumps(model))
    log.info("Moments forts : modèle réentraîné (%d passages clippés, %d live(s))%s", n_pos, n_lives,
             "" if n_pos >= MODEL_MIN_EXAMPLES else f" — utilisé à partir de {MODEL_MIN_EXAMPLES}")


def pick_with_model(model, P, length, count, blocked=(), min_gap=120, boosts=(), min_score=0.5, chat=None):
    import analyse
    starts, sc = analyse.scores(model, P, length)
    sc = sc.copy()
    if chat is not None:              # le chat s'emballe : bonus jusqu'à +0,4
        for k, s0 in enumerate(starts):
            sc[k] += 0.4 * float(chat[s0:s0 + length].mean()) if s0 < len(chat) else 0
    for k, s in enumerate(starts):
        if any(s - 15 <= b <= s + length for b in boosts):
            sc[k] += 0.2
    taken = [(max(0, a - min_gap), b + min_gap) for a, b in blocked]
    picks = []
    for k in sc.argsort()[::-1]:
        if len(picks) >= count or sc[k] < min_score:
            break
        t = starts[k]
        if any(a < t + length and t < b for a, b in taken):
            continue
        picks.append((t, t + length, float(sc[k])))
        taken.append((t - min_gap, t + length + min_gap))
    return sorted(picks)


def pick_highlights(levels, length, count, blocked=(), min_gap=120, boosts=(), step=5, min_excess=3.0, chat=None):
    """Les `count` passages de `length` s les plus « forts » (son au-dessus du niveau habituel du live),
    sans chevaucher les passages déjà pris (`blocked`) ni être à moins de `min_gap` s l'un de l'autre.
    `boosts` : instants où les viewers ont créé des clips (bonus)."""
    n = len(levels)
    if n < length:
        return []
    med = sorted(levels)[n // 2]
    score = [max(0.0, v - med) for v in levels]
    for b in boosts:
        for t in range(max(0, int(b)), min(n, int(b) + 30)):
            score[t] += 8.0
    if chat is not None:              # chat qui s'emballe : jusqu'à +6 dB équivalents par seconde
        for t in range(min(n, len(chat))):
            score[t] += 6.0 * float(chat[t])
    pref = [0.0]
    for v in score:
        pref.append(pref[-1] + v)
    taken = [(max(0, a - min_gap), b + min_gap) for a, b in blocked]
    picks = []
    windows = sorted(((pref[t + length] - pref[t], t) for t in range(0, n - length, step)), reverse=True)
    for total, t in windows:
        # en moyenne au moins `min_excess` dB au-dessus du niveau habituel (sinon : passage calme)
        if len(picks) >= count or total < min_excess * length:
            break
        if any(a < t + length and t < b for a, b in taken):
            continue
        picks.append((t, t + length, total / length / 20.0))   # force ≈ dB au-dessus du niveau habituel / 20
        taken.append((t - min_gap, t + length + min_gap))
    return sorted(picks)


def collect_vods(cfg, tw, login, scan=True):
    """Rediffusions récentes d'une chaîne, analysées (son + empreinte)."""
    vcfg = cfg["sources"]["twitch"].get("vods") or {}
    keywords = [k.lower() for k in vcfg.get("title_keywords") or [] if k]
    max_age = timedelta(hours=vcfg.get("lookback_hours", 72))
    now = datetime.now(timezone.utc)
    work = resolve(cfg.get("work_dir", "work"))
    vods = {}
    for v in tw.videos(login, first=int(vcfg.get("max_vods", 2))):
        created = datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))
        if now - created > max_age:
            continue
        if keywords and not any(k in v["title"].lower() for k in keywords):
            log.debug("Rediffusion ignorée (titre) : %s", v["title"])
            continue
        if not scan and not (work / "vodcache" / f"{v['id']}.s60.npy").exists():
            continue
        marker = work / "vodcache" / f"{v['id']}.abonnes"
        if marker.exists() and time.time() - marker.stat().st_mtime < 86400:
            continue                      # déjà vu « réservée aux abonnés » il y a moins de 24 h
        try:
            levels, feats = vod_analysis(cfg, v["id"], v["url"])
        except Exception as e:
            if "subscriber-only" in str(e) or "logged into an account" in str(e):
                log.warning("Rediffusion %s de %s réservée aux abonnés : ignorée (cookies Twitch d'un compte "
                            "abonné nécessaires pour l'analyser)", v["id"], login)
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
            else:
                log.error("Rediffusion %s illisible : %s", v["id"], e)
            continue
        vods[v["id"]] = {**v, "levels": levels, "feats": feats, "login": login}
    return vods


def _norm(text):
    import unicodedata
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)


def discover_rediffs(cfg, login, scan=True):
    """Trouve tout seul la chaîne YouTube où un streamer republie ses lives complets.
    Cherche sur YouTube « <pseudo> rediffusion live »… et garde la chaîne dont le nom contient le pseudo
    et qui publie des vidéos longues (lives). Résultat gardé 7 jours (ou pour toujours s'il est trouvé)."""
    f = resolve(cfg.get("work_dir", "work")) / "rediffs_auto.json"
    data = json.loads(f.read_text()) if f.exists() else {}
    e = data.get(login)
    if e and (e.get("url") or time.time() - e.get("t", 0) < 7 * 86400):
        return e.get("url")
    if not scan:
        return None
    key = _norm(login)
    cands = {}
    for q in (f"{login} rediffusion live", f"{login} rediff live complet", f"{login} live twitch vod"):
        try:
            with yt_dlp.YoutubeDL({**ydl_base_opts(cfg), "extract_flat": True}) as y:
                res = y.extract_info(f"ytsearch20:{q}", download=False)
        except Exception as ex:
            log.warning("Recherche YouTube « %s » impossible : %s", q, ex)
            continue
        for v in res.get("entries") or []:
            name, cid = v.get("channel") or v.get("uploader") or "", v.get("channel_id")
            if not cid or key not in _norm(name):
                continue
            c = cands.setdefault(cid, {"name": name, "long": 0,
                                       "kw": bool(re.search(r"redif|replay|vod|live|stream", _norm(name)))})
            if (v.get("duration") or 0) >= 2700:
                c["long"] += 1
    good = [(c["kw"], c["long"], cid) for cid, c in cands.items() if c["long"] >= 2]
    url = name = None
    if good:
        cid = max(good)[2]
        url, name = f"https://www.youtube.com/channel/{cid}", cands[cid]["name"]
        log.info("Rediffs YouTube trouvées automatiquement pour %s : « %s » (%s)", login, name, url)
    else:
        log.info("Pas de chaîne YouTube de rediffs trouvée pour %s (nouvel essai dans 7 jours)", login)
    data[login] = {"url": url, "name": name, "t": time.time()}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, ensure_ascii=False))
    return url


_age_warned = set()


def _age_warning(cfg, login, yid):
    """Rediff limitée aux adultes (GTA…) : YouTube exige un compte connecté et majeur."""
    if cfg.get("ytdlp_cookies") and resolve(cfg["ytdlp_cookies"]).exists():
        log.warning("Rediff %s de %s réservée aux adultes et refusée malgré les cookies de téléchargement "
                    "(compte pas vérifié majeur ? cookies expirés ?)", yid, login)
    elif login not in _age_warned:
        log.warning("Rediffs de %s réservées aux adultes (contenu GTA) : ajoute des cookies dans "
                    "Plus → Cookies → « Téléchargements YouTube » (compte Google majeur, pas RedifStream). "
                    "En attendant, ces rediffs sont ignorées.", login)
    _age_warned.add(login)


def youtube_rediffs(cfg, url, login, scan=True):
    """Lives complets republiés sur une chaîne YouTube de rediffusions (ex. @MichouRediffs)."""
    if scan and youtube_blocked(cfg):
        scan = False              # YouTube en pause : seulement les lives déjà analysés
    vcfg = cfg["sources"]["twitch"].get("vods") or {}
    keywords = [k.lower() for k in vcfg.get("title_keywords") or [] if k]
    max_age = timedelta(hours=max(vcfg.get("lookback_hours", 72), 168))   # rediff mise en ligne après le live
    now = datetime.now(timezone.utc)
    work = resolve(cfg.get("work_dir", "work"))
    url = url.strip().rstrip("/")
    if not url.endswith(("/videos", "/streams")):
        url += "/videos"
    want = int(vcfg.get("max_vods", 2))
    with yt_dlp.YoutubeDL({**ydl_base_opts(cfg), "extract_flat": True, "playlistend": want * 4}) as y:
        listing = y.extract_info(url, download=False)
    vods = {}
    for e in listing.get("entries") or []:
        if len(vods) >= want:
            break
        yid, title = e.get("id"), e.get("title") or ""
        if not yid or (e.get("duration") and e["duration"] < 1800):
            continue                                       # trop court pour être un live complet
        if keywords and not any(k in title.lower() for k in keywords):
            log.debug("Rediff YouTube ignorée (titre) : %s", title)
            continue
        vid = "yt-" + yid
        watch = f"https://www.youtube.com/watch?v={yid}"
        meta_file = work / "vodcache" / f"{vid}.meta.json"
        info = json.loads(meta_file.read_text()) if meta_file.exists() else None
        # « Moments les plus revus » de YouTube : relevés à nouveau toutes les 12 h (ils apparaissent avec les vues)
        stale = info is not None and not info.get("heatmap") and time.time() - info.get("heatmap_t", 0) > 12 * 3600
        if info is None or (scan and stale):
            if not scan:
                continue
            try:
                with yt_dlp.YoutubeDL(ydl_base_opts(cfg)) as y:
                    full = y.extract_info(watch, download=False)
            except Exception as ex:
                if "confirm your age" in str(ex) or "inappropriate for some users" in str(ex):
                    _age_warning(cfg, login, yid)
                    continue
                if is_bot_error(ex):
                    mark_youtube_blocked(cfg)
                    break
                log.warning("Rediff YouTube %s ignorée : %s", yid, str(ex)[:150])
                continue
            info = {"timestamp": full.get("release_timestamp") or full.get("timestamp"),
                    "duration": full.get("duration") or 0, "title": full.get("title") or title,
                    "heatmap": [(h["start_time"], h["end_time"], h["value"]) for h in full.get("heatmap") or []],
                    "heatmap_t": time.time()}
            meta_file.parent.mkdir(parents=True, exist_ok=True)
            meta_file.write_text(json.dumps(info))
            if info["heatmap"]:
                log.info("Rediff %s : courbe « moments les plus revus » de YouTube récupérée", yid)
        if (info.get("duration") or 0) < 1800 or not info.get("timestamp"):
            continue
        published = datetime.fromtimestamp(info["timestamp"], timezone.utc)
        if now - published > max_age:
            continue
        if not scan and not (work / "vodcache" / f"{vid}.s60.npy").exists():
            continue
        try:
            levels, feats = vod_analysis(cfg, vid, watch)
        except Exception as ex:
            log.error("Rediff YouTube %s illisible : %s", yid, ex)
            continue
        # le live a eu lieu avant la mise en ligne : on recule la date pour accepter les Shorts des clippers
        created = (published - timedelta(days=3)).isoformat()
        vods[vid] = {"id": vid, "title": info["title"], "url": watch, "user_name": login,
                     "created_at": created, "levels": levels, "feats": feats, "login": login,
                     "heatmap": info.get("heatmap") or [], "published": published.isoformat()}
    if vods:
        log.info("Rediffs YouTube de %s : %d live(s) utilisable(s)", login, len(vods))
    return vods


def vod_candidates(cfg, login, vods, clips, state, moments, model, scan=True, own_clips=frozenset()):
    """Extraits pris dans les rediffusions d'une chaîne, en plus des clips Twitch :
    1) les moments repris par les comptes de clippers (YouTube/TikTok), retrouvés dans le live ;
    2) les moments forts (modèle appris, ou pics de son), autour des passages clippés par les viewers."""
    vcfg = cfg["sources"]["twitch"].get("vods") or {}
    length = int(cfg["video"]["max_duration"])
    trends = load_trends(cfg) if vcfg.get("follow_trends", True) else {}
    hl_len = min(length, trends.get("best_len") or length)   # durée qui marche chez les clippers
    out = []
    for vid, v in vods.items():
        # Passages déjà publiés : extraits de ce live + clips Twitch publiés de ce live
        blocked = []
        if state is not None:
            for sid, st in state.db.execute(
                    "SELECT source_id, status FROM clips WHERE source_id LIKE ?", (f"vod:{vid}:%",)):
                if st == "done":
                    start = int(float(sid.rsplit(":", 1)[1]))
                    blocked.append((start, start + length))
        boosts = []
        for cl in clips:
            if cl.get("video_id") == vid and cl.get("vod_offset") is not None:
                row = state.get("tw:" + cl["id"]) if state is not None else None
                if cl["id"] in own_clips or (row and row["status"] == "done"):
                    # ce clip est (ou sera) publié tel quel : on ne refait pas le même moment
                    blocked.append((cl["vod_offset"], cl["vod_offset"] + int(cl["duration"])))
                else:
                    boosts.append(cl["vod_offset"])

        try:
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(
                (v.get("published") or v["created_at"]).replace("Z", "+00:00"))).total_seconds() / 86400
        except (KeyError, ValueError):
            age_days = 0
        fresh = 0.15 * min(1.0, max(0.0, age_days) / 7)     # jusqu'à -0,15 pour un live d'une semaine

        def add(start, end, views, rank):
            rank -= fresh
            out.append({"source_id": f"vod:{vid}:{int(round(start))}", "platform": "twitch-vod",
                        "kind": "clipper" if views > 0 else "moment",
                        "channel": v["user_name"], "title": v["title"], "url": v["url"],
                        "views": views, "credit_url": f"https://twitch.tv/{login}",
                        "start": start, "end": end, "rank": round(rank, 3)})

        import analyse
        vc = analyse.voice_curve(v["feats"])      # pour couper dans les silences, pas en pleine phrase
        # 1) Moments repris par les clippers (prioritaires : déjà validés par le public)
        ms = moments.get(vid, [])

        def agree(o, d):          # combien de clippers ont repris ce même passage
            return sum(1 for o2, d2, _ in ms if o2 < o + d and o < o2 + d2)
        # tri : passage repris par plusieurs clippers d'abord, puis le plus vu
        for offset, dur, views in sorted(ms, key=lambda m: (-agree(m[0], m[1]), -m[2])):
            end = offset + min(max(dur, 10), length)
            if any(a < end and offset < b for a, b in blocked):
                continue
            n = agree(offset, dur)
            s0, e0 = analyse.snap(offset, end, v["feats"], min(10, end - offset), length, vc)
            add(s0, e0, max(int(views), 1), 3 + min(0.7, math.log10(int(views) + 1) / 10) + min(0.29, 0.1 * (n - 1)))
            blocked.append((offset, end))
        # Exemples pour l'apprentissage : passages repris par les clippers et par les viewers
        P = analyse.per_second(v["levels"], v["feats"])
        positives = [(o, o + max(d, 10)) for o, d, _ in moments.get(vid, [])]
        positives += [(cl["vod_offset"], cl["vod_offset"] + int(cl["duration"]))
                      for cl in clips if cl.get("video_id") == vid and cl.get("vod_offset") is not None]
        if scan and len(P):
            try:
                update_training(cfg, vid, P, positives, length)
            except Exception as e:
                log.warning("Apprentissage ignoré pour %s : %s", vid, e)
        # 2) Moments forts : modèle appris si assez d'exemples, sinon pics de son
        count, gap = int(vcfg.get("highlights_per_vod", 6)), int(vcfg.get("min_gap_seconds", 120))
        chat = None
        if vcfg.get("chat", True) and len(P):
            try:
                act = vod_chat(cfg, vid, v["url"], scan)
                if act is not None:
                    chat = analyse.chat_bonus(act, len(P))
            except Exception as e:
                log.warning("Chat de %s ignoré : %s", vid, e)
        if v.get("heatmap") and len(P):
            replay = analyse.heatmap_bonus(v["heatmap"], len(P))
            chat = replay if chat is None else 1 - (1 - chat) * (1 - replay)   # public : chat OU revisionnages
        if model and len(P):
            picks = pick_with_model(model, P, hl_len, count, blocked, gap, boosts, chat=chat)
        else:
            picks = pick_highlights(v["levels"], hl_len, count, blocked, gap, boosts, chat=chat)
        # 3) Découpage intelligent : le cœur de l'action + un peu de contexte, coupé dans un silence
        ex = analyse.excitement(P, model) if len(P) else None
        if ex is not None and chat is not None:
            ex = ex + 2 * (ex.std() + 1e-6) * chat[:len(ex)] if len(chat) >= len(ex) else ex
        for start, end, strength in picks:
            if ex is not None and len(ex) > end:
                start, end = analyse.refine(start, end, v["feats"], ex, min_len=min(20, hl_len),
                                            max_len=hl_len, v=vc)
            rank = 1 + min(0.99, strength)
            # 4) Écoute approfondie : ce qui est dit (réactions, rires) + découpe sur les phrases
            if vcfg.get("deep_listen", True) and scan:
                try:
                    d = deep_listen(cfg, vid, v["url"], start, end, hl_len)
                except Exception as e:
                    if is_bot_error(e):
                        mark_youtube_blocked(cfg)     # on arrête d'insister : pause YouTube
                    else:
                        log.warning("Écoute approfondie ignorée (%s à %ds) : %s", vid, start, str(e)[:200])
                    d = None
                if d:
                    start, end = d["s"], d["e"]
                    rank = 1 + min(0.99, 0.5 * min(1.0, strength) + 0.49 * d["score"])
            elif vcfg.get("deep_listen", True):
                d = deep_cached(cfg, vid, start, end)
                if d:
                    start, end = d["s"], d["e"]
                    rank = 1 + min(0.99, 0.5 * min(1.0, strength) + 0.49 * d["score"])
            add(start, end, 0, rank)
    return out


_deep_budget = {"left": 0}


def _deep_file(cfg):
    return resolve(cfg.get("work_dir", "work")) / "deepcache.json"


def deep_cached(cfg, vid, start, end):
    f = _deep_file(cfg)
    if not f.exists():
        return None
    return json.loads(f.read_text()).get(f"{vid}:{int(start)}:{int(end)}")


def deep_listen(cfg, vid, url, start, end, max_len):
    """Télécharge le passage (+5 s autour), le transcrit avec Whisper et note ce qui est dit.
    Limité à `deep_max_per_run` passages par lancement ; résultat gardé en cache."""
    import analyse
    key = f"{vid}:{int(start)}:{int(end)}"
    f = _deep_file(cfg)
    cache = json.loads(f.read_text()) if f.exists() else {}
    if key in cache:
        return cache[key]
    if _deep_budget["left"] <= 0 or (vid.startswith("yt-") and youtube_blocked(cfg)):
        return None
    model = _whisper(cfg)
    if model is None:
        return None
    _deep_budget["left"] -= 1
    progress("Écoute approfondie", f"{vid} à {int(start) // 60} min")
    a, b = max(0.0, start - 5), end + 5
    tmp = resolve(cfg.get("work_dir", "work")) / "deeptmp"
    tmp.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w-]", "_", key)
    fmt = "140/139/bestaudio" if vid.startswith("yt-") else "Audio_Only/audio_only/bestaudio/worst"
    opts = {**ydl_base_opts(cfg), "format": fmt, "outtmpl": str(tmp / f"{name}.%(ext)s"),
            "download_ranges": yt_dlp.utils.download_range_func(None, [(a, b)])}
    t0 = time.time()
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.extract_info(url, download=True)
        files = [x for x in tmp.glob(f"{name}.*") if not x.name.endswith((".part", ".ytdl"))]
        if not files:
            return None
        sub = cfg["video"].get("subtitles") or {}
        segs, _ = model.transcribe(str(files[0]), language=sub.get("language", "fr"), vad_filter=True,
                                   word_timestamps=True)
        words = [(w.start, w.end, w.word) for sg in segs for w in (sg.words or [])]
    finally:
        for x in tmp.glob(f"{name}.*"):
            x.unlink(missing_ok=True)
    inside = [w for w in words if start - a <= w[0] <= end - a]
    # débit habituel du streamer sur ce live (médiane des passages déjà écoutés)
    prev = [e.get("wps") for k, e in cache.items() if k.startswith(vid + ":") and e.get("wps")]
    base = float(sorted(prev)[len(prev) // 2]) if len(prev) >= 3 else None
    score, det = analyse.speech_score(inside, end - start, base)
    s2, e2 = analyse.snap_words(words, start - a, end - a, max_len, min_len=min(15, end - start))
    res = {"score": score, "s": round(a + s2, 1), "e": round(a + e2, 1), "wps": det.get("mots/s"),
           "texte": " ".join(w for _, _, w in words)[:160]}
    log.info("Écoute approfondie %s à %ds : note %.2f %s (%d s) « %s… »", vid, start, score, det,
             time.time() - t0, res["texte"][:70])
    cache[key] = res
    f.write_text(json.dumps(cache, ensure_ascii=False))
    return res


def _whisper(cfg):
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    if _whisper_model is None:
        sub = cfg["video"].get("subtitles") or {}
        _whisper_model = WhisperModel(sub.get("model", "small"), device="cpu", compute_type="int8")
    return _whisper_model


# --------------------------------------------------------------------------
# Sources : YouTube (moments les plus revus via la heatmap)
# --------------------------------------------------------------------------
BOT_MARKERS = ("confirm you're not a bot", "confirm you’re not a bot", "Sign in to confirm")


def youtube_blocked(cfg):
    """YouTube a demandé « confirme que tu n'es pas un robot » récemment : on le laisse tranquille un moment."""
    f = resolve(cfg.get("work_dir", "work")) / "youtube_bloque"
    return f.exists() and f.stat().st_mtime > time.time()


def youtube_block_until(cfg):
    f = resolve(cfg.get("work_dir", "work")) / "youtube_bloque"
    try:
        return datetime.fromtimestamp(f.stat().st_mtime).strftime("%H:%M")
    except OSError:
        return "?"


def same_moment(a, b, tol=60):
    """Deux identifiants désignent-ils le même moment ? (la découpe peut bouger de quelques secondes)"""
    if a == b:
        return True
    if not (a.startswith("vod:") and b.startswith("vod:")):
        return False
    va, _, sa = a.rpartition(":")
    vb, _, sb = b.rpartition(":")
    try:
        return va == vb and abs(int(sa) - int(sb)) <= tol
    except ValueError:
        return False


def mark_youtube_blocked(cfg, hours=2):
    f = resolve(cfg.get("work_dir", "work")) / "youtube_bloque"
    first = not youtube_blocked(cfg)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.touch()
    t = time.time() + hours * 3600
    os.utime(f, (t, t))
    if first:
        log.warning("YouTube demande de confirmer qu'on n'est pas un robot : pause YouTube de %d h.", hours)
        notify(cfg, "⚠️ ClipBot : YouTube bloque les téléchargements",
               f"YouTube demande de confirmer que ce n'est pas un robot. ClipBot laisse YouTube tranquille "
               f"{hours} h et continue avec Twitch. Si ça revient souvent : Réglages → cookies de téléchargement.",
               4, ["warning"])


def is_bot_error(e):
    return any(m.lower() in str(e).lower() for m in BOT_MARKERS)


def ydl_base_opts(cfg):
    opts = {"quiet": True, "no_warnings": True, "noprogress": True,
            # discret : petites pauses entre les requêtes (moins de risque de blocage YouTube)
            "sleep_interval_requests": 0.75}
    cookies = cfg.get("ytdlp_cookies")
    if cookies:
        opts["cookiefile"] = str(resolve(cookies))
    return opts


def best_windows(heatmap, duration, length, count):
    """Trouve les `count` fenêtres de `length` s les plus revues (sans chevauchement)."""
    secs = int(duration)
    if secs <= length:
        return [(0, secs)]
    score = [0.0] * secs
    for seg in heatmap:
        for s in range(int(seg["start_time"]), min(int(seg["end_time"]) + 1, secs)):
            score[s] = max(score[s], seg["value"])
    picks = []
    for _ in range(count):
        pref = [0.0]
        for v in score:
            pref.append(pref[-1] + v)
        best, best_start = -1.0, 0
        for s in range(0, secs - length):
            total = pref[s + length] - pref[s]
            if total > best:
                best, best_start = total, s
        if best <= 0:
            break
        picks.append((best_start, best_start + length))
        for s in range(max(0, best_start - length), min(secs, best_start + 2 * length)):
            score[s] = 0.0  # on évite deux extraits quasi identiques
    return picks


def youtube_candidates(cfg):
    src = cfg["sources"].get("youtube") or {}
    if not src.get("enabled"):
        return []
    length = cfg["video"]["max_duration"]
    per_video = src.get("clips_per_video", 1)
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=src.get("lookback_hours", 72))
    min_age = timedelta(hours=src.get("min_age_hours", 12))
    out = []
    for ch in src.get("channels", []):
        url = ch["url"] if isinstance(ch, dict) else ch
        name = ch.get("name") if isinstance(ch, dict) else None
        try:
            with yt_dlp.YoutubeDL({**ydl_base_opts(cfg), "extract_flat": True,
                                   "playlistend": src.get("videos_per_channel", 5)}) as y:
                listing = y.extract_info(url, download=False)
        except Exception as e:  # yt-dlp lève des types variés
            log.error("YouTube %s : %s", url, e)
            continue
        for entry in listing.get("entries") or []:
            vid = entry.get("id")
            if not vid:
                continue
            sid_prefix = f"yt:{vid}:"
            try:
                with yt_dlp.YoutubeDL(ydl_base_opts(cfg)) as y:
                    info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            except Exception as e:
                log.warning("YouTube %s : %s", vid, e)
                continue
            ts = info.get("timestamp") or info.get("release_timestamp")
            if ts:
                age = now - datetime.fromtimestamp(ts, timezone.utc)
                if age > max_age or age < min_age:
                    continue
            heatmap = info.get("heatmap")
            if not heatmap:
                log.info("Pas de heatmap pour %s (vidéo trop récente ou peu vue), ignorée", vid)
                continue
            for k, (s, e) in enumerate(best_windows(heatmap, info["duration"], length, per_video)):
                out.append({
                    "source_id": sid_prefix + str(k), "platform": "youtube", "kind": "youtube",
                    "channel": name or info.get("channel") or info.get("uploader", "?"),
                    "title": info.get("title", ""), "url": info["webpage_url"],
                    "views": info.get("view_count") or 0,
                    "credit_url": info.get("channel_url") or url,
                    "start": s, "end": e,
                })
    log.info("YouTube : %d extrait(s) candidat(s)", len(out))
    return out


# --------------------------------------------------------------------------
# Téléchargement et montage
# --------------------------------------------------------------------------
def download(c, work, cfg):
    opts = {
        **ydl_base_opts(cfg),
        "outtmpl": str(work / "source.%(ext)s"),
        # meilleure image jusqu'en 1080p : 60 i/s et plus gros débit d'abord
        "format": "bv*[height<=1440]+ba/b[height<=1440]/bv*+ba/b",
        "format_sort": ["res:1440", "fps", "vbr", "tbr"],
        "merge_output_format": "mp4",
    }
    if c.get("start") is not None:
        # Coupe SANS réencodage (pas de perte) avec 6 s de marge ; la coupe exacte est faite ensuite,
        # en recalant l'extrait sur le son du live (précision 0,1 s)
        a = max(0.0, float(c["start"]) - 6)
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(a, float(c["end"]) + 6)])
        c["_dl_from"] = a
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([c["url"]])
    files = sorted(work.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("téléchargement vide")
    if c.get("start") is not None:
        return exact_cut(c, files[0], work, cfg)
    return files[0]


def video_fps(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=avg_frame_rate", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=30).stdout.strip()
        a, b = r.split("/") if "/" in r else (r, "1")
        return float(a) / float(b or 1)
    except Exception:
        return 30.0


def exact_cut(c, src, work, cfg):
    """La copie sans réencodage commence sur une image clé (quelques secondes avant).
    On retrouve où elle commence vraiment en comparant son son à celui du live, puis on coupe pile."""
    import numpy as np
    import analyse
    want_from = float(c["start"])
    dl_from = float(c.get("_dl_from", want_from))
    offset = want_from - dl_from                       # valeur par défaut si le recalage échoue
    vid = c["source_id"].split(":")[1] if c["source_id"].startswith("vod:") else None
    npy = resolve(cfg.get("work_dir", "work")) / "vodcache" / f"{vid}.s60.npy"
    if vid and npy.exists():
        try:
            F = np.load(npy).astype(np.float32)
            _, Fc = analyse.features(str(src))
            a = max(0, int((dl_from - 15) * analyse.FPS))
            ix = analyse.VodIndex(F[a:int((want_from + 25) * analyse.FPS)])
            q = Fc[:12 * analyse.FPS]
            Wq = analyse._whiten(q)
            best = (-1.0, None)
            for p in ix.coarse(analyse._groups(Wq), 5):
                sc, pos = ix.verify(Wq, p, 0)
                if sc > best[0]:
                    best = (sc, pos)
            if best[1] is not None and best[0] >= 0.35:
                real_from = (a + best[1]) / analyse.FPS
                offset = want_from - real_from
                log.debug("Recalage : l'extrait commence à %.1f s (demandé %.1f s)", real_from, want_from)
        except Exception as e:
            log.debug("Recalage impossible (%s)", e)
    offset = max(0.0, offset)
    dur = float(c["end"]) - want_from
    out = work / "cut.mp4"
    q = quality_profile(cfg)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{offset:.2f}", "-i", src.name, "-t", f"{dur:.2f}",
                    "-c:v", "libx264", "-preset", "fast", "-crf", str(q["inter_crf"]),
                    "-c:a", "aac", "-b:a", "256k", out.name], cwd=work, check=True)
    return out


def quality_profile(cfg):
    """Réglages d'encodage selon Réglages → Qualité vidéo."""
    v = cfg["video"]
    level = v.get("quality", "max")
    if level == "normale":
        return {"preset": "veryfast", "crf": int(v.get("crf", 21)), "scale": None, "inter_crf": 16, "sharp": False}
    if level == "max":
        return {"preset": "slow", "crf": 16, "scale": (1440, 2560), "inter_crf": 10, "sharp": True}
    return {"preset": "medium", "crf": 18, "scale": None, "inter_crf": 12, "sharp": True}


def _finish_filters(q):
    """Fin de chaîne vidéo : léger renfort de netteté (le flux du live est compressé) et 1440p en qualité max
    (YouTube garde alors une bien meilleure qualité)."""
    f = ""
    if q["sharp"]:
        f += ",unsharp=5:5:0.5:5:5:0.0"
    if q["scale"]:
        f += f",scale={q['scale'][0]}:{q['scale'][1]}:flags=lanczos"
    return f


_whisper_model = None


def _ts(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02}:{int(m):02}:{int(s):02},{int((s % 1) * 1000):03}"


def transcribe(src, srt_path, cfg):
    """Sous-titres façon TikTok (3-4 mots à la fois). Retourne False si indisponible."""
    sub = cfg["video"].get("subtitles") or {}
    if not sub.get("enabled"):
        return False
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning("faster-whisper non installé : pas de sous-titres")
        return False
    if _whisper_model is None:
        _whisper_model = WhisperModel(sub.get("model", "small"), device="cpu", compute_type="int8")
    segments, _ = _whisper_model.transcribe(
        str(src), language=sub.get("language", "fr"), vad_filter=True, word_timestamps=True)
    words = [w for seg in segments for w in (seg.words or [])]
    if not words:
        return False
    per = sub.get("words_per_line", 4)
    lines = []
    for i in range(0, len(words), per):
        chunk = words[i:i + per]
        text = " ".join(w.word.strip() for w in chunk).upper()
        lines.append(f"{len(lines) + 1}\n{_ts(chunk[0].start)} --> {_ts(chunk[-1].end)}\n{text}\n")
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return True


FONT_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _dt_escape(text):
    """Échappe un texte pour le filtre drawtext de ffmpeg."""
    return (text.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:")
            .replace("%", "\\%").replace(",", "\\,"))


def _srt_to_ass(srt_path, ass_path, fontname, accent_hex):
    """Sous-titres au format ASS en repère 1080x1920 : placés juste sous le gameplay,
    la seconde moitié de chaque ligne en couleur d'accent (comme la maquette)."""
    r, g, b = accent_hex[0:2], accent_hex[2:4], accent_hex[4:6]
    accent_ass = f"&H{b}{g}{r}&"
    def t(ts):  # 00:00:01,234 -> 0:00:01.23
        h, m, rest = ts.strip().split(":")
        sec, ms = rest.split(",")
        return f"{int(h)}:{m}:{sec}.{ms[:2]}"
    events = []
    for block in srt_path.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start, end = lines[1].split("-->")
        words = " ".join(lines[2:]).replace("{", "(").replace("}", ")").split()
        cut = max(1, len(words) // 2)
        text = " ".join(words[:cut]) + (" {\\c" + accent_ass + "}" + " ".join(words[cut:]) if words[cut:] else "")
        events.append(f"Dialogue: 0,{t(start)},{t(end)},Sub,,0,0,0,,{text}")
    ass_path.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Sub,{fontname},62,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,4,0,8,60,60,1240,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(events) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Accroche dès la 1re seconde
# --------------------------------------------------------------------------
def make_teaser(src, work, cfg):
    """« Cold open » : les ~3 s les plus fortes de l'extrait sont montrées tout au début,
    puis la vidéo repart du début. Les 2 premières secondes décident si les gens restent."""
    import numpy as np
    import analyse
    h = (cfg["video"].get("hook") or {})
    if not h.get("enabled", True) or not h.get("teaser", True):
        return src, 0.0
    levels, _ = analyse.features(str(src))
    n = len(levels)
    if n < 12:
        return src, 0.0
    lv = np.convolve(np.asarray(levels, float), np.ones(2) / 2, mode="same")
    peak = 4 + int(np.argmax(lv[4:n - 2]))
    if peak < 7 or lv[peak] - np.median(lv) < 4:      # déjà au début, ou pas de vrai pic : pas de teaser
        return src, 0.0
    a, b = max(0.0, peak - 1.4), min(float(n), peak + 1.6)
    t_len = b - a
    keep = min(float(n), float(cfg["video"]["max_duration"]) - t_len)
    out = work / "hooked.mp4"
    g = (f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[tv];[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[ta];"
         f"[0:v]trim=start=0:end={keep},setpts=PTS-STARTPTS[mv];[0:a]atrim=start=0:end={keep},asetpts=PTS-STARTPTS[ma];"
         "[tv][ta][mv][ma]concat=n=2:v=1:a=1[v][a]")
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src.name, "-filter_complex", g,
                        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "fast",
                        "-crf", str(quality_profile(cfg)["inter_crf"]),
                        "-c:a", "aac", "-b:a", "256k", out.name], cwd=work, check=True)
    except subprocess.CalledProcessError as e:
        log.warning("Teaser impossible (%s) : vidéo sans teaser", e)
        return src, 0.0
    log.info("Accroche : teaser de %.1f s (pic à %d s) placé au début", t_len, peak)
    return out, t_len


def best_quote(work):
    """La réplique la plus forte des sous-titres (réactions, rires, répétitions) : (texte, note) ou (None, 0)."""
    import analyse
    best, best_s = None, 0.0
    srt = work / "subs.srt"
    if srt.exists():
        for block in srt.read_text(encoding="utf-8").strip().split("\n\n"):
            lines = block.strip().splitlines()
            if len(lines) < 3:
                continue
            txt = " ".join(lines[2:]).strip()
            words = [(i * 0.3, i * 0.3 + 0.25, w) for i, w in enumerate(txt.split())]
            sc, _ = analyse.speech_score(words, max(1.0, len(words) * 0.3))
            if 2 <= len(words) <= 12 and sc > best_s:
                best, best_s = txt, sc
    return (best, best_s) if best and best_s >= 0.15 else (None, 0.0)


def hook_text(work, cfg):
    """Phrase d'accroche affichée en gros pendant 2,5 s : la réplique la plus forte des sous-titres,
    sinon le texte de secours des réglages."""
    h = (cfg["video"].get("hook") or {})
    if not h.get("enabled", True) or not h.get("text_banner", False):   # bandeau texte : désactivé par défaut
        return None
    best, _ = best_quote(work)
    if best:
        return ("« " + best.upper().rstrip(".") + " »")[:44]
    fb = (h.get("fallback") or "").strip()
    return fb[:44] or None


def _hook_filter(work, font, accent, y):
    """drawtext de la phrase d'accroche (fichier texte : pas de souci d'échappement)."""
    txt = (work / "hook.txt")
    if not txt.exists():
        return ""
    words, lines, cur = txt.read_text(encoding="utf-8").split(), [], ""
    for w in words:                      # 2 lignes de ~22 caractères max
        if len(cur) + len(w) + 1 > 22 and cur:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    lines.append(cur)
    txt.write_text("\n".join(lines[:2]), encoding="utf-8")
    return (f",drawtext=fontfile='{font}':expansion=none:textfile=hook.txt:fontsize=58:fontcolor=black:"
            f"box=1:boxcolor=0x{accent}:boxborderw=22:line_spacing=10:x=(w-text_w)/2:y={y}:"
            "enable='lt(t,2.6)'")


def render_bandeau(src, work, cfg, with_subs):
    """Design A : fond noir, logo + « ABONNEZ-VOUS ET LIKEZ ! » en haut, gameplay encadré
    de jaune au centre, sous-titres en dessous, pied de page discret."""
    v = cfg["video"]
    b = v.get("bandeau") or {}
    accent = b.get("accent", "#FFD21F").lstrip("#")
    font = resolve(b.get("font", "assets/fonts/RubikMonoOne-Regular.ttf"))
    font = str(font) if font.exists() else FONT_FALLBACK
    lg = v.get("logo") or {}
    logo = resolve(lg.get("file", "assets/logo.png"))
    use_logo = logo.exists()
    tx = 264 if use_logo else 54                      # texte à droite du logo
    line1 = _dt_escape(b.get("line1", "ABONNEZ-VOUS"))
    line2 = _dt_escape(b.get("line2", "ET LIKEZ !"))
    footer = _dt_escape(b.get("footer", "REDIFSTREAM"))
    dur = float(v["max_duration"])

    fps = 60 if video_fps(src) >= 50 else 30          # garde la fluidité des lives en 60 i/s
    qp = quality_profile(cfg)
    g = (f"color=c=0x0E0F13:s=1080x1920:r={fps}:d={dur}[bg0];"
         "[0:v]scale=1080:608:force_original_aspect_ratio=decrease:flags=lanczos,"
         f"pad=1080:608:(ow-iw)/2:(oh-ih)/2:color=0x0E0F13,fps={fps}[fg];"
         f"[bg0]drawbox=x=0:y=0:w=1080:h=24:color=0x{accent}:t=fill,"
         f"drawbox=x=0:y=555:w=1080:h=9:color=0x{accent}:t=fill,"
         f"drawbox=x=0:y=1173:w=1080:h=9:color=0x{accent}:t=fill[bg1];"
         "[bg1][fg]overlay=0:564:shortest=1[b2];")
    last = "b2"
    if use_logo:
        g += "[1:v]scale=174:174:force_original_aspect_ratio=decrease[lg];[b2][lg]overlay=54:168[b3];"
        last = "b3"
    g += (f"[{last}]drawtext=fontfile='{font}':expansion=none:text='{line1}':fontsize=57:fontcolor=white:x={tx}:y=190,"
          f"drawtext=fontfile='{font}':expansion=none:text='{line2}':fontsize=57:fontcolor=0x{accent}:x={tx}:y=262,"
          f"drawtext=fontfile='{font}':expansion=none:text='{footer}':fontsize=34:fontcolor=0x9AA1AE:"
          "x=(w-text_w)/2:y=1440"
          + _hook_filter(work, font, accent, 640) + ",format=yuv420p")
    if with_subs and (work / "subs.srt").exists():
        fontname = "Rubik Mono One" if font != FONT_FALLBACK else "DejaVu Sans"
        _srt_to_ass(work / "subs.srt", work / "subs.ass", fontname, accent)
        g += f",subtitles=subs.ass:fontsdir='{Path(font).parent}'"
    g += _finish_filters(qp) + "[v]"
    out = work / "final.mp4"
    inputs = ["-i", src.name] + (["-i", str(logo)] if use_logo else [])
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-t", str(dur),
           "-filter_complex", g, "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", qp["preset"], "-crf", str(qp["crf"]), "-profile:v", "high",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", out.name]
    subprocess.run(cmd, cwd=work, check=True)
    return out


def render(src, work, cfg, with_subs):
    v = cfg["video"]
    if v.get("layout", "flou") == "bandeau":
        return render_bandeau(src, work, cfg, with_subs)
    lg = v.get("logo") or {}
    logo = resolve(lg.get("file", "assets/logo.png"))
    use_logo = bool(lg.get("enabled")) and logo.exists()
    if lg.get("enabled") and not logo.exists():
        log.warning("Logo activé mais fichier absent (%s) : vidéo sans logo", logo)
    fps = 60 if video_fps(src) >= 50 else 30
    qp = quality_profile(cfg)
    graph = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=20:5[bg];"
        "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={fps}[base];"
    )
    last = "base"
    if use_logo:
        width = int(lg.get("width", 220))
        margin = int(lg.get("margin", 150))
        # En haut : dans la bande floue au-dessus de l'image. En bas : au-dessus de l'interface YouTube.
        y = str(margin) if lg.get("position", "top") == "top" else "H-h-200"
        opacity = float(lg.get("opacity", 1.0))
        graph += (f"[1:v]scale={width}:-1,format=rgba,colorchannelmixer=aa={opacity}[lg];"
                  f"[base][lg]overlay=(W-w)/2:{y}[withlogo];")
        last = "withlogo"
    bfont = resolve((v.get("bandeau") or {}).get("font", "assets/fonts/RubikMonoOne-Regular.ttf"))
    bfont = str(bfont) if bfont.exists() else FONT_FALLBACK
    graph += f"[{last}]null{_hook_filter(work, bfont, 'FFD21F', 330)},format=yuv420p"
    if with_subs:
        graph += (",subtitles=subs.srt:force_style='FontName=DejaVu Sans,FontSize=16,Bold=1,"
                  "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,"
                  "Outline=2,Shadow=0,Alignment=2,MarginV=70'")
    graph += _finish_filters(qp) + "[v]"
    out = work / "final.mp4"
    inputs = ["-i", src.name] + (["-i", str(logo)] if use_logo else [])
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", *inputs,
        "-t", str(v["max_duration"]), "-filter_complex", graph,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", qp["preset"], "-crf", str(qp["crf"]), "-profile:v", "high",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", out.name,
    ]
    subprocess.run(cmd, cwd=work, check=True)
    return out


def build_meta(cfg, c):
    m = cfg["metadata"]
    raw = c["title"].strip()
    if len(raw.replace(" ", "")) < m.get("min_title_length", 4):  # titres du genre « x »
        raw = m.get("fallback_title") or "Moment fou en live"
    q = (c.get("quote") or "").strip().strip("«»\"“” ").rstrip(".")
    if q and m.get("spoken_titles", True):
        # Titre tiré de ce qui est dit, au style qui marche chez les clippers (voir Tendances)
        styles = {x["name"]: x["effect"] for x in (load_trends(cfg).get("tables") or {}).get("Style de titre", [])}
        if styles.get("en MAJUSCULES — oui", 0) > 0.08:
            q = q.upper()
        if styles.get("point d'exclamation « ! » — oui", 0) > 0.05 and not q.endswith(("!", "?")):
            q += " !"
        q = (q[:1].upper() + q[1:])[:70]
        raw = (m.get("title_spoken") or "« {quote} »").replace("{quote}", q)
    fields = {"title": raw, "channel": c["channel"],
              "url": c["url"], "credit_url": c["credit_url"]}

    def fill(tpl):  # remplacement simple : pas de str.format sur un modèle modifiable
        for k, v in fields.items():
            tpl = tpl.replace("{" + k + "}", str(v))
        return tpl

    title = fill(m["title"]).replace("<", "").replace(">", "")
    desc = fill(m["description"])
    tiktok_caption = fill(m.get("tiktok_caption", "{title}"))
    return {"title": title[:100], "description": desc[:4900],
            "tags": m.get("tags", []), "tiktok_caption": tiktok_caption[:2200]}


# --------------------------------------------------------------------------
# Publication : YouTube
# --------------------------------------------------------------------------
class YouTubeUploader:
    SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

    def __init__(self, ycfg):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_file = resolve(ycfg["token_file"])
        creds = Credentials.from_authorized_user_file(str(token_file), self.SCOPES)
        if not creds.valid:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
        self.yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        self.cfg = ycfg

    def upload(self, path, meta):
        from googleapiclient.http import MediaFileUpload

        body = {
            "snippet": {"title": meta["title"], "description": meta["description"],
                        "tags": meta["tags"], "categoryId": str(self.cfg.get("category_id", 20))},
            "status": {"privacyStatus": self.cfg.get("privacy", "public"),
                       "selfDeclaredMadeForKids": False},
        }
        req = self.yt.videos().insert(
            part="snippet,status", body=body,
            media_body=MediaFileUpload(str(path), mimetype="video/mp4",
                                       chunksize=8 * MB, resumable=True))
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp["id"]


# --------------------------------------------------------------------------
# Publication : TikTok (Content Posting API)
# --------------------------------------------------------------------------
class TikTokUploader:
    API = "https://open.tiktokapis.com/v2"

    def __init__(self, tcfg):
        self.cfg = tcfg
        self.token_file = resolve(tcfg["token_file"])
        self._refresh()

    def _refresh(self):
        tok = json.loads(self.token_file.read_text())
        r = requests.post(
            self.API + "/oauth/token/",
            data={"client_key": self.cfg["client_key"], "client_secret": self.cfg["client_secret"],
                  "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        data = r.json()
        if "access_token" not in data:
            raise RuntimeError(f"TikTok : rafraîchissement du jeton impossible ({data})")
        self.token_file.write_text(json.dumps(data))
        os.chmod(self.token_file, 0o600)
        self.headers = {"Authorization": "Bearer " + data["access_token"],
                        "Content-Type": "application/json; charset=UTF-8"}

    def _post(self, path, payload):
        r = requests.post(self.API + path, headers=self.headers, json=payload, timeout=60)
        data = r.json()
        err = data.get("error") or {}
        if r.status_code != 200 or err.get("code", "ok") != "ok":
            raise RuntimeError(f"TikTok {path} : {err.get('code')} {err.get('message')}")
        return data.get("data") or {}

    def upload(self, path, meta):
        size = path.stat().st_size
        # Règles TikTok : morceaux de 5 à 64 Mo, le dernier absorbe le reste
        if size <= 64 * MB:
            chunk, count = size, 1
        else:
            chunk = 10 * MB
            count = size // chunk
        source = {"source": "FILE_UPLOAD", "video_size": size,
                  "chunk_size": chunk, "total_chunk_count": count}

        if self.cfg.get("mode", "inbox") == "direct":
            info = self._post("/post/publish/creator_info/query/", {})
            wanted = self.cfg.get("privacy_level", "PUBLIC_TO_EVERYONE")
            options = info.get("privacy_level_options") or ["SELF_ONLY"]
            privacy = wanted if wanted in options else options[-1]
            data = self._post("/post/publish/video/init/", {
                "post_info": {"title": meta["tiktok_caption"], "privacy_level": privacy,
                              "disable_duet": False, "disable_comment": False,
                              "disable_stitch": False, "video_cover_timestamp_ms": 1000},
                "source_info": source})
        else:
            data = self._post("/post/publish/inbox/video/init/", {"source_info": source})

        upload_url, publish_id = data["upload_url"], data["publish_id"]
        with open(path, "rb") as f:
            for i in range(count):
                start = i * chunk
                length = chunk if i < count - 1 else size - start
                r = requests.put(upload_url, data=f.read(length), timeout=300, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(length),
                    "Content-Range": f"bytes {start}-{start + length - 1}/{size}"})
                if r.status_code not in (200, 201, 206):
                    raise RuntimeError(f"TikTok envoi morceau {i + 1}/{count} : HTTP {r.status_code}")

        for _ in range(30):
            time.sleep(10)
            st = self._post("/post/publish/status/fetch/", {"publish_id": publish_id})
            status = st.get("status")
            if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                return publish_id
            if status == "FAILED":
                raise RuntimeError(f"TikTok : échec ({st.get('fail_reason')})")
        log.warning("TikTok : statut final non confirmé pour %s", publish_id)
        return publish_id


# --------------------------------------------------------------------------
# Boucle principale
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Avancement (affiché en direct dans l'interface)
# --------------------------------------------------------------------------
_progress = {"started": None}


def progress(step, detail=""):
    try:
        if _progress["started"] is None:
            _progress["started"] = time.time()
        f = BASE / "work" / "status.json"
        f.parent.mkdir(exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"step": step, "detail": detail, "t": time.time(),
                                   "started": _progress["started"], "pid": os.getpid()}, ensure_ascii=False))
        tmp.replace(f)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Apprendre de ses propres Shorts (vues et likes sur RedifStream)
# --------------------------------------------------------------------------
KIND_NAMES = {"clipper": "repris par des clippers", "viewer": "clip des viewers",
              "moment": "moment fort détecté", "youtube": "vidéo YouTube"}


def own_stats(cfg, state, force=False):
    """Relève les vues/likes des Shorts publiés (au plus toutes les 6 h) et en déduit ce qui marche :
    effet de chaque streamer, de chaque type de passage, de la durée et de l'heure de publication."""
    import numpy as np
    work = resolve(cfg.get("work_dir", "work"))
    f_raw, f_out = work / "ytstats.json", work / "performances.json"
    out = json.loads(f_out.read_text()) if f_out.exists() else {}
    if (not force and out.get("t", 0) > time.time() - 6 * 3600) or youtube_blocked(cfg):
        return out
    raw = json.loads(f_raw.read_text()) if f_raw.exists() else {}
    url = (cfg.get("youtube_url") or "https://www.youtube.com/@redifstream2").rstrip("/") + "/shorts"
    try:
        with yt_dlp.YoutubeDL({**ydl_base_opts(cfg), "extract_flat": True, "playlistend": 200}) as y:
            listing = y.extract_info(url, download=False)
    except Exception as e:
        log.warning("Statistiques YouTube indisponibles : %s", e)
        return out
    now = time.time()
    rows = {r["youtube_id"]: dict(r) for r in state.db.execute(
        "SELECT * FROM clips WHERE status='done' AND youtube_id IS NOT NULL")}
    detail_budget = 30
    for e in listing.get("entries") or []:
        vid = e.get("id")
        if not vid:
            continue
        r = raw.setdefault(vid, {})
        if e.get("view_count") is not None:
            r["views"] = int(e["view_count"])
        r["title"] = e.get("title") or r.get("title", "")
        # détails (likes, date) : pour les nouveaux, puis une fois par jour, dans la limite du budget
        if detail_budget > 0 and now - r.get("fetched", 0) > 86400:
            detail_budget -= 1
            try:
                with yt_dlp.YoutubeDL(ydl_base_opts(cfg)) as y:
                    info = y.extract_info(f"https://www.youtube.com/shorts/{vid}", download=False)
                r.update(views=int(info.get("view_count") or r.get("views") or 0),
                         likes=int(info.get("like_count") or 0), ts=info.get("timestamp"),
                         duration=info.get("duration"), fetched=now)
            except Exception as ex:
                log.debug("Short %s : %s", vid, ex)
    f_raw.write_text(json.dumps(raw, ensure_ascii=False))

    from zoneinfo import ZoneInfo
    data = []
    for vid, r in raw.items():
        row = rows.get(vid)
        if not row or r.get("views") is None:
            continue
        ts = r.get("ts")
        if not ts:
            try:
                ts = datetime.fromisoformat(row["updated"]).timestamp()
            except Exception:
                continue
        age_h = (now - ts) / 3600
        if age_h < 24:            # trop récent pour juger
            continue
        kind = row.get("kind") or ("viewer" if row["platform"] == "twitch" else
                                   "moment" if row["platform"] == "twitch-vod" else row["platform"])
        hour = datetime.fromtimestamp(ts, ZoneInfo("Europe/Paris")).hour
        dur = r.get("duration") or 0
        # score : vues (échelle log) + un peu les likes, corrigé de l'âge (les vues montent avec le temps)
        perf = (np.log10(r["views"] + 1) + 0.5 * np.log10(r.get("likes", 0) + 1)
                - 0.3 * np.log10(max(age_h, 24) / 24))
        data.append({"id": vid, "title": r.get("title") or row["title"], "streamer": row["channel"],
                     "kind": kind, "hour": hour, "dur": dur, "views": r["views"], "likes": r.get("likes", 0),
                     "perf": float(perf)})
    res = {"t": now, "n": len(data), "effects": {}, "tables": {}, "top": []}
    if data:
        base = float(np.median([d["perf"] for d in data]))

        def effects(key, label=lambda k: k):
            groups = {}
            for d in data:
                groups.setdefault(d[key], []).append(d)
            table = []
            for k, ds in groups.items():
                n = len(ds)
                eff = (np.mean([d["perf"] for d in ds]) - base) * n / (n + 3)   # prudent s'il y a peu de Shorts
                table.append({"name": label(k), "key": str(k), "n": n, "effect": round(float(eff), 3),
                              "views": int(np.mean([d["views"] for d in ds])),
                              "likes": int(np.mean([d["likes"] for d in ds]))})
            return sorted(table, key=lambda x: -x["effect"])

        res["tables"] = {
            "Streamer": effects("streamer"),
            "Type de passage": effects("kind", lambda k: KIND_NAMES.get(k, k)),
            "Heure de publication": effects("hour", lambda k: f"{k}h"),
        }
        for d in data:
            d["dur_b"] = "moins de 30 s" if d["dur"] and d["dur"] < 30 else "30-45 s" if d["dur"] and d["dur"] < 45 \
                else "45 s et plus" if d["dur"] else "?"
        res["tables"]["Durée"] = effects("dur_b")
        res["effects"] = {"streamer": {t["key"].lower(): t["effect"] for t in res["tables"]["Streamer"]},
                          "kind": {t["key"]: t["effect"] for t in res["tables"]["Type de passage"]}}
        res["top"] = sorted(data, key=lambda d: -d["views"])[:5]
        log.info("Statistiques : %d Short(s) analysé(s) ; meilleur streamer : %s, meilleur type : %s",
                 len(data), res["tables"]["Streamer"][0]["name"], res["tables"]["Type de passage"][0]["name"])
    f_out.write_text(json.dumps(res, ensure_ascii=False))
    return res


def queue_file(cfg):
    return resolve(cfg.get("work_dir", "work")) / "file_attente.json"


def load_queue(cfg):
    try:
        q = json.loads(queue_file(cfg).read_text())
    except (OSError, ValueError):
        q = {}
    return {"top": q.get("top") or [], "skip": q.get("skip") or [], "info": q.get("info") or {}}


def save_queue(cfg, q):
    f = queue_file(cfg)
    f.parent.mkdir(parents=True, exist_ok=True)
    skip = q["skip"][-2000:]
    info = {k: v for k, v in (q.get("info") or {}).items() if k in set(skip)}
    f.write_text(json.dumps({"top": q["top"][-50:], "skip": skip, "info": info}, ensure_ascii=False))


def apply_perf(candidates, perf):
    """Ce qui marche sur ta chaîne (streamer, type de passage) fait monter ou descendre un candidat."""
    for c in candidates:
        bonus = (perf.get("streamer") or {}).get(c["channel"].lower(), 0) + (perf.get("kind") or {}).get(c.get("kind"), 0)
        if bonus:
            base_rank = c.get("rank", 2 + min(1.0, math.log10(c["views"] + 1) / 5))
            c["rank"] = base_rank + 0.25 * max(-1.0, min(1.0, bonus))


def order_candidates(cfg, state, candidates, max_tries=3):
    """L'ordre dans lequel ClipBot publiera (utilisé par les passages ET par la page File d'attente)."""
    seen = set()
    q = load_queue(cfg)
    skip = set(q["skip"])
    skip_vod = [x for x in skip if x.startswith("vod:")]
    out = [c for c in candidates
           if c["source_id"] not in seen and not seen.add(c["source_id"])
           and c["source_id"] not in skip and not any(same_moment(c["source_id"], x) for x in skip_vod)
           and not state.is_done(c["source_id"], max_tries)]
    # Ordre : moments des clippers > clips des viewers > moments forts (chacun trié par force/vues)
    out.sort(key=lambda c: (c.get("rank", 2 + min(1.0, math.log10(c["views"] + 1) / 5)), c["views"]),
             reverse=True)
    if cfg.get("rotation", True):
        # Le créateur publié il y a le plus longtemps passe en premier (tri stable : garde l'ordre ci-dessus)
        last_by_channel = state.last_post_by_channel()
        out.sort(key=lambda c: last_by_channel.get(c["channel"].lower(), ""))
    # Variété : un extrait du même live que la dernière vidéo publiée passe après les autres
    last = state.db.execute("SELECT source_id FROM clips WHERE status='done' ORDER BY updated DESC LIMIT 1").fetchone()
    if last and str(last[0]).startswith("vod:"):
        same = str(last[0]).rsplit(":", 1)[0] + ":"
        out.sort(key=lambda c: c["source_id"].startswith(same))
    # Ce que tu as mis en tête dans la File d'attente passe avant tout (dans ton ordre)
    def top_pos(c):
        for i, sid in enumerate(q["top"]):
            if same_moment(c["source_id"], sid):
                return i
        return len(q["top"])
    out.sort(key=top_pos)
    return out


def run(cfg, dry_run=False, only=None, force=False):
    state = State(resolve(cfg.get("state_db", "clipbot.db")))
    max_tries = cfg.get("max_tries", 3)

    # Pause : seuls les essais et les publications demandées à la main passent
    if cfg.get("paused") and not dry_run and not only and not force:
        log.info("ClipBot est en pause : aucune publication automatique.")
        return

    if force:
        log.info("Lancement manuel : heures d'activité et intervalle ignorés.")
    if not dry_run and not only and not force:
        sch = cfg.get("schedule") or {}
        # Heures d'activité (heure de Paris)
        try:
            from zoneinfo import ZoneInfo
            hour = datetime.now(ZoneInfo(sch.get("timezone", "Europe/Paris"))).hour
        except Exception:
            hour = datetime.now().hour
        h_start, h_end = int(sch.get("start_hour", 0)), int(sch.get("end_hour", 24))
        if not (h_start <= hour < h_end):
            log.info("Hors des heures d'activité (%dh-%dh) : rien à faire.", h_start, h_end)
            return
        # Intervalle entre deux clips
        interval = int(sch.get("interval_minutes", 0))
        last = state.last_post_time()
        if interval and last:
            wait = last + timedelta(minutes=interval) - datetime.now(timezone.utc)
            if wait.total_seconds() > 60:
                log.info("Prochain clip dans %d min.", wait.total_seconds() // 60 + 1)
                return

    progress("Recherche des clips et des lives")
    perf = {}
    if cfg.get("learn_from_stats", True):
        try:
            perf = own_stats(cfg, state).get("effects") or {}
        except Exception as e:
            log.warning("Statistiques ignorées : %s", e)
    candidates = twitch_candidates(cfg, state) + youtube_candidates(cfg)
    try:   # image de la file pour l'interface (page File d'attente instantanée)
        f = resolve(cfg.get("work_dir", "work")) / "file_snapshot.json"
        f.write_text(json.dumps([c for c in candidates if c["platform"] != "youtube"], ensure_ascii=False, default=str))
    except (OSError, TypeError):
        pass
    apply_perf(candidates, perf)
    if only:
        # « Publier maintenant » : ce clip-là seulement, sauf s'il est déjà publié
        row = state.get(only)
        exact = [c for c in candidates if c["source_id"] == only]
        near = sorted((c for c in candidates if same_moment(c["source_id"], only)),
                      key=lambda c: abs(int(c["source_id"].rsplit(":", 1)[1]) - int(only.rsplit(":", 1)[1])))
        candidates = (exact or near)[:1]
        if row and row["status"] == "done":
            log.info("Ce clip est déjà publié.")
            return
        if not candidates:
            log.error("Clip %s introuvable parmi les clips disponibles (trop ancien ?).", only)
            notify(cfg, "ClipBot : clip introuvable", only, 4, ["warning"])
            return
        budget = 1
    else:
        candidates = order_candidates(cfg, state, candidates, max_tries)
        budget = min(cfg.get("max_posts_per_run", 2),
                     cfg.get("max_posts_per_day", 6) - state.posted_last_24h())
    if budget <= 0 or not candidates:
        log.info("Rien à publier (budget %d, %d candidat(s))", budget, len(candidates))
        return

    pub = cfg["publish"]
    yt = tt = browser = None
    if not dry_run:
        try:
            needs_browser = any(pub[p].get("enabled") and pub[p].get("method") == "browser"
                                for p in ("youtube", "tiktok"))
            if needs_browser:
                from browser_upload import BrowserSession, BrowserTikTok, BrowserYouTube
                browser = BrowserSession(pub.get("browser") or {}, resolve, resolve("screenshots"))
            if pub["youtube"].get("enabled"):
                yt = (BrowserYouTube(browser, pub["youtube"].get("privacy", "public"))
                      if pub["youtube"].get("method") == "browser" else YouTubeUploader(pub["youtube"]))
            if pub["tiktok"].get("enabled"):
                tt = (BrowserTikTok(browser) if pub["tiktok"].get("method") == "browser"
                      else TikTokUploader(pub["tiktok"]))
        except Exception as e:
            log.exception("Connexion aux plateformes impossible")
            notify(cfg, "ClipBot : erreur de connexion", str(e), 4, ["warning"])
            if browser:
                browser.close()
            return
    try:
        _process(cfg, state, candidates, budget, yt, tt, dry_run)
    finally:
        if browser:
            browser.close()


def _process(cfg, state, candidates, budget, yt, tt, dry_run):
    pub = cfg["publish"]

    work_root = resolve(cfg.get("work_dir", "work"))
    work_root.mkdir(parents=True, exist_ok=True)
    posted = 0
    waiting = 0
    for c in candidates:
        if posted >= budget:
            break
        from_youtube = "youtube.com" in (c.get("url") or "") or c["source_id"].startswith(("vod:yt-", "yt:"))
        if from_youtube and youtube_blocked(cfg):
            if not waiting:
                log.warning("Téléchargements YouTube en pause jusqu'à %s : les extraits des rediffs YouTube "
                            "attendent, on publie les clips Twitch (YouTube Shorts et TikTok).",
                            youtube_block_until(cfg))
            waiting += 1
            continue
        log.info("→ %s | %s (%d vues)", c["channel"], c["title"], c["views"])
        progress("Téléchargement de l'extrait", f"{c['channel']} — {c['title'][:60]}")
        work = Path(tempfile.mkdtemp(prefix="clip-", dir=work_root))
        try:
            src = download(c, work, cfg)
            try:
                src, _teaser = make_teaser(src, work, cfg)      # accroche : le moment fort d'abord
            except Exception as e:
                log.warning("Teaser ignoré : %s", e)
            try:
                progress("Sous-titres", f"{c['channel']} — {c['title'][:60]}")
                subs = transcribe(src, work / "subs.srt", cfg)
            except Exception as e:  # les sous-titres ne doivent jamais bloquer un clip
                log.warning("Sous-titres impossibles (%s), clip publié sans", e)
                subs = False
            try:
                c["quote"] = best_quote(work)[0] if subs else None
            except Exception:
                c["quote"] = None
            try:
                hk = hook_text(work, cfg) if subs or (cfg["video"].get("hook") or {}).get("fallback") else None
                (work / "hook.txt").unlink(missing_ok=True)
                if hk:
                    (work / "hook.txt").write_text(hk, encoding="utf-8")
                    log.info("Accroche : %s", hk)
            except Exception as e:
                log.warning("Texte d'accroche ignoré : %s", e)
            progress("Montage de la vidéo", f"{c['channel']} — {c['title'][:60]}")
            final = render(src, work, cfg, subs)
            meta = build_meta(cfg, c)

            if dry_run:
                preview = resolve("preview")
                preview.mkdir(exist_ok=True)
                dest = preview / (c["source_id"].replace(":", "_") + ".mp4")
                shutil.copy(final, dest)
                log.info("   [dry-run] %s | titre : %s", dest, meta["title"])
                posted += 1
                continue

            state.save(c)
            row = state.get(c["source_id"]) or {}
            links = []
            if yt:
                progress("Publication sur YouTube", meta["title"][:80])
                yt_id = row.get("youtube_id") or yt.upload(final, meta)
                state.save(c, youtube_id=yt_id)
                links.append("YouTube : " + (f"https://youtube.com/shorts/{yt_id}"
                                             if yt_id != "navigateur" else "publié"))
            if tt:
                tt_id = row.get("tiktok_id") or tt.upload(final, meta)
                state.save(c, tiktok_id=tt_id)
                inbox = pub["tiktok"].get("method") != "browser" and pub["tiktok"].get("mode", "inbox") != "direct"
                links.append("TikTok : " + ("brouillon dans ta boîte" if inbox else "publié"))
            if yt or tt:
                time.sleep(random.uniform(20, 60))  # pause entre deux clips
            state.save(c, status="done", error=None)
            posted += 1
            try:
                q = load_queue(cfg)
                if c["source_id"] in q["top"]:
                    q["top"].remove(c["source_id"])
                    save_queue(cfg, q)
            except OSError:
                pass
            short_url = (f"https://youtube.com/shorts/{row.get('youtube_id') or yt_id}"
                         if yt and (row.get("youtube_id") or yt_id) not in (None, "navigateur") else None)
            notify(cfg, f"🎬 Vidéo publiée : {c['channel']}",
                   f"{meta['title']}\n{KIND_NAMES.get(c.get('kind'), '')}\n" + "\n".join(links),
                   3, ["clapper"], click=short_url)
        except Exception as e:
            if is_bot_error(e):
                # pas la faute du clip : YouTube bloque un moment. On ne compte pas d'essai, on passe à Twitch.
                mark_youtube_blocked(cfg)
                state.save(c, status="error", error="YouTube bloque temporairement (anti-robot) : réessai auto")
                continue
            log.exception("Échec pour %s", c["source_id"])
            if not dry_run:
                state.fail(c, str(e))
            notify(cfg, "❌ ClipBot : échec d'une vidéo", f"{c['channel']} – {c['title']}\n{str(e)[:300]}",
                   4, ["warning"])
        finally:
            shutil.rmtree(work, ignore_errors=True)
    log.info("Terminé : %d clip(s) traité(s)", posted)


def main():
    ap = argparse.ArgumentParser(description="ClipBot")
    ap.add_argument("-c", "--config", default=str(BASE / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="monte sans publier (dossier preview/)")
    ap.add_argument("--force", action="store_true",
                    help="lancement manuel : ignore la pause, les heures d'activité et l'intervalle")
    ap.add_argument("--only", metavar="ID", help="publie tout de suite ce clip-là (id de l'historique)")
    ap.add_argument("--check-browser", action="store_true",
                    help="vérifie la connexion TikTok/YouTube du navigateur (captures dans screenshots/)")
    ap.add_argument("--tiktok-qr", action="store_true", help="connexion TikTok par QR code (depuis l'interface)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    from logging.handlers import RotatingFileHandler
    (BASE / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(),
                  RotatingFileHandler(BASE / "logs" / "clipbot.log", maxBytes=2 * MB,
                                      backupCount=3, encoding="utf-8")])

    # Un seul passage à la fois (minuteur systemd + bouton de l'interface web)
    import fcntl
    lock = open(BASE / "clipbot.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Un passage est déjà en cours, on s'arrête.")
        return
    lock.write(str(os.getpid()))
    lock.flush()
    import signal
    try:
        os.setpgrp()           # groupe à part : « Arrêter » coupe aussi ffmpeg, Chromium…
    except OSError:
        pass

    def _stop(signum, frame):
        log.warning("Arrêt demandé depuis l'interface : on s'arrête proprement.")
        raise SystemExit(1)
    signal.signal(signal.SIGTERM, _stop)
    progress("Démarrage")
    try:
        _main_body(args)
    except SystemExit:
        raise
    except Exception as e:
        log.exception("ClipBot s'est arrêté sur une erreur")
        try:
            notify(load_config(args.config), "❌ ClipBot : plantage", f"{type(e).__name__} : {str(e)[:300]}",
                   4, ["rotating_light"])
        except Exception:
            pass
        raise
    finally:
        try:
            (BASE / "work" / "status.json").unlink(missing_ok=True)
        except OSError:
            pass


def _main_body(args):
    cfg = load_config(args.config)
    if args.check_browser:
        from browser_upload import BrowserSession
        s = BrowserSession(cfg["publish"].get("browser") or {}, resolve, resolve("screenshots"))
        try:
            for name, (ok, shot, url) in s.check_logins().items():
                print(f"{name:8} {'✅ connecté' if ok else '❌ non connecté'}  {url}  ({shot})")
        finally:
            s.close()
        return
    if args.tiktok_qr:
        from browser_upload import BrowserSession
        progress("Connexion TikTok par QR code")
        s = BrowserSession(cfg["publish"].get("browser") or {}, resolve, resolve("screenshots"))
        try:
            work = resolve(cfg.get("work_dir", "work"))
            work.mkdir(parents=True, exist_ok=True)
            s.tiktok_qr_login(work / "tiktok_qr.png", work / "tiktok_login.json")
        finally:
            s.close()
        return
    run(cfg, dry_run=args.dry_run, only=args.only, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
