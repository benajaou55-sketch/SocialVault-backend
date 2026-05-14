from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yt_dlp
import uvicorn
import httpx
import asyncio
import re
import os
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Thread pool dédié à yt-dlp (bloquant) ─────────────────────────────────────
executor = ThreadPoolExecutor(max_workers=4)

# ── Cache en mémoire simple (url → résultat, TTL 10 min) ──────────────────────
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 600

def cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < CACHE_TTL:
        return entry[1]
    _cache.pop(key, None)
    return None

def cache_set(key: str, value: dict):
    if value.get("success"):
        _cache[key] = (time.time(), value)

# ── Modèles ───────────────────────────────────────────────────────────────────
class ResolveRequest(BaseModel):
    url: str
    cookies: str = ""
    platform_hint: str = ""

# ── Fichiers cookies ──────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTAGRAM_COOKIES = os.path.join(BASE_DIR, "www.instagram.com_cookies.txt")
FACEBOOK_COOKIES  = os.path.join(BASE_DIR, "web.facebook.com_cookies.txt")
TIKTOK_COOKIES    = os.path.join(BASE_DIR, "www.tiktok.com_cookies.txt")

def get_cookie_file(platform):
    m = {"instagram": INSTAGRAM_COOKIES, "facebook": FACEBOOK_COOKIES, "tiktok": TIKTOK_COOKIES}
    p = m.get(platform)
    return p if p and os.path.exists(p) else None

def cookies_to_tempfile(cookies_str, platform):
    domain = {"instagram": ".instagram.com", "facebook": ".facebook.com"}.get(platform, f".{platform}.com")
    lines = ["# Netscape HTTP Cookie File\n"]
    for pair in cookies_str.split(";"):
        pair = pair.strip()
        if "=" in pair:
            n, _, v = pair.partition("=")
            n, v = n.strip(), v.strip()
            if n:
                lines.append(f"{domain}\tTRUE\t/\tTRUE\t2147483647\t{n}\t{v}\n")
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, prefix="sv_")
    tmp.writelines(lines)
    tmp.close()
    return tmp.name

def clean_instagram_url(url):
    url = re.sub(r'[?&]igsh=[^&]*', '', url)
    url = re.sub(r'[?&]img_index=[^&]*', '', url)
    url = re.sub(r'[?&]utm_source=[^&]*', '', url)
    url = re.sub(r'[?&]utm_medium=[^&]*', '', url)
    url = url.rstrip('?&')
    return url

# ── Wrapper : exécute yt-dlp dans le thread pool ──────────────────────────────
async def run_yt_dlp(opts: dict, url: str) -> dict:
    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _extract)

