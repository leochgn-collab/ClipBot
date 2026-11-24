"""Publication par navigateur (Playwright) sur TikTok Studio et YouTube Studio.

Le navigateur utilise TON compte grâce à un profil persistant, initialisé
avec les cookies exportés depuis ton navigateur (format Netscape, cookies.txt).

⚠️ L'automatisation de ces sites est contraire à leurs conditions d'utilisation
et peut casser à chaque modification de leur interface. Les sélecteurs sont
modifiables dans config.yaml (section publish.browser.selectors).
"""

import logging
import random
import time
from pathlib import Path

log = logging.getLogger("clipbot.browser")

DEFAULT_SELECTORS = {
    "tiktok": {
        "upload_url": "https://www.tiktok.com/tiktokstudio/upload?lang=fr",
        "file_input": "input[type=file]",
        "caption": "div[contenteditable='true']",
        "post_button": "button[data-e2e='post_video_button']",
        # Boutons de confirmation possibles après « Publier » (FR / EN)
        "confirm_texts": ["Publier maintenant", "Post now", "Publier", "Post"],
        "success_url_part": "/tiktokstudio/content",
    },
    "youtube": {
        "upload_url": "https://www.youtube.com/upload",
        "file_input": "input[type=file]",
        "title": "#title-textarea #textbox",
        "description": "#description-textarea #textbox",
        "not_for_kids": "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
        "next_button": "#next-button",
        "visibility": {"public": "tp-yt-paper-radio-button[name='PUBLIC']",
                       "unlisted": "tp-yt-paper-radio-button[name='UNLISTED']",
                       "private": "tp-yt-paper-radio-button[name='PRIVATE']"},
        "video_link": "span.video-url-fadeable a, a.ytcp-video-info",
        "done_button": "#done-button",
    },
}


def _pause(a=0.6, b=1.8):
    time.sleep(random.uniform(a, b))


def load_netscape_cookies(path):
    """Lit un cookies.txt (format Netscape) et le convertit pour Playwright."""
    cookies = []
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only, line = True, line[len("#HttpOnly_"):]
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path_, secure, expires, name, value = parts[:7]
        c = {"name": name, "value": value.strip(), "domain": domain, "path": path_,
             "secure": secure.upper() == "TRUE", "httpOnly": http_only, "sameSite": "Lax"}
        if expires.strip() not in ("", "0"):
            c["expires"] = float(expires)
        cookies.append(c)
    return cookies


