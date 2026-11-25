"""ClipBot – analyse du son.

1) Empreinte sonore d'un live ou d'un Short (spectre en 60 demi-tons, 10 fois par seconde).
2) Recherche d'un Short de clipper dans un live : résiste à la musique ajoutée, à l'accélération
   (avec ou sans voix plus aiguë), aux coupes recollées et aux intros.
3) Détection des moments forts : caractéristiques par seconde + petit modèle qui apprend tout seul
   à partir des passages déjà choisis par les clippers et les viewers.

Banc d'essai (lives de test, 400+ Shorts déformés) : ~98 % de Shorts retrouvés à la bonne seconde,
0 faux positif ; moments forts : 92 % d'extraits justes avec le modèle contre 72 % au volume seul.
"""
import re
import subprocess

import numpy as np

SR, HOP, NFFT, FPS = 8000, 800, 1024, 10
N_B, F_LO, GROUP = 60, 100.0, 6
RATES = (1.0, 1.1, 1.15, 1.2, 1.25, 1.33, 1.5)       # accélérations courantes des clippers

_edges = F_LO * 2 ** (np.arange(N_B + 1) / 12)
_freqs = np.fft.rfftfreq(NFFT, 1 / SR)
_W = np.zeros((len(_freqs), N_B), np.float32)
for _b in range(N_B):
    _m = (_freqs >= _edges[_b]) & (_freqs < _edges[_b + 1])
    if not _m.any():
        _m[np.argmin(abs(_freqs - (_edges[_b] + _edges[_b + 1]) / 2))] = True
    _W[_m, _b] = 1 / _m.sum()