# ── Helper : première tâche qui réussit parmi plusieurs ───────────────────────
async def first_success(*coros):
    """Lance toutes les coroutines en parallèle, retourne le premier résultat
    avec success=True. Retourne le dernier résultat si aucun ne réussit."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    last = {"success": False, "error": "Aucune source disponible"}
    pending = set(tasks)
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            try:
                r = t.result()
                if r and r.get("success"):
                    for p in pending:
                        p.cancel()
                    return r
                last = r or last
            except Exception:
                pass
    return last

# ── Routes utilitaires ────────────────────────────────────────────────────────
@app.get("/")
def root(): return {"status": "SocialVault Backend running"}

@app.get("/health")
def health(): return {"status": "ok"}

@app.post("/set_cookies")
async def set_cookies(request: Request):
    try:
        body = await request.json()
        platform, cookies_str = body.get("platform", ""), body.get("cookies", "")
        if not platform or not cookies_str:
            return {"success": False, "error": "Données manquantes"}
        file_map = {"instagram": INSTAGRAM_COOKIES, "facebook": FACEBOOK_COOKIES}
        cf = file_map.get(platform)
        if not cf:
            return {"success": False, "error": "Plateforme non supportée"}
        domain = f".{platform}.com"
        lines = ["# Netscape HTTP Cookie File\n"]
        for pair in cookies_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                n, _, v = pair.partition("=")
                lines.append(f"{domain}\tTRUE\t/\tTRUE\t2147483647\t{n.strip()}\t{v.strip()}\n")
        with open(cf, "w") as f:
            f.writelines(lines)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)[:100]}

@app.get("/download")
async def download_video(url: str):
    async def stream():
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                async for chunk in r.aiter_bytes(8192):
                    yield chunk
    return StreamingResponse(stream(), media_type="video/mp4",
                             headers={"Content-Disposition": "attachment; filename=video.mp4"})

# ── TikTok : résolution des liens courts (vt / vm / m.tiktok.com) ─────────────
# FIX #1 : expand_tiktok_short_url est maintenant APPELÉ dans resolve_tiktok
# (dans l'ancienne version, la fonction existait mais n'était jamais utilisée)
async def expand_tiktok_short_url(url: str) -> str:
    """
    Résout les liens courts vt.tiktok.com / vm.tiktok.com / m.tiktok.com
    vers l'URL canonique tiktok.com/@user/video/ID en suivant les redirections.

    Variantes d'URL gérées :
      - vt.tiktok.com/XXXXX/       (Samsung, appli mobile)
      - vm.tiktok.com/XXXXX/       (partage web)
      - m.tiktok.com/v/XXXXX.html  (ancienne appli)
      - www.tiktok.com/t/XXXXX/    (nouveau format mobile 2024)
    """
    short_domains = ["vt.tiktok.com", "vm.tiktok.com", "m.tiktok.com"]
    mobile_share  = re.search(r'tiktok\.com/t/([A-Za-z0-9]+)', url)

    if not any(d in url for d in short_domains) and not mobile_share:
        return url  # déjà un lien long → rien à faire

    # User-Agent mobile Samsung pour maximiser les chances de redirection
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-S928B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Mobile Safari/537.36 TikTok/34.2.3"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }

    # Tentative 1 : HEAD (rapide, ne télécharge pas le contenu)
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True, headers=headers) as c:
            r = await c.head(url)
            resolved = str(r.url)
            if "tiktok.com" in resolved and "/video/" in resolved:
                return resolved.split("?")[0].rstrip("/")
    except Exception:
        pass

    # Tentative 2 : GET (plus lent mais plus fiable, suit les meta-refresh)
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as c:
            r = await c.get(url)
            resolved = str(r.url)
            if "tiktok.com" in resolved and "/video/" in resolved:
                return resolved.split("?")[0].rstrip("/")
            # Chercher l'URL canonique dans le HTML (page intermédiaire JS)
            for pattern in [
                r'https://www\.tiktok\.com/@[^/\s"\']+/video/\d+',
                r'"canonical"[^>]+href="(https://www\.tiktok\.com/@[^"]+/video/\d+)"',
                r'itemId["\s]*:["\s]*"?(\d{15,20})"?',
            ]:
                m = re.search(pattern, r.text)
                if m:
                    if "/video/" in m.group(0):
                        return m.group(0).split("?")[0]
                    # On a un itemId mais pas l'URL complète → on ne peut pas reconstruire
                    # sans le @username, on laisse tomber
    except Exception:
        pass

    return url  # fallback : URL originale, yt-dlp tentera quand même


def _is_short_tiktok(url: str) -> bool:
    return bool(
        any(d in url for d in ["vt.tiktok.com", "vm.tiktok.com", "m.tiktok.com"])
        or re.search(r'tiktok\.com/t/[A-Za-z0-9]+', url)
    )

# ── TikTok ────────────────────────────────────────────────────────────────────
async def _verify_video_url(url: str) -> bool:
    """
    Vérifie qu'une URL pointe vers une vraie vidéo (pas un fichier audio).
    tikwm retourne parfois hdplay = fichier M4A (audio only) déguisé en .mp4.
    """
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
            r = await c.head(url, headers={"User-Agent": "Mozilla/5.0"})
        ct = r.headers.get("content-type", "").lower()
        cl = int(r.headers.get("content-length", "0") or "0")
        is_audio_only = any(x in ct for x in ["audio/", "application/octet-stream"])
        too_small = 0 < cl < 500_000
        return not (is_audio_only or too_small)
    except Exception:
        return True  # optimiste en cas d'erreur réseau

async def _tiktok_tikwm(url: str) -> dict:
    """API tikwm — vérifie que hdplay est une vraie vidéo."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            data = (await c.get(f"https://tikwm.com/api/?url={url}&hd=1")).json()
        if data.get("code") == 0:
            vd = data.get("data", {})

            # Cas diaporama (images)
            imgs = vd.get("images", [])
            if imgs:
                return {
                    "success": True, "direct_url": imgs[0], "title": vd.get("title", "TikTok"),
                    "thumbnail": imgs[0], "duration": 0, "platform": "tiktok",
                    "ext": "jpg", "is_image": True, "all_images": imgs
                }

            hdplay = vd.get("hdplay", "")
            play   = vd.get("play", "")
            title  = vd.get("title", "TikTok")
            cover  = vd.get("cover", "")
            dur    = vd.get("duration", 0)

            if hdplay and await _verify_video_url(hdplay):
                return {
                    "success": True, "direct_url": hdplay, "title": title,
                    "thumbnail": cover, "duration": dur, "platform": "tiktok",
                    "ext": "mp4", "is_image": False, "all_images": []
                }
            if play and await _verify_video_url(play):
                return {
                    "success": True, "direct_url": play, "title": title,
                    "thumbnail": cover, "duration": dur, "platform": "tiktok",
                    "ext": "mp4", "is_image": False, "all_images": []
                }
            if play:
                return {
                    "success": True, "direct_url": play, "title": title,
                    "thumbnail": cover, "duration": dur, "platform": "tiktok",
                    "ext": "mp4", "is_image": False, "all_images": []
                }
    except Exception:
        pass
    return {"success": False, "error": "tikwm failed"}

