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
import json
from functools import partial
from concurrent.futures import ThreadPoolExecutor

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

executor = ThreadPoolExecutor(max_workers=4)

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

class ResolveRequest(BaseModel):
    url: str
    cookies: str = ""
    platform_hint: str = ""

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

async def run_yt_dlp(opts: dict, url: str) -> dict:
    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _extract)

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

# ── TikTok ────────────────────────────────────────────────────────────────────
async def resolve_tiktok(url):
    cached = cache_get(url)
    if cached:
        return cached
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            data = (await c.get(f"https://tikwm.com/api/?url={url}&hd=1")).json()
        if data.get("code") == 0:
            vd = data.get("data", {})
            imgs = vd.get("images", [])
            if imgs:
                result = {"success": True, "direct_url": imgs[0], "title": vd.get("title", "TikTok"),
                          "thumbnail": imgs[0], "duration": 0, "platform": "tiktok",
                          "ext": "jpg", "is_image": True, "all_images": imgs}
                cache_set(url, result)
                return result
            play = vd.get("hdplay") or vd.get("play")
            if play:
                result = {"success": True, "direct_url": play, "title": vd.get("title", "TikTok"),
                          "thumbnail": vd.get("cover", ""), "duration": vd.get("duration", 0),
                          "platform": "tiktok", "ext": "mp4", "is_image": False, "all_images": []}
                cache_set(url, result)
                return result
    except Exception:
        pass
    return {"success": False, "error": "Impossible de résoudre cette vidéo TikTok"}

async def resolve_tiktok_story(url):
    cached = cache_get(url)
    if cached:
        return cached
    opts = {"quiet": True, "skip_download": True, "format": "best", "socket_timeout": 15}
    try:
        info = await run_yt_dlp(opts, url)
        ext = info.get("ext", "mp4")
        du = info.get("url") or (info.get("formats") or [{}])[-1].get("url", "")
        if du:
            result = {"success": True, "direct_url": du, "title": info.get("title", "TikTok Story"),
                      "thumbnail": info.get("thumbnail", ""), "duration": int(info.get("duration") or 0),
                      "platform": "tiktok", "ext": ext, "is_image": ext in ["jpg", "jpeg", "png", "webp"],
                      "all_images": []}
            cache_set(url, result)
            return result
    except Exception:
        pass
    return {"success": False, "error": "Impossible de télécharger cette story TikTok."}

# ── Twitter / X — VERSION DEBUG ───────────────────────────────────────────────
async def resolve_twitter(url):
    cached = cache_get(url)
    if cached:
        return cached

    tid = url.rstrip("/").split("/")[-1].split("?")[0]
    apis = [
        f"https://api.fxtwitter.com/status/{tid}",
        f"https://api.vxtwitter.com/status/{tid}"
    ]

    async def fetch_api(api_url):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(api_url, headers={"User-Agent": "Mozilla/5.0"})
                data = r.json()
                # ══ LOG COMPLET ══
                print(f"\n[TWITTER DEBUG] {api_url}")
                print(json.dumps(data, indent=2, ensure_ascii=False)[:5000])
                print("[TWITTER DEBUG END]\n")
                return data
        except Exception as e:
            print(f"[TWITTER ERROR] {api_url} → {e}")
            return None

    results = await asyncio.gather(*[fetch_api(a) for a in apis])

    for data in results:
        if not data:
            continue
        t = data.get("tweet", data)
        m = t.get("media", {})

        def best_thumbnail(video_obj=None):
            if video_obj:
                for key in ("thumbnail_url", "thumbnail", "preview_image_url"):
                    v = video_obj.get(key, "")
                    if v:
                        return v
            for key in ("thumbnail_url", "thumbnail"):
                v = t.get(key, "")
                if v and isinstance(v, str):
                    return v
                if v and isinstance(v, dict):
                    return v.get("url", "")
            for p in m.get("photos", []):
                if p.get("url"):
                    return p["url"]
            return ""

        for v in m.get("videos", []):
            if v.get("url"):
                result = {"success": True, "direct_url": v["url"],
                          "title": t.get("text", "X")[:100],
                          "thumbnail": best_thumbnail(v),
                          "duration": 0, "platform": "twitter",
                          "ext": "mp4", "is_image": False, "all_images": []}
                cache_set(url, result)
                return result

        for g in m.get("gifs", []):
            if g.get("url"):
                result = {"success": True, "direct_url": g["url"],
                          "title": t.get("text", "X")[:100],
                          "thumbnail": best_thumbnail(g),
                          "duration": 0, "platform": "twitter",
                          "ext": "mp4", "is_image": False, "all_images": []}
                cache_set(url, result)
                return result

        for p in m.get("photos", []):
            if p.get("url"):
                result = {"success": True, "direct_url": p["url"],
                          "title": t.get("text", "X")[:100],
                          "thumbnail": p["url"],
                          "duration": 0, "platform": "twitter",
                          "ext": "jpg", "is_image": True, "all_images": []}
                cache_set(url, result)
                return result

        for me in data.get("media_extended", []):
            if me.get("type") in ["video", "gif"] and me.get("url"):
                result = {"success": True, "direct_url": me["url"],
                          "title": data.get("text", "X")[:100],
                          "thumbnail": me.get("thumbnail_url") or me.get("thumbnail") or "",
                          "duration": 0, "platform": "twitter",
                          "ext": "mp4", "is_image": False, "all_images": []}
                cache_set(url, result)
                return result
            if me.get("type") == "image" and me.get("url"):
                result = {"success": True, "direct_url": me["url"],
                          "title": data.get("text", "X")[:100],
                          "thumbnail": me["url"],
                          "duration": 0, "platform": "twitter",
                          "ext": "jpg", "is_image": True, "all_images": []}
                cache_set(url, result)
                return result

    return {"success": False, "error": "Ce tweet ne contient pas de média ou est privé."}