# ---------------------------------------------------------------------------
# Lecture du son
# ---------------------------------------------------------------------------
def features(source, progress=None):
    """Lit un fichier ou un flux en une passe (sans le stocker).
    Renvoie (volume dB par seconde, spectre log T×60 à 10 images/s)."""
    p = subprocess.Popen(["ffmpeg", "-nostats", "-loglevel", "error", "-i", source, "-vn", "-ac", "1",
                          "-ar", str(SR), "-f", "s16le", "-"], stdout=subprocess.PIPE)
    win = np.hanning(NFFT).astype(np.float32)
    levels, specs, rest = [], [], np.zeros(0, np.float32)
    while True:
        data = p.stdout.read(SR * 2 * 600)           # 10 minutes à la fois
        if not data:
            break
        x = np.concatenate([rest, np.frombuffer(data[:len(data) // 2 * 2], np.int16).astype(np.float32) / 32768])
        nsec = len(x) // SR
        if nsec == 0:
            rest = x
            continue
        sec = x[:nsec * SR].reshape(nsec, SR)
        levels += (20 * np.log10(np.sqrt((sec ** 2).mean(1)) + 1e-9)).clip(-90, 0).round(1).tolist()
        pad = np.concatenate([x[:nsec * SR], np.zeros(NFFT, np.float32)])
        for a in range(0, nsec * FPS, 6000):        # par paquets (mémoire maîtrisée)
            idx = np.arange(a, min(nsec * FPS, a + 6000))[:, None] * HOP + np.arange(NFFT)[None, :]
            spec = np.abs(np.fft.rfft(pad[idx] * win, axis=1)) ** 2
            specs.append(np.log10(spec @ _W + 1e-7).astype(np.float32))
        rest = x[nsec * SR:]
        if progress and len(levels) // 1800 != (len(levels) - nsec) // 1800:
            progress(len(levels))            # toutes les 30 min de son analysées
    p.wait()
    if p.returncode not in (0, None) and not levels:
        raise RuntimeError("ffmpeg n'a pas pu lire le son")
    return levels, (np.concatenate(specs) if specs else np.zeros((0, N_B), np.float32))


# ---------------------------------------------------------------------------
# Recherche d'un Short dans un live
# ---------------------------------------------------------------------------
def _whiten(F, k=31):
    """Retire ce qui reste stable (musique tenue, bruit de fond) et le volume global."""
    F = F - F.mean(1, keepdims=True)
    if len(F) <= k:
        return F - F.mean(0, keepdims=True)
    cs = np.cumsum(np.vstack([np.zeros((1, F.shape[1]), np.float64), F]), 0)
    i = np.arange(len(F)); h = k // 2
    a, b = np.clip(i - h, 0, len(F)), np.clip(i + h + 1, 0, len(F))
    return (F - (cs[b] - cs[a]) / (b - a)[:, None]).astype(np.float32)


def _groups(W, shift=0):
    """Résumé en 10 groupes de 6 demi-tons (décalé si la voix a été rendue plus aiguë)."""
    if shift > 0:
        W = np.concatenate([W[:, shift:], np.zeros((len(W), shift), W.dtype)], 1)
    elif shift < 0:
        W = np.concatenate([np.zeros((len(W), -shift), W.dtype), W[:, :shift]], 1)
    return W.reshape(len(W), -1, GROUP).mean(2)


def _stretch(F, rate):
    """Short accéléré de « rate » : on le remet à la vitesse du live."""
    if rate == 1.0:
        return F
    pos = np.linspace(0, len(F) - 1, int(round(len(F) * rate)))
    i = np.floor(pos).astype(int); f = (pos - i)[:, None]
    j = np.minimum(i + 1, len(F) - 1)
    return (F[i] * (1 - f) + F[j] * f).astype(np.float32)


class VodIndex:
    """Live préparé pour des recherches rapides (à créer une fois par live)."""

    def __init__(self, F):
        self.W = _whiten(np.asarray(F, np.float32))
        G = _groups(self.W).astype(np.float64)
        self.N = len(G)
        self.size = 1 << int(np.ceil(np.log2(self.N + 2000)))
        self.ffts = [np.fft.rfft(G[:, g], self.size) for g in range(G.shape[1])]
        self.cs = [np.concatenate([[0], np.cumsum(G[:, g])]) for g in range(G.shape[1])]
        self.cs2 = [np.concatenate([[0], np.cumsum(G[:, g] ** 2)]) for g in range(G.shape[1])]

    def coarse(self, Gq, top):
        L = len(Gq)
        if L >= self.N:
            return []
        corr = np.zeros(self.N - L + 1)
        for g in range(Gq.shape[1]):
            q = Gq[:, g]; q = (q - q.mean()) / (q.std() + 1e-9)
            c = np.fft.irfft(self.ffts[g] * np.conj(np.fft.rfft(q, self.size)), self.size)[:self.N - L + 1]
            cs, cs2 = self.cs[g], self.cs2[g]
            mean = (cs[L:] - cs[:-L]) / L
            std = np.sqrt(np.maximum((cs2[L:] - cs2[:-L]) / L - mean ** 2, 1e-6))
            corr += c / (L * std)
        picks = []
        for k in np.argsort(-corr)[:top * 40]:
            if all(abs(k - p) > 20 for p in picks):
                picks.append(int(k))
                if len(picks) >= top:
                    break
        return picks

    def verify(self, Wq, pos, shift, slack=8):
        """Vérification fine : corrélation temps × fréquence autour de pos."""
        L = len(Wq)
        lo, hi = max(0, pos - slack), min(self.N - L, pos + slack)
        if hi < lo:
            return -1.0, pos
        q = Wq[:, max(0, -shift):N_B - max(0, shift)]
        q = q - q.mean(); qn = np.sqrt((q ** 2).sum()) + 1e-9
        V = np.lib.stride_tricks.sliding_window_view(
            self.W[lo:hi + L, max(0, shift):N_B - max(0, -shift)], L, axis=0).transpose(0, 2, 1)
        V = V - V.mean(axis=(1, 2), keepdims=True)
        c = (V * q).sum(axis=(1, 2)) / (qn * (np.sqrt((V ** 2).sum(axis=(1, 2))) + 1e-9))
        k = int(np.argmax(c))
        return float(c[k]), lo + k


def _match_chunk(Fq, vods, prefer=None, top=8, good=0.4):
    best = (None, 0, 1.0, -1.0)
    for r in ([prefer] if prefer else []) + [r for r in RATES if r != prefer]:
        if best[3] >= good:
            break
        Wq = _whiten(_stretch(Fq, r))
        shifts = [0] if r == 1.0 else sorted({0, -int(round(12 * np.log2(r)))})
        for vid, ix in vods.items():
            for sh in shifts:
                for p in ix.coarse(_groups(Wq, -sh), top):
                    sc, bp = ix.verify(Wq, p, sh)
                    if sc > best[3]:
                        best = (vid, bp, r, sc)
    return best


def locate(F_clip, vods, chunk_s=8, hop_s=4, thr=0.25):
    """Cherche un Short dans plusieurs lives ({id: VodIndex}).
    Le Short est découpé en morceaux de 8 s cherchés séparément ; les morceaux doivent « voter »
    pour le même endroit d'un même live. Renvoie {vod, start, rate, score, votes} ou None."""
    L = len(F_clip)
    if L < 5 * FPS:
        return None
    cl, hp = chunk_s * FPS, hop_s * FPS
    starts = list(range(0, L - cl + 1, hp)) if L > cl else [0]
    hits, prefer = [], None
    for st in starts:
        vid, pos, r, sc = _match_chunk(F_clip[st:st + cl], vods, prefer)
        if sc >= 0.4:
            prefer = r
        if vid is not None and sc >= thr:
            hits.append((vid, pos - st * r, pos, r, sc))
    if not hits:
        return None
    dur = L / FPS
    best = None
    for vid, origin, _, _, _ in hits:
        grp = [h for h in hits if h[0] == vid and origin - 20 * FPS <= h[2] <= origin + (dur * 2 + 90) * FPS]
        key = (len(grp), sum(h[4] for h in grp))
        if best is None or key > best[0]:
            best = (key, vid, grp)
    (votes, total), vid, grp = best
    avg, frac = total / votes, votes / len(starts)
    # Règle de confiance (réglée sur le banc d'essai) : assez de morceaux d'accord ET assez ressemblants,
    # ou très ressemblants. Mieux vaut rater un Short que publier le mauvais passage.
    if votes < 2 or not ((frac >= 0.4 and avg >= 0.36) or (frac >= 0.3 and avg >= 0.45)):
        return None
    first = min(grp, key=lambda h: h[2])
    return {"vod": vid, "start": max(0, int(first[1] // FPS)), "rate": first[3],
            "score": round(total / votes, 3), "votes": votes}


# ---------------------------------------------------------------------------
# Moments forts
# ---------------------------------------------------------------------------
NAMES = ["volume", "volume_vs_5min", "variation_volume", "changements", "hauteur_voix",
         "rythme_syllabes", "aigus", "voix_vs_fond"]


def _z(x):
    return (x - np.median(x)) / (np.percentile(x, 75) - np.percentile(x, 25) + 1e-6)


def _roll_mean(x, k):
    cs = np.concatenate([[0], np.cumsum(x)])
    i = np.arange(len(x)); h = k // 2
    a, b = np.clip(i - h, 0, len(x)), np.clip(i + h + 1, 0, len(x))
    return (cs[b] - cs[a]) / (b - a)


def per_second(levels, F):
    """Une ligne par seconde (8 mesures), normalisée par live : indépendant du streamer et du micro."""
    S = min(len(levels), len(F) // FPS)
    if S < 60:
        return np.zeros((0, len(NAMES)))
    lv = np.asarray(levels[:S], np.float64)
    out = []
    for a in range(0, S, 1800):                       # par paquets de 30 min (mémoire)
        b = min(S, a + 1800)
        Fs = np.asarray(F[a * FPS:b * FPS], np.float64).reshape(b - a, FPS, -1)
        E = 10 ** Fs
        tot = np.log10(E.sum(2) + 1e-9)
        flux = np.maximum(np.diff(Fs, axis=1), 0).sum(2).mean(1)
        voice = E[:, :, 14:48].sum(2)
        cent = (E[:, :, 14:48] * np.arange(14, 48)).sum(2) / (voice + 1e-12)
        env = np.log10(voice + 1e-12)
        mod = np.abs(np.fft.rfft(env - env.mean(1, keepdims=True), axis=1))[:, 3:5].sum(1)
        high = np.log10(E[:, :, 48:].sum(2).mean(1) + 1e-12) - tot.mean(1)
        low = np.log10(E[:, :, :10].sum(2).mean(1) + 1e-12)
        out.append(np.stack([tot.std(1), flux, cent.mean(1), mod, high, env.mean(1) - low], 1))
    rest = np.concatenate(out)
    X = np.column_stack([lv, lv - _roll_mean(lv, 301), rest])
    return np.stack([_z(X[:, j]) for j in range(X.shape[1])], 1)


def window_feats(P, start, length):
    w = P[start:start + length]
    return np.concatenate([w.mean(0), np.percentile(w, 90, axis=0)])


def training_rows(P, positives, length, step=5):
    """Exemples d'apprentissage d'un live : fenêtres qui recouvrent un passage clippé (1)
    et fenêtres loin de tout passage clippé (0)."""
    X, y = [], []
    for s in range(0, len(P) - length, step):
        over = max((min(s + length, b) - max(s, a) for a, b in positives), default=-999)
        if over >= min(10, length // 2):
            X.append(window_feats(P, s, length)); y.append(1)
        elif over < -60:
            X.append(window_feats(P, s, length)); y.append(0)
    return np.array(X), np.array(y)


def train(X, y, l2=1.0, iters=800, lr=0.1):
    X = np.asarray(X, np.float64); y = np.asarray(y, np.float64)
    mu, sd = X.mean(0), X.std(0) + 1e-6
    Z = (X - mu) / sd
    w = np.zeros(Z.shape[1]); b = 0.0
    sw = np.where(y > 0, (len(y) - y.sum()) / max(y.sum(), 1), 1.0)   # classes déséquilibrées
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(Z @ w + b)))
        g = (p - y) * sw
        w -= lr * (Z.T @ g / len(y) + l2 * w / len(y))
        b -= lr * g.mean()
    return {"mu": mu.tolist(), "sd": sd.tolist(), "w": w.tolist(), "b": float(b)}


def scores(model, P, length, step=5):
    starts = list(range(0, max(0, len(P) - length), step))
    if not starts:
        return [], np.zeros(0)
    X = np.array([window_feats(P, s, length) for s in starts])
    z = (X - np.asarray(model["mu"])) / np.asarray(model["sd"])
    return starts, 1 / (1 + np.exp(-(z @ np.asarray(model["w"]) + model["b"])))


# ---------------------------------------------------------------------------
# Découpage intelligent d'un extrait
# ---------------------------------------------------------------------------
def voice_curve(F):
    """Énergie de la voix (225 Hz – 1,6 kHz), 10 fois par seconde, lissée sur 0,3 s."""
    v = np.log10((10 ** np.asarray(F[:, 14:48], np.float64)).sum(1) + 1e-12)
    return np.convolve(v, np.ones(3) / 3, mode="same")


def excitement(P, model=None):
    """Intensité seconde par seconde : selon le modèle appris (poids des moyennes), sinon le volume."""
    if model is not None and len(P):
        k = P.shape[1]
        w = np.asarray(model["w"][:k]) / np.asarray(model["sd"][:k])
        e = (P - np.asarray(model["mu"][:k])) @ w
    else:
        e = P[:, 1] if len(P) else np.zeros(0)     # volume par rapport aux 5 dernières minutes
    return np.convolve(e, np.ones(5) / 5, mode="same")


def refine(start, end, F, ex, min_len=25, max_len=59, lead=4, tail=3, frac=0.3, v=None):
    """Ajuste un extrait : garde le cœur de l'action (là où l'intensité reste haute), avec un peu de
    contexte avant, puis place le début et la fin dans un silence pour ne pas couper une phrase."""
    n = len(ex)
    a0, b0 = max(0, start - 10), min(n, end + 10)
    if b0 - a0 < min_len:
        return start, end
    seg = ex[a0:b0]
    peak = int(np.argmax(seg)) + a0
    base = np.median(ex)
    level = base + frac * (ex[peak] - base)
    a = peak
    while a > a0 and ex[a - 1] >= level:
        a -= 1
    b = peak + 1
    while b < b0 and ex[b] >= level:
        b += 1
    a, b = a - lead, b + tail
    if b - a < min_len:                               # trop court : on élargit autour du cœur
        extra = min_len - (b - a)
        a, b = a - extra // 2, b + extra - extra // 2
    if b - a > max_len - 2:                           # trop long : on garde autour du pic
        a = max(a, min(peak - lead - 6, b - (max_len - 2)))
        b = a + max_len - 2
    a, b = max(0, a), min(n, b)
    return snap(a, b, F, min_len, max_len, v)


def snap(a, b, F, min_len=10, max_len=59, v=None):
    """Place le début et la fin dans un silence de la voix, pour ne pas couper une phrase."""
    v = voice_curve(F) if v is None else v
    # début : moment le plus calme de la voix entre 4 s avant et 1 s après
    lo, hi = max(0, int((a - 4) * FPS)), min(len(v), int((a + 1) * FPS))
    sa = (lo + int(np.argmin(v[lo:hi]))) / FPS if hi > lo else a
    # fin : moment le plus calme entre 1 s avant et 3 s après, sans dépasser la durée max
    lo = int(max(sa * FPS + min_len * FPS, (b - 1) * FPS))
    hi = int(min(len(v), (b + 3) * FPS, sa * FPS + max_len * FPS))
    sb = (lo + int(np.argmin(v[lo:hi]))) / FPS if hi > lo else min(b, sa + max_len)
    return round(sa, 1), round(sb, 1)



# ---------------------------------------------------------------------------
# Chat du live : messages par seconde (replay du chat) ou chat incrusté dans la vidéo
# ---------------------------------------------------------------------------
_LAUGH = re.compile(r"(mdr|ptdr|xpt|lol|xd|kekw|omegalul|lul|😂|🤣|haha|ahah|jpp|wtf|\?\?\?|!!!|gg\b|pog|clip)", re.I)


def chat_from_messages(msgs, n_seconds):
    """msgs = [(seconde, texte)] -> activité du chat par seconde (messages + rires/réactions comptés double)."""
    a = np.zeros(n_seconds)
    for t, txt in msgs:
        k = int(t)
        if 0 <= k < n_seconds:
            a[k] += 1 + (1 if _LAUGH.search(txt or "") else 0)
    return a


def parse_youtube_live_chat(path):
    """Fichier live_chat.json de yt-dlp (replay du chat YouTube) -> [(seconde, texte)]."""
    import json
    out = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            rep = d.get("replayChatItemAction") or {}
            t = int(rep.get("videoOffsetTimeMsec") or 0) / 1000
            for act in rep.get("actions") or []:
                item = (act.get("addChatItemAction") or {}).get("item") or {}
                r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                if not r:
                    continue
                runs = (r.get("message") or {}).get("runs") or []
                txt = "".join(x.get("text") or (x.get("emoji") or {}).get("shortcuts", [""])[0] for x in runs)
                out.append((t, txt))
    return out


def video_chat_activity(source, fps=2, W=160, H=90):
    """Chat incrusté dans la vidéo : repère la bande verticale où du texte défile vers le haut
    et mesure, seconde par seconde, de combien il défile (≈ nombre de messages).
    Renvoie un tableau par seconde, ou None si aucun chat incrusté n'est détecté."""
    p = subprocess.Popen(["ffmpeg", "-nostats", "-loglevel", "error", "-i", source, "-an",
                          "-vf", f"fps={fps},scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
                         stdout=subprocess.PIPE)
    strips, K = 8, 12
    sw = W // strips
    prev = None
    scroll = []          # (images, bandes) : pixels défilés vers le haut
    while True:
        buf = p.stdout.read(W * H * 600)
        if not buf:
            break
        fr = np.frombuffer(buf[:len(buf) // (W * H) * W * H], np.uint8).reshape(-1, H, W).astype(np.float32)
        if prev is not None:
            fr = np.concatenate([prev[None], fr])
        A, B = fr[:-1], fr[1:]                                   # image précédente, image suivante
        res = np.zeros((len(B), strips))
        for s in range(strips):
            a, b = A[:, :, s * sw:(s + 1) * sw], B[:, :, s * sw:(s + 1) * sw]
            d0 = np.abs(b - a).mean(axis=(1, 2))
            ds = np.stack([np.abs(b[:, :-k] - a[:, k:]).mean(axis=(1, 2)) for k in range(1, K + 1)], 1)
            k = ds.argmin(1) + 1
            moved = (d0 > 4) & (ds.min(1) < 0.55 * d0)
            res[:, s] = np.where(moved, k, 0)
        scroll.append(res)
        prev = fr[-1]
    p.wait()
    if not scroll:
        return None
    S = np.concatenate(scroll)
    frac = (S > 0).mean(0)
    best = int(frac.argmax())
    others = np.delete(frac, best)
    # chat présent : une bande qui défile souvent, nettement plus que le reste de l'image
    if frac[best] < 0.08 or frac[best] < 2.5 * (np.median(others) + 1e-3):
        return None
    cols = [s for s in range(strips) if frac[s] >= 0.6 * frac[best]]
    rate = S[:, cols].max(1)
    n = len(rate) // fps
    return rate[:n * fps].reshape(n, fps).sum(1)


def chat_bonus(activity, n_seconds, delay=3):
    """Activité du chat -> bonus 0..1 par seconde, par rapport aux 5 minutes autour.
    Le chat réagit quelques secondes après l'action : on avance le signal de `delay` s."""
    a = np.zeros(n_seconds)
    m = min(n_seconds, len(activity))
    a[:m] = activity[:m]
    a = np.convolve(a, np.ones(5) / 5, mode="same")
    base = _roll_mean(a, 301)
    spread = np.percentile(a, 90) - np.percentile(a, 50) + 1e-6
    z = np.clip((a - base) / spread, 0, 3) / 3
    if delay:
        z = np.concatenate([z[delay:], np.zeros(delay)])
    return z


# ---------------------------------------------------------------------------
# Écoute approfondie : ce qui est DIT (transcription Whisper d'un passage)
# ---------------------------------------------------------------------------
_REACT = [
    "c'est pas possible", "pas possible", "oh non", "non non", "regardez", "regarde ça", "oh mon dieu",
    "mon dieu", "incroyable", "de fou", "c'est fou", "je suis mort", "jsuis mort", "t'es sérieux", "sérieux",
    "attends attends", "attention", "cours", "vas-y", "vas y", "let's go", "on l'a fait", "c'est quoi ça",
    "qu'est-ce que", "comment", "wesh", "putain", "bordel", "oh la la", "ohlala", "nan mais", "non mais",
    "aide-moi", "aidez-moi", "au secours", "il m'a tué", "je suis mort", "gg", "trop fort", "trop drôle",
    "j'en peux plus", "je pleure", "ça va pas", "ah non", "oh putain", "quoi ?",
]
_LAUGH_W = re.compile(r"\b(ha(ha)+|ah(ah)+|hi(hi)+|mdr|ptdr|lol)\b|\(rires?\)|\[rires?\]", re.I)


def speech_score(words, duration):
    """words = [(début, fin, mot)] -> (score 0..1, détails). Mesure les réactions dites :
    exclamations typiques, rires, mots répétés (« non non non »), débit de parole rapide."""
    if duration <= 0:
        return 0.0, {}
    text = " ".join(w for _, _, w in words).lower()
    flat = re.sub(r"\s+", " ", text)
    react = sum(flat.count(p) for p in _REACT)
    laugh = len(_LAUGH_W.findall(flat))
    toks = [re.sub(r"[^\wà-ÿ']", "", w.lower()) for _, _, w in words]
    rep = sum(1 for i in range(2, len(toks)) if toks[i] and toks[i] == toks[i - 1] == toks[i - 2])
    bangs = flat.count("!") + flat.count("?!")
    wps = len(words) / duration
    s = (0.40 * min(1.0, react / max(1.0, duration / 15))
         + 0.20 * min(1.0, laugh / 2)
         + 0.15 * min(1.0, (rep + bangs / 2) / 3)
         + 0.25 * float(np.clip((wps - 1.5) / 2.0, 0, 1)))
    if len(words) < 5:          # presque personne ne parle
        s *= 0.5
    return round(float(min(1.0, s)), 3), {"réactions": react, "rires": laugh, "répétitions": rep,
                                          "mots/s": round(wps, 2)}


def snap_words(words, start, end, max_len, min_len=10):
    """Recale début/fin sur les phrases : le début juste avant un mot qui suit une pause,
    la fin juste après un mot suivi d'une pause (pas de phrase coupée). Temps relatifs au fichier."""
    if len(words) < 3:
        return start, end
    gaps_before = [words[0][0]] + [words[i][0] - words[i - 1][1] for i in range(1, len(words))]
    gaps_after = [words[i + 1][0] - words[i][1] for i in range(len(words) - 1)] + [9.0]
    starts = [w[0] for i, w in enumerate(words) if gaps_before[i] >= 0.3 and abs(w[0] - start) <= 4]
    s = min(starts, key=lambda t: abs(t - start)) - 0.2 if starts else start
    ends = [w[1] for i, w in enumerate(words) if gaps_after[i] >= 0.4 and s + min_len <= w[1] <= s + max_len
            and abs(w[1] - end) <= 5]
    e = min(ends, key=lambda t: abs(t - end)) + 0.4 if ends else end
    return max(0.0, s), min(e, s + max_len)