async def _tiktok_tikmate(url: str) -> dict:
    """API tikmate — fallback parallèle."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.post("https://tikmate.online/api/", data={"url": url},
                             headers={"User-Agent": "Mozilla/5.0"})
            data = r.json()
        videos = data.get("video", [])
        if videos:
            best = videos[0].get("url", "")
            if best:
                return {
                    "success": True, "direct_url": best, "title": data.get("title", "TikTok"),
                    "thumbnail": data.get("cover", ""), "duration": 0,
                    "platform": "tiktok", "ext": "mp4", "is_image": False, "all_images": []
                }
    except Exception:
        pass
    return {"success": False, "error": "tikmate failed"}

async def _tiktok_ytdlp(url: str) -> dict:
    """yt-dlp comme dernier recours."""
    try:
        opts = {
            "quiet": True, "skip_download": True, "format": "best",
            "socket_timeout": 10, "retries": 2,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 14; SM-S928B) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Mobile Safari/537.36"
                )
            }
        }
        cf = get_cookie_file("tiktok")
        if cf:
            opts["cookiefile"] = cf
        info = await run_yt_dlp(opts, url)
        du = info.get("url") or (info.get("formats") or [{}])[-1].get("url", "")
        if du:
            ext = info.get("ext", "mp4")
            return {
                "success": True, "direct_url": du, "title": info.get("title", "TikTok"),
                "thumbnail": info.get("thumbnail", ""), "duration": int(info.get("duration") or 0),
                "platform": "tiktok", "ext": ext,
                "is_image": ext in ["jpg", "jpeg", "png", "webp"], "all_images": []
            }
    except Exception:
        pass
    return {"success": False, "error": "yt-dlp tiktok failed"}


async def resolve_tiktok(url: str) -> dict:
    """
    FIX #1 : expand_tiktok_short_url est maintenant appelé AVANT tikwm/tikmate.
    FIX #2 : Les liens www.tiktok.com/t/XXXXX (format mobile 2024) sont détectés
             et résolus comme des liens courts.
    Stratégie :
      - Lien court → résolution HTTP d'abord, puis yt-dlp sur l'URL longue
      - Lien long  → tikwm + tikmate en parallèle, yt-dlp en fallback
    """
    cached = cache_get(url)
    if cached:
        return cached

    if _is_short_tiktok(url):
        # Étape 1 : Résoudre l'URL courte → URL canonique
        resolved_url = await expand_tiktok_short_url(url)

        # Étape 2 : Si résolution réussie, utiliser tikwm + yt-dlp sur l'URL longue
        if resolved_url != url and "/video/" in resolved_url:
            result = await first_success(
                _tiktok_tikwm(resolved_url),
                _tiktok_tikmate(resolved_url)
            )
            if not result.get("success"):
                result = await _tiktok_ytdlp(resolved_url)
        else:
            # Résolution échouée → yt-dlp directement (gère les redirections en interne)
            result = await _tiktok_ytdlp(url)
            if not result.get("success"):
                result = await first_success(
                    _tiktok_tikwm(url),
                    _tiktok_tikmate(url)
                )
    else:
        result = await first_success(_tiktok_tikwm(url), _tiktok_tikmate(url))
        if not result.get("success"):
            result = await _tiktok_ytdlp(url)

    cache_set(url, result)
    return result


async def resolve_tiktok_story(url: str) -> dict:
    cached = cache_get(url)
    if cached:
        return cached

    if _is_short_tiktok(url):
        resolved_url = await expand_tiktok_short_url(url)
        target = resolved_url if resolved_url != url else url
        result = await _tiktok_ytdlp(target)
        if not result.get("success"):
            result = await _tiktok_tikwm(target)
    else:
        result = await first_success(_tiktok_tikwm(url), _tiktok_ytdlp(url))

    if result.get("success"):
        cache_set(url, result)
        return result
    return {"success": False, "error": "Impossible de télécharger cette story TikTok."}

# ── Twitter / X ───────────────────────────────────────────────────────────────
def _best_twitter_thumbnail(t: dict, tid: str, video_url: str = "") -> str:
    m = t.get("media", {})
    for v in m.get("videos", []):
        th = v.get("thumbnail_url", "")
        if th:
            return th
    th = t.get("thumbnail_url", "")
    if th:
        return th
    for p in m.get("photos", []):
        ph = p.get("url", "")
        if ph:
            return ph
    if tid:
        return f"https://pbs.twimg.com/tweet_video_thumb/{tid}.jpg"
    return ""

async def resolve_twitter(url):
    cached = cache_get(url)
    if cached:
        return cached

    tid = url.rstrip("/").split("/")[-1].split("?")[0]

    async def fetch_api(api_url):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                return (await c.get(api_url, headers={"User-Agent": "Mozilla/5.0"})).json()
        except Exception:
            return None

    results = await asyncio.gather(
        fetch_api(f"https://api.fxtwitter.com/status/{tid}"),
        fetch_api(f"https://api.vxtwitter.com/status/{tid}")
    )

    for data in results:
        if not data:
            continue
        t = data.get("tweet", data)
        m = t.get("media", {})

        for v in m.get("videos", []):
            if v.get("url"):
                thumbnail = v.get("thumbnail_url") or _best_twitter_thumbnail(t, tid, v["url"])
                result = {
                    "success": True, "direct_url": v["url"], "title": t.get("text", "X")[:100],
                    "thumbnail": thumbnail, "duration": 0, "platform": "twitter",
                    "ext": "mp4", "is_image": False, "all_images": []
                }
                cache_set(url, result)
                return result

        for g in m.get("gifs", []):
            if g.get("url"):
                result = {
                    "success": True, "direct_url": g["url"], "title": t.get("text", "X")[:100],
                    "thumbnail": g.get("thumbnail_url") or _best_twitter_thumbnail(t, tid),
                    "duration": 0, "platform": "twitter", "ext": "mp4",
                    "is_image": False, "all_images": []
                }
                cache_set(url, result)
                return result

        for p in m.get("photos", []):
            if p.get("url"):
                result = {
                    "success": True, "direct_url": p["url"], "title": t.get("text", "X")[:100],
                    "thumbnail": p["url"], "duration": 0, "platform": "twitter",
                    "ext": "jpg", "is_image": True, "all_images": []
                }
                cache_set(url, result)
                return result

        for me in data.get("media_extended", []):
            if me.get("type") in ["video", "gif"] and me.get("url"):
                result = {
                    "success": True, "direct_url": me["url"], "title": data.get("text", "X")[:100],
                    "thumbnail": me.get("thumbnail_url") or _best_twitter_thumbnail(t, tid),
                    "duration": 0, "platform": "twitter", "ext": "mp4",
                    "is_image": False, "all_images": []
                }
                cache_set(url, result)
                return result
            if me.get("type") == "image" and me.get("url"):
                result = {
                    "success": True, "direct_url": me["url"], "title": data.get("text", "X")[:100],
                    "thumbnail": me["url"], "duration": 0, "platform": "twitter",
                    "ext": "jpg", "is_image": True, "all_images": []
                }
                cache_set(url, result)
                return result

    return {"success": False, "error": "Ce tweet ne contient pas de média ou est privé."}

# ── Instagram ─────────────────────────────────────────────────────────────────
async def _instagram_snapinsta(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
            tr = await c.get("https://snapinsta.app/", headers={"User-Agent": "Mozilla/5.0"})
            m = re.search(r'name="_token"\s+value="([^"]+)"', tr.text)
            if not m:
                return {"success": False, "error": "snapinsta token failed"}
            dr = await c.post(
                "https://snapinsta.app/action.php",
                data={"url": url, "_token": m.group(1)},
                headers={
                    "User-Agent": "Mozilla/5.0", "Referer": "https://snapinsta.app/",
                    "X-Requested-With": "XMLHttpRequest"
                }
            )
            if dr.status_code == 200:
                try:
                    html = dr.json().get("data", "") if isinstance(dr.json(), dict) else str(dr.json())
                except Exception:
                    html = dr.text
                vids = re.findall(r'https://[^"\']+\.mp4[^"\']*', html)
                imgs = re.findall(r'https://[^"\']+\.jpg[^"\']*', html)
                if vids:
                    return {
                        "success": True, "direct_url": vids[0], "title": "Video Instagram",
                        "thumbnail": imgs[0] if imgs else "", "duration": 0,
                        "platform": "instagram", "ext": "mp4", "is_image": False,
                        "all_images": [], "carousel_items": []
                    }
                if imgs:
                    return {
                        "success": True, "direct_url": imgs[0], "title": "Photo Instagram",
                        "thumbnail": imgs[0], "duration": 0, "platform": "instagram",
                        "ext": "jpg", "is_image": True, "all_images": [], "carousel_items": []
                    }
    except Exception:
        pass
    return {"success": False, "error": "snapinsta failed"}

async def _instagram_instavideosave(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
            r = await c.post(
                "https://instavideosave.net/", data={"url": url},
                headers={
                    "User-Agent": "Mozilla/5.0", "Referer": "https://instavideosave.net/",
                    "Content-Type": "application/x-www-form-urlencoded"
                }
            )
            vids = re.findall(r'https://[^"\']+\.mp4[^"\']*', r.text)
            imgs = re.findall(r'https://[^"\']+\.jpg[^"\']*', r.text)
            if vids:
                return {
                    "success": True, "direct_url": vids[0], "title": "Video Instagram",
                    "thumbnail": imgs[0] if imgs else "", "duration": 0,
                    "platform": "instagram", "ext": "mp4", "is_image": False,
                    "all_images": [], "carousel_items": []
                }
            if imgs:
                return {
                    "success": True, "direct_url": imgs[0], "title": "Photo Instagram",
                    "thumbnail": imgs[0], "duration": 0, "platform": "instagram",
                    "ext": "jpg", "is_image": True, "all_images": [], "carousel_items": []
                }
    except Exception:
        pass
    return {"success": False, "error": "instavideosave failed"}

async def _instagram_ytdlp(url: str, extra_cookies: str = "") -> dict:
    tmp = None
    try:
        if extra_cookies:
            tmp = cookies_to_tempfile(extra_cookies, "instagram")
        cf = tmp or get_cookie_file("instagram")
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "format": "best", "socket_timeout": 8, "retries": 1,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605.1.15"
            }
        }
        if cf:
            opts["cookiefile"] = cf

        info = await run_yt_dlp(opts, url)

        if info.get("_type") == "playlist":
            entries = [e for e in info.get("entries", []) if e]
            valid = []
            for e in entries:
                du = e.get("url", "")
                if not du and e.get("formats"):
                    fmts = [f for f in e["formats"] if f.get("url")]
                    du = fmts[-1]["url"] if fmts else ""
                if du:
                    ext = e.get("ext", "jpg")
                    valid.append({
                        "url": du, "is_video": ext == "mp4",
                        "ext": ext, "thumbnail": e.get("thumbnail", "")
                    })
            if not valid:
                raise Exception("no entries")
            if len(valid) == 1:
                item = valid[0]
                return {
                    "success": True, "direct_url": item["url"],
                    "title": info.get("title", "Instagram"),
                    "thumbnail": item["thumbnail"], "duration": 0,
                    "platform": "instagram", "ext": item["ext"],
                    "is_image": item["ext"] in ["jpg", "jpeg", "png", "webp"],
                    "all_images": [], "carousel_items": []
                }
            first = valid[0]
            carousel_items = [
                {"url": item["url"], "is_video": item["is_video"],
                 "thumbnail": item["thumbnail"], "index": i}
                for i, item in enumerate(valid)
            ]
            all_images = [item["url"] for item in valid if not item["is_video"]]
            return {
                "success": True, "direct_url": first["url"],
                "title": info.get("title", "Instagram Carrousel"),
                "thumbnail": first["thumbnail"], "duration": 0,
                "platform": "instagram", "ext": first["ext"],
                "is_image": not first["is_video"],
                "all_images": all_images, "carousel_items": carousel_items
            }

        ext = info.get("ext", "mp4")
        is_image = ext in ["jpg", "jpeg", "png", "webp"]
        du = info.get("url", "")
        if not du and info.get("formats"):
            fmts = [f for f in info["formats"] if f.get("url")]
            mp4s = [f for f in fmts if f.get("ext") == "mp4"]
            du = mp4s[-1]["url"] if mp4s else (fmts[-1].get("url", "") if fmts else "")
        if du:
            return {
                "success": True, "direct_url": du, "title": info.get("title", "Instagram"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": int(info.get("duration") or 0),
                "platform": "instagram", "ext": ext,
                "is_image": is_image, "all_images": [], "carousel_items": []
            }
    except Exception:
        pass
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass
    return {"success": False, "error": "yt-dlp instagram failed"}

async def resolve_instagram(url, extra_cookies=""):
    url = clean_instagram_url(url)
    cached = cache_get(url)
    if cached:
        return cached

    is_story = "/stories/" in url.lower()

    if is_story:
        result = await first_success(
            _instagram_ytdlp(url, extra_cookies),
            _instagram_snapinsta(url)
        )
    else:
        result = await first_success(
            _instagram_snapinsta(url),
            _instagram_instavideosave(url),
            _instagram_ytdlp(url, extra_cookies)
        )

    if result.get("success"):
        result.setdefault("carousel_items", [])
        cache_set(url, result)
        return result

    msg = ("Story Instagram expirée ou privée." if is_story
           else "Impossible de télécharger ce contenu Instagram.")
    return {"success": False, "error": msg}

# ── Facebook ──────────────────────────────────────────────────────────────────
async def _facebook_getfvid(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
            r1 = await c.get("https://www.getfvid.com/", headers={"User-Agent": "Mozilla/5.0"})
            token_m = re.search(r'name="_token"\s+value="([^"]+)"', r1.text)
            if not token_m:
                return {"success": False, "error": "getfvid token failed"}
            r2 = await c.post(
                "https://www.getfvid.com/downloader",
                data={"url": url, "_token": token_m.group(1)},
                headers={
                    "User-Agent": "Mozilla/5.0", "Referer": "https://www.getfvid.com/",
                    "Content-Type": "application/x-www-form-urlencoded"
                }
            )
            vids = re.findall(r'https://[^"\']+\.mp4[^"\']*', r2.text)
            if vids:
                hd = [v for v in vids if "hd" in v.lower()]
                best = hd[0] if hd else vids[0]
                return {
                    "success": True, "direct_url": best, "title": "Video Facebook",
                    "thumbnail": "", "duration": 0, "platform": "facebook",
                    "ext": "mp4", "is_image": False, "all_images": []
                }
    except Exception:
        pass
    return {"success": False, "error": "getfvid failed"}

async def _facebook_savefrom(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(
                f"https://worker.sf-tools.com/savefrom.php?sf_url={url}",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            data = r.json()
            urls = data.get("url", [])
            if isinstance(urls, list) and urls:
                best = urls[0]
                video_url = best.get("url", "") if isinstance(best, dict) else str(best)
                if video_url and ".mp4" in video_url:
                    return {
                        "success": True, "direct_url": video_url,
                        "title": data.get("meta", {}).get("title", "Facebook"),
                        "thumbnail": data.get("thumb", ""), "duration": 0,
                        "platform": "facebook", "ext": "mp4", "is_image": False, "all_images": []
                    }
    except Exception:
        pass
    return {"success": False, "error": "savefrom failed"}

async def _facebook_ytdlp(url: str, extra_cookies: str = "") -> dict:
    tmp = None
    try:
        if extra_cookies:
            tmp = cookies_to_tempfile(extra_cookies, "facebook")
        cf = tmp or get_cookie_file("facebook")
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "format": "best[ext=mp4]/best", "socket_timeout": 8, "retries": 1
        }
        if cf:
            opts["cookiefile"] = cf
            opts["http_headers"] = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        info = await run_yt_dlp(opts, url)
        if info.get("_type") == "playlist":
            entries = info.get("entries", [])
            if entries:
                info = entries[0]
        ext = info.get("ext", "mp4")
        is_image = ext in ["jpg", "jpeg", "png", "webp"]
        du = info.get("url", "")
        if not du and info.get("formats"):
            fmts = info["formats"]
            mp4s = [f for f in fmts if f.get("ext") == "mp4" and f.get("url")]
            du = mp4s[-1]["url"] if mp4s else fmts[-1].get("url", "")
        if du:
            return {
                "success": True, "direct_url": du, "title": info.get("title", "Facebook"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": int(info.get("duration") or 0),
                "platform": "facebook", "ext": ext,
                "is_image": is_image, "all_images": []
            }
    except Exception:
        pass
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass
    return {"success": False, "error": "yt-dlp facebook failed"}

async def resolve_facebook(url, extra_cookies=""):
    lower = url.lower()
    for bad in ["checkpoint", "login.php", "/login", "facebook.com/?",
                "m.facebook.com/?", "web.facebook.com/?", "facebook.com/home",
                "lm.facebook.com"]:
        if bad in lower:
            return {"success": False, "error": "Naviguez vers un post, vidéo ou story spécifique."}

    cached = cache_get(url)
    if cached:
        return cached

    is_story = "/stories/" in lower

    result = await first_success(_facebook_getfvid(url), _facebook_savefrom(url))
    if not result.get("success"):
        result = await _facebook_ytdlp(url, extra_cookies)

    if result.get("success"):
        cache_set(url, result)
        return result

    if is_story:
        return {"success": False, "error": "Impossible de télécharger cette story Facebook."}
    err = result.get("error", "").lower()
    if "login" in err or "cookie" in err:
        return {"success": False, "error": "Connexion Facebook requise."}
    if "private" in err:
        return {"success": False, "error": "Ce contenu Facebook est privé."}
    return {"success": False, "error": "Impossible de télécharger cette vidéo Facebook."}

# ── Route principale ──────────────────────────────────────────────────────────
@app.post("/resolve")
async def resolve_video(req: ResolveRequest):
    url     = req.url.strip()
    cookies = req.cookies.strip()

    if any(b in url.lower() for b in ["youtube.com", "youtu.be"]):
        raise HTTPException(status_code=403, detail="YouTube non supporté")

    # FIX #3 : Bloc try/except correctement indenté — le check liens courts
    # est maintenant DANS le try pour être couvert par le timeout global.
    # SUPPRESSION du retour anticipé "LIEN_COURT_TIKTOK" : on résout maintenant
    # ces liens en interne via expand_tiktok_short_url (voir resolve_tiktok).
    try:
        if "tiktok.com" in url.lower():
            coro = (resolve_tiktok_story(url)
                    if "/story/" in url.lower() or "/photo/" in url.lower()
                    else resolve_tiktok(url))
        elif any(x in url.lower() for x in ["twitter.com", "x.com", "t.co"]):
            coro = resolve_twitter(url)
        elif "instagram.com" in url.lower():
            coro = resolve_instagram(url, extra_cookies=cookies)
        elif any(x in url.lower() for x in ["facebook.com", "fb.watch", "fb.com"]):
            coro = resolve_facebook(url, extra_cookies=cookies)
        else:
            cached = cache_get(url)
            if cached:
                return cached

            async def _generic():
                opts = {
                    "quiet": True, "skip_download": True,
                    "format": "best[ext=mp4]/best", "socket_timeout": 8, "retries": 1
                }
                try:
                    info = await run_yt_dlp(opts, url)
                    ext = info.get("ext", "mp4")
                    du = info.get("url", "")
                    if not du and info.get("formats"):
                        mp4s = [f for f in info["formats"] if f.get("ext") == "mp4" and f.get("url")]
                        du = mp4s[-1]["url"] if mp4s else info["formats"][-1].get("url", "")
                    if not du:
                        return {"success": False, "error": "Impossible de résoudre cette URL"}
                    result = {
                        "success": True, "direct_url": du, "title": info.get("title", "Media"),
                        "thumbnail": info.get("thumbnail", ""),
                        "duration": int(info.get("duration") or 0),
                        "platform": "unknown", "ext": ext,
                        "is_image": ext in ["jpg", "jpeg", "png", "webp"], "all_images": []
                    }
                    cache_set(url, result)
                    return result
                except Exception as e:
                    return {"success": False, "error": str(e)[:200]}

            coro = _generic()

        return await asyncio.wait_for(coro, timeout=25.0)

    except asyncio.TimeoutError:
        return {"success": False, "error": "Délai dépassé. Réessayez ou vérifiez le lien."}
    except Exception as e:
        return {"success": False, "error": f"Erreur serveur: {str(e)[:150]}"}

# ── Nettoyage du cache ────────────────────────────────────────────────────────
@app.delete("/cache")
def clear_cache():
    _cache.clear()
    return {"cleared": True}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=2)