# ── Instagram ─────────────────────────────────────────────────────────────────
async def resolve_instagram_via_api(url):
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            tr = await c.get("https://snapinsta.app/", headers={"User-Agent": "Mozilla/5.0"})
            m = re.search(r'name="_token"\s+value="([^"]+)"', tr.text)
            if m:
                dr = await c.post(
                    "https://snapinsta.app/action.php",
                    data={"url": url, "_token": m.group(1)},
                    headers={"User-Agent": "Mozilla/5.0",
                             "Referer": "https://snapinsta.app/",
                             "X-Requested-With": "XMLHttpRequest"}
                )
                if dr.status_code == 200:
                    try:
                        html = dr.json().get("data", "") if isinstance(dr.json(), dict) else str(dr.json())
                    except Exception:
                        html = dr.text
                    vids = re.findall(r'https://[^"\']+\.mp4[^"\']*', html)
                    imgs = re.findall(r'https://[^"\']+\.jpg[^"\']*', html)
                    if vids:
                        return {"success": True, "direct_url": vids[0], "title": "Video Instagram",
                                "thumbnail": imgs[0] if imgs else "", "duration": 0,
                                "platform": "instagram", "ext": "mp4", "is_image": False, "all_images": []}
                    if imgs:
                        return {"success": True, "direct_url": imgs[0], "title": "Photo Instagram",
                                "thumbnail": imgs[0], "duration": 0, "platform": "instagram",
                                "ext": "jpg", "is_image": True, "all_images": []}
    except Exception:
        pass
    return {"success": False, "error": ""}