def _merge(defaults, overrides):
    out = dict(defaults)
    for k, v in (overrides or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class BrowserSession:
    """Un seul navigateur pour tout le passage, fermé à la fin."""

    def __init__(self, bcfg, resolve, shots_dir):
        from playwright.sync_api import sync_playwright

        self.cfg = bcfg
        self.shots = shots_dir
        self.shots.mkdir(parents=True, exist_ok=True)
        self.sel = {k: _merge(v, (bcfg.get("selectors") or {}).get(k))
                    for k, v in DEFAULT_SELECTORS.items()}
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(resolve(bcfg.get("profile_dir", "secrets/browser-profile"))),
            headless=bcfg.get("headless", False),  # False + xvfb-run = moins détectable
            locale="fr-FR", timezone_id="Europe/Paris",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        # Les cookies du fichier ne sont importés qu'UNE fois (ou quand tu en envoies de nouveaux) :
        # ensuite le profil du navigateur les rafraîchit tout seul. Les réinjecter à chaque passage
        # écrasait la session rafraîchie et Google déconnectait ClipBot.
        for key in ("tiktok_cookies", "youtube_cookies"):
            if not bcfg.get(key):
                continue
            src = resolve(bcfg[key])
            if not src.exists():
                continue
            marker = src.with_name("." + src.name + ".importe")
            if marker.exists() and marker.stat().st_mtime >= src.stat().st_mtime:
                continue
            self.ctx.add_cookies(load_netscape_cookies(src))
            marker.write_text(str(time.time()))
            log.info("Cookies %s importés dans le profil du navigateur", key.split("_")[0])
        self.ctx.set_default_timeout(bcfg.get("timeout_s", 60) * 1000)

    def close(self):
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    def screenshot(self, page, name):
        path = self.shots / f"{int(time.time())}-{name}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            return None
        return path

    def check_logins(self):
        """Ouvre les deux studios et fait une capture : vérifie que tu es connecté."""
        res = {}
        for name, url in (("tiktok", self.sel["tiktok"]["upload_url"]),
                          ("youtube", self.sel["youtube"]["upload_url"])):
            page = self.ctx.new_page()
            page.goto(url, wait_until="domcontentloaded")
            _pause(4, 6)
            logged = page.locator(self.sel[name]["file_input"]).count() > 0
            res[name] = (logged, self.screenshot(page, f"check-{name}"), page.url)
            page.close()
        return res


class BrowserTikTok:
    def __init__(self, session):
        self.s = session
        self.sel = session.sel["tiktok"]

    def upload(self, path, meta):
        page = self.s.ctx.new_page()
        try:
            page.goto(self.sel["upload_url"], wait_until="domcontentloaded")
            _pause(2, 4)
            page.locator(self.sel["file_input"]).first.set_input_files(str(path))
            log.info("TikTok : vidéo envoyée, attente du traitement…")

            editor = page.locator(self.sel["caption"]).first
            editor.wait_for(state="visible", timeout=120_000)
            _pause()
            editor.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            for word in meta["tiktok_caption"].split(" "):
                page.keyboard.type(word, delay=random.randint(25, 70))
                if word.startswith("#"):
                    _pause(1.0, 1.6)          # laisse la suggestion de hashtag apparaître
                    page.keyboard.press("Escape")
                page.keyboard.type(" ")
            _pause()

            post = page.locator(self.sel["post_button"]).first
            deadline = time.time() + 600
            while time.time() < deadline:
                if post.is_visible() and post.is_enabled() and post.get_attribute("aria-disabled") != "true":
                    break
                time.sleep(3)
            else:
                raise RuntimeError("le bouton Publier ne s'est jamais activé")
            _pause(1.5, 3)
            post.click()

            # Fenêtre de confirmation éventuelle (« Publier maintenant »…)
            _pause(2, 3)
            for text in self.sel["confirm_texts"]:
                btn = page.locator(f"div[role='dialog'] button:has-text('{text}')")
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    break

            try:
                page.wait_for_url(f"**{self.sel['success_url_part']}**", timeout=90_000)
            except Exception:
                shot = self.s.screenshot(page, "tiktok-incertain")
                raise RuntimeError(f"publication non confirmée (capture : {shot})")
            return "navigateur"
        except Exception:
            self.s.screenshot(page, "tiktok-erreur")
            raise
        finally:
            page.close()


class BrowserYouTube:
    def __init__(self, session, privacy="public"):
        self.s = session
        self.sel = session.sel["youtube"]
        self.privacy = privacy

    def _fill(self, page, selector, text):
        box = page.locator(selector).first
        box.wait_for(state="visible")
        box.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        # insert_text : fiable avec les emojis et les accents (type() perdait des lignes)
        for line_no, line in enumerate(text.split("\n")):
            if line_no:
                page.keyboard.press("Enter")
            if line:
                page.keyboard.insert_text(line)
            time.sleep(random.uniform(0.05, 0.2))
        _pause()

    def upload(self, path, meta):
        page = self.s.ctx.new_page()
        try:
            page.goto(self.sel["upload_url"], wait_until="domcontentloaded")
            _pause(3, 5)
            if "accounts.google" in page.url:
                raise RuntimeError("non connecté à YouTube : réexporte tes cookies")
            page.locator(self.sel["file_input"]).first.set_input_files(str(path))
            log.info("YouTube : vidéo envoyée, remplissage des champs…")

            self._fill(page, self.sel["title"], meta["title"])
            self._fill(page, self.sel["description"], meta["description"])
            page.locator(self.sel["not_for_kids"]).first.click()
            _pause()

            for _ in range(3):  # Détails → Éléments vidéo → Vérifications → Visibilité
                page.locator(self.sel["next_button"]).first.click()
                _pause(1.5, 2.5)

            page.locator(self.sel["visibility"][self.privacy]).first.click()
            _pause()

            link = ""
            loc = page.locator(self.sel["video_link"]).first
            try:
                loc.wait_for(state="visible", timeout=60_000)
                link = loc.get_attribute("href") or loc.inner_text()
            except Exception:
                pass

            done = page.locator(self.sel["done_button"]).first
            deadline = time.time() + 900
            while time.time() < deadline:
                if done.is_enabled() and done.get_attribute("aria-disabled") != "true":
                    break
                time.sleep(5)
            else:
                raise RuntimeError("le bouton Publier n'est jamais devenu actif")
            done.click()
            _pause(4, 6)
            vid = link.rstrip("/").split("/")[-1] if link else "navigateur"
            return vid
        except Exception:
            self.s.screenshot(page, "youtube-erreur")
            raise
        finally:
            page.close()