async def resolve_instagram(url, extra_cookies=""):
    url = clean_instagram_url(url)
    cached = cache_get(url)
    if cached:
        return cached
    is_story = "/stories/" in url.lower()
    tmp = None
    try:
        if extra_cookies:
            tmp = cookies_to_tempfile(extra_cookies, "instagram")
        cf = tmp or get_cookie_file("instagram")
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "format": "best", "socket_timeout": 15,
            "http_headers": {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605.1.15"}
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
                    valid.append({"url": du, "is_video": ext == "mp4",
                                  "ext": ext, "thumbnail": e.get("thumbnail", "")})
            if not valid:
                raise Exception("no entries")
            if len(valid) == 1:
                item = valid[0]
                result = {"success": True, "direct_url": item["url"],
                          "title": info.get("title", "Instagram"),
                          "thumbnail": item["thumbnail"], "duration": 0,
                          "platform": "instagram", "ext": item["ext"],
                          "is_image": item["ext"] in ["jpg", "jpeg", "png", "webp"],
                          "all_images": [], "carousel_items": []}
                cache_set(url, result)
                return result
            first = valid[0]
            carousel_items = [{"url": item["url"], "is_video": item["is_video"],
                               "thumbnail": item["thumbnail"], "index": i}
                              for i, item in enumerate(valid)]
            all_images = [item["url"] for item in valid if not item["is_video"]]
            result = {"success": True, "direct_url": first["url"],
                      "title": info.get("title", "Instagram Carrousel"),
                      "thumbnail": first["thumbnail"], "duration": 0,
                      "platform": "instagram", "ext": first["ext"],
                      "is_image": not first["is_video"],
                      "all_images": all_images, "carousel_items": carousel_items}
            cache_set(url, result)
            return result
        ext = info.get("ext", "mp4")
        is_image = ext in ["jpg", "jpeg", "png", "webp"]
        du = info.get("url", "")
        if not du and info.get("formats"):
            fmts = [f for f in info["formats"] if f.get("url")]
            mp4s = [f for f in fmts if f.get("ext") == "mp4"]
            du = mp4s[-1]["url"] if mp4s else (fmts[-1].get("url", "") if fmts else "")
        if du:
            result = {"success": True, "direct_url": du,
                      "title": info.get("title", "Instagram"),
                      "thumbnail": info.get("thumbnail", ""),
                      "duration": int(info.get("duration") or 0),
                      "platform": "instagram", "ext": ext,
                      "is_image": is_image, "all_images": [], "carousel_items": []}
            cache_set(url, result)
            return result
    except Exception:
        pass
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass
    result = await resolve_instagram_via_api(url)
    if result.get("success"):
        result.setdefault("carousel_items", [])
        cache_set(url, result)
        return result
    msg = ("Story Instagram expirée ou privée." if is_story
           else "Impossible de télécharger ce contenu Instagram.")
    return {"success": False, "error": msg}

# ── Facebook ──────────────────────────────────────────────────────────────────
async def resolve_facebook(url, extra_cookies=""):
    lower = url.lower()
    for bad in ["checkpoint", "login.php", "/login", "facebook.com/?",
                "m.facebook.com/?", "web.facebook.com/?", "facebook.com/home", "lm.facebook.com"]:
        if bad in lower:
            return {"success": False, "error": "Naviguez vers un post, vidéo ou story spécifique."}
    cached = cache_get(url)
    if cached:
        return cached
    is_story = "/stories/" in lower
    tmp = None
    try:
        if extra_cookies:
            tmp = cookies_to_tempfile(extra_cookies, "facebook")
        cf = tmp or get_cookie_file("facebook")
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "format": "best[ext=mp4]/best", "socket_timeout": 15}
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
        if not du:
            raise Exception("no url")
        result = {"success": True, "direct_url": du, "title": info.get("title", "Facebook"),
                  "thumbnail": info.get("thumbnail", ""),
                  "duration": int(info.get("duration") or 0),
                  "platform": "facebook", "ext": ext,
                  "is_image": is_image, "all_images": []}
        cache_set(url, result)
        return result
    except Exception as e:
        msg = str(e).lower()
        if is_story:
            return {"success": False, "error": "Impossible de télécharger cette story Facebook."}
        if "login" in msg or "cookie" in msg:
            return {"success": False, "error": "Connexion Facebook requise."}
        if "private" in msg:
            return {"success": False, "error": "Ce contenu Facebook est privé."}
        return {"success": False, "error": f"Erreur Facebook: {str(e)[:150]}"}
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass

# ── Route principale ──────────────────────────────────────────────────────────
@app.post("/resolve")
async def resolve_video(req: ResolveRequest):
    url = req.url.strip()
    cookies = req.cookies.strip()
    if any(b in url.lower() for b in ["youtube.com", "youtu.be"]):
        raise HTTPException(status_code=403, detail="YouTube non supporté")
    if "tiktok.com" in url.lower():
        return await (resolve_tiktok_story(url)
                      if "/story/" in url.lower() or "/photo/" in url.lower()
                      else resolve_tiktok(url))
    if any(x in url.lower() for x in ["twitter.com", "x.com", "t.co"]):
        return await resolve_twitter(url)
    if "instagram.com" in url.lower():
        return await resolve_instagram(url, extra_cookies=cookies)
    if any(x in url.lower() for x in ["facebook.com", "fb.watch", "fb.com"]):
        return await resolve_facebook(url, extra_cookies=cookies)
    cached = cache_get(url)
    if cached:
        return cached
    opts = {"quiet": True, "skip_download": True,
            "format": "best[ext=mp4]/best", "socket_timeout": 15}
    try:
        info = await run_yt_dlp(opts, url)
        ext = info.get("ext", "mp4")
        du = info.get("url", "")
        if not du and info.get("formats"):
            mp4s = [f for f in info["formats"] if f.get("ext") == "mp4" and f.get("url")]
            du = mp4s[-1]["url"] if mp4s else info["formats"][-1].get("url", "")
        if not du:
            return {"success": False, "error": "Impossible de résoudre cette URL"}
        result = {"success": True, "direct_url": du, "title": info.get("title", "Media"),
                  "thumbnail": info.get("thumbnail", ""),
                  "duration": int(info.get("duration") or 0),
                  "platform": "unknown", "ext": ext,
                  "is_image": ext in ["jpg", "jpeg", "png", "webp"], "all_images": []}
        cache_set(url, result)
        return result
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}

@app.delete("/cache")
def clear_cache():
    _cache.clear()
    return {"cleared": True}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=2)
