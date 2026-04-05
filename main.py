from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yt_dlp
import uvicorn
import httpx
import re
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ResolveRequest(BaseModel):
    url: str

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTAGRAM_COOKIES = os.path.join(BASE_DIR, "www.instagram.com_cookies.txt")
FACEBOOK_COOKIES = os.path.join(BASE_DIR, "web.facebook.com_cookies.txt")
TIKTOK_COOKIES = os.path.join(BASE_DIR, "www.tiktok.com_cookies.txt")

def get_cookie_file(platform: str):
    if platform == "instagram" and os.path.exists(INSTAGRAM_COOKIES):
        return INSTAGRAM_COOKIES
    elif platform == "facebook" and os.path.exists(FACEBOOK_COOKIES):
        return FACEBOOK_COOKIES
    elif platform == "tiktok" and os.path.exists(TIKTOK_COOKIES):
        return TIKTOK_COOKIES
    return None

@app.get("/")
def root():
    return {"status": "SocialVault Backend running"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/check")
def check_files():
    files = {}
    for name, path in [
        ("www.instagram.com_cookies.txt", INSTAGRAM_COOKIES),
        ("web.facebook.com_cookies.txt", FACEBOOK_COOKIES),
        ("www.tiktok.com_cookies.txt", TIKTOK_COOKIES)
    ]:
        files[name] = os.path.exists(path)
    files["base_dir"] = BASE_DIR
    try:
        files["dir_contents"] = os.listdir(BASE_DIR)
    except Exception:
        files["dir_contents"] = []
    return files

@app.post("/set_cookies")
async def set_cookies(request: Request):
    try:
        body = await request.json()
        platform = body.get("platform", "")
        cookies_str = body.get("cookies", "")

        if not platform or not cookies_str:
            return {"success": False, "error": "Donnees manquantes"}

        cookie_file_map = {
            "instagram": INSTAGRAM_COOKIES,
            "facebook": FACEBOOK_COOKIES,
        }

        cookie_file = cookie_file_map.get(platform)
        if not cookie_file:
            return {"success": False, "error": "Plateforme non supportee"}

        domain_map = {
            "instagram": ".instagram.com",
            "facebook": ".facebook.com",
        }
        domain = domain_map[platform]

        lines = ["# Netscape HTTP Cookie File\n"]
        for cookie_pair in cookies_str.split(";"):
            cookie_pair = cookie_pair.strip()
            if "=" in cookie_pair:
                name, _, value = cookie_pair.partition("=")
                lines.append(
                    f"{domain}\tTRUE\t/\tTRUE\t2147483647\t{name.strip()}\t{value.strip()}\n"
                )

        with open(cookie_file, "w") as f:
            f.writelines(lines)

        return {"success": True, "message": f"Cookies {platform} sauvegardes"}
    except Exception as e:
        return {"success": False, "error": str(e)[:100]}

@app.get("/download")
async def download_video(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36",
        "Referer": "https://www.tiktok.com/",
    }
    async def stream():
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers) as response:
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    yield chunk
    return StreamingResponse(
        stream(),
        media_type="video/mp4",
        headers={"Content-Disposition": "attachment; filename=video.mp4"}
    )

async def resolve_tiktok(url: str) -> dict:
    try:
        api_url = f"https://tikwm.com/api/?url={url}&hd=1"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(api_url)
            data = response.json()
        if data.get("code") == 0:
            video_data = data.get("data", {})
            images = video_data.get("images", [])
            if images and len(images) > 0:
                return {
                    "success": True,
                    "direct_url": images[0],
                    "title": video_data.get("title", "TikTok Photo"),
                    "thumbnail": images[0],
                    "duration": 0,
                    "platform": "tiktok",
                    "ext": "jpg",
                    "is_image": True,
                    "all_images": images
                }
            play_url = video_data.get("hdplay") or video_data.get("play")
            if play_url:
                return {
                    "success": True,
                    "direct_url": play_url,
                    "title": video_data.get("title", "TikTok Video"),
                    "thumbnail": video_data.get("cover", ""),
                    "duration": video_data.get("duration", 0),
                    "platform": "tiktok",
                    "ext": "mp4",
                    "is_image": False,
                    "all_images": []
                }
    except Exception:
        pass

    try:
        api_url2 = f"https://api.tikmate.app/api/lookup?url={url}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(api_url2, data={"url": url})
            data2 = resp.json()
        if data2.get("success"):
            return {
                "success": True,
                "direct_url": data2.get("download_url", ""),
                "title": data2.get("desc", "TikTok Video"),
                "thumbnail": data2.get("cover", ""),
                "duration": 0,
                "platform": "tiktok",
                "ext": "mp4",
                "is_image": False,
                "all_images": []
            }
    except Exception:
        pass

    return {"success": False, "error": "Impossible de resoudre cette video TikTok"}

async def resolve_tiktok_story(url: str) -> dict:
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "best",
            "socket_timeout": 30,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            ext = info.get("ext", "mp4")
            is_image = ext in ["jpg", "jpeg", "png", "webp"]
            direct_url = info.get("url", "")
            if not direct_url and info.get("formats"):
                direct_url = info["formats"][-1].get("url", "")
            if direct_url:
                return {
                    "success": True,
                    "direct_url": direct_url,
                    "title": info.get("title", "TikTok Story"),
                    "thumbnail": info.get("thumbnail", ""),
                    "duration": int(info.get("duration") or 0),
                    "platform": "tiktok",
                    "ext": ext,
                    "is_image": is_image,
                    "all_images": []
                }
    except Exception:
        pass
    return {"success": False, "error": "Impossible de telecharger cette story TikTok."}

async def resolve_twitter(url: str) -> dict:
    try:
        tweet_id = url.rstrip("/").split("/")[-1].split("?")[0]
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"https://api.fxtwitter.com/status/{tweet_id}",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            data = response.json()

        if data.get("code") == 200:
            tweet = data.get("tweet", {})
            media = tweet.get("media", {})
            videos = media.get("videos", [])
            photos = media.get("photos", [])
            gifs = media.get("gifs", [])

            if videos:
                best_video = max(videos, key=lambda v: v.get("width", 0))
                video_url = best_video.get("url", "")
                if video_url:
                    return {
                        "success": True,
                        "direct_url": video_url,
                        "title": tweet.get("text", "Video X")[:100],
                        "thumbnail": tweet.get("thumbnail_url", ""),
                        "duration": 0,
                        "platform": "twitter",
                        "ext": "mp4",
                        "is_image": False,
                        "all_images": []
                    }
            if gifs:
                gif_url = gifs[0].get("url", "")
                if gif_url:
                    return {
                        "success": True,
                        "direct_url": gif_url,
                        "title": tweet.get("text", "GIF X")[:100],
                        "thumbnail": tweet.get("thumbnail_url", ""),
                        "duration": 0,
                        "platform": "twitter",
                        "ext": "mp4",
                        "is_image": False,
                        "all_images": []
                    }
            if photos:
                photo_url = photos[0].get("url", "")
                if photo_url:
                    return {
                        "success": True,
                        "direct_url": photo_url,
                        "title": tweet.get("text", "Photo X")[:100],
                        "thumbnail": photo_url,
                        "duration": 0,
                        "platform": "twitter",
                        "ext": "jpg",
                        "is_image": True,
                        "all_images": []
                    }
            return {
                "success": False,
                "error": "Ce tweet est un tweet texte et ne contient pas de video, photo ou GIF."
            }
    except Exception:
        pass

    try:
        tweet_id = url.rstrip("/").split("/")[-1].split("?")[0]
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"https://api.vxtwitter.com/status/{tweet_id}",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            data = response.json()
        media_urls = data.get("media_extended", [])
        for media in media_urls:
            if media.get("type") in ["video", "gif"]:
                return {
                    "success": True,
                    "direct_url": media.get("url", ""),
                    "title": data.get("text", "Video X")[:100],
                    "thumbnail": data.get("tweetThumbnailUrl", ""),
                    "duration": 0,
                    "platform": "twitter",
                    "ext": "mp4",
                    "is_image": False,
                    "all_images": []
                }
            elif media.get("type") == "image":
                return {
                    "success": True,
                    "direct_url": media.get("url", ""),
                    "title": data.get("text", "Photo X")[:100],
                    "thumbnail": media.get("url", ""),
                    "duration": 0,
                    "platform": "twitter",
                    "ext": "jpg",
                    "is_image": True,
                    "all_images": []
                }
        return {
            "success": False,
            "error": "Ce tweet est un tweet texte et ne contient pas de media."
        }
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"https://twitsave.com/info?url={url}",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if response.status_code == 200:
                urls = re.findall(r'https://[^"]*\.mp4[^"]*', response.text)
                if urls:
                    return {
                        "success": True,
                        "direct_url": urls[0],
                        "title": "Video X",
                        "thumbnail": "",
                        "duration": 0,
                        "platform": "twitter",
                        "ext": "mp4",
                        "is_image": False,
                        "all_images": []
                    }
    except Exception:
        pass

    return {
        "success": False,
        "error": "Ce tweet ne contient pas de media ou le contenu est prive."
    }

async def resolve_instagram_photo(url: str) -> dict:
    shortcode = ""
    parts = url.rstrip("/").split("/")
    for i, p in enumerate(parts):
        if p in ["p", "reel", "tv", "stories"]:
            if i + 1 < len(parts):
                shortcode = parts[i + 1].split("?")[0]
                break

    if not shortcode:
        return {"success": False, "error": "Lien Instagram invalide."}

    # API 1 — snapinsta
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            token_resp = await client.get(
                "https://snapinsta.app/",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            token_match = re.search(r'name="_token"\s+value="([^"]+)"', token_resp.text)
            token = token_match.group(1) if token_match else ""
            if token:
                dl_resp = await client.post(
                    "https://snapinsta.app/action.php",
                    data={"url": url, "_token": token},
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "https://snapinsta.app/",
                        "X-Requested-With": "XMLHttpRequest"
                    }
                )
                if dl_resp.status_code == 200:
                    result = dl_resp.json()
                    html = result.get("data", "") if isinstance(result, dict) else str(result)
                    img_urls = re.findall(r'https://[^"\']+\.jpg[^"\']*', html)
                    vid_urls = re.findall(r'https://[^"\']+\.mp4[^"\']*', html)
                    if vid_urls:
                        return {
                            "success": True,
                            "direct_url": vid_urls[0],
                            "title": "Video Instagram",
                            "thumbnail": img_urls[0] if img_urls else "",
                            "duration": 0,
                            "platform": "instagram",
                            "ext": "mp4",
                            "is_image": False,
                            "all_images": []
                        }
                    elif img_urls:
                        return {
                            "success": True,
                            "direct_url": img_urls[0],
                            "title": "Photo Instagram",
                            "thumbnail": img_urls[0],
                            "duration": 0,
                            "platform": "instagram",
                            "ext": "jpg",
                            "is_image": True,
                            "all_images": []
                        }
    except Exception:
        pass

    # API 2 — saveinsta
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.post(
                "https://saveinsta.app/api/ajaxSearch",
                data={"q": url, "t": "media", "lang": "fr"},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://saveinsta.app/",
                    "X-Requested-With": "XMLHttpRequest"
                }
            )
            if resp.status_code == 200:
                result = resp.json()
                data_html = result.get("data", "")
                img_urls = re.findall(r'https://[^"\']+scontent[^"\']+\.jpg[^"\']*', data_html)
                vid_urls = re.findall(r'https://[^"\']+\.mp4[^"\']*', data_html)
                if vid_urls:
                    return {
                        "success": True,
                        "direct_url": vid_urls[0],
                        "title": "Video Instagram",
                        "thumbnail": img_urls[0] if img_urls else "",
                        "duration": 0,
                        "platform": "instagram",
                        "ext": "mp4",
                        "is_image": False,
                        "all_images": []
                    }
                elif img_urls:
                    return {
                        "success": True,
                        "direct_url": img_urls[0],
                        "title": "Photo Instagram",
                        "thumbnail": img_urls[0],
                        "duration": 0,
                        "platform": "instagram",
                        "ext": "jpg",
                        "is_image": True,
                        "all_images": []
                    }
    except Exception:
        pass

    # API 3 — instagramdownloader
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.post(
                "https://instagramdownloader.io/api/index.php",
                data={"url": url},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "X-Requested-With": "XMLHttpRequest"
                }
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    media = data.get("media", [])
                    if media:
                        item = media[0]
                        direct_url = item.get("url", "")
                        is_video = item.get("type", "") == "video"
                        if direct_url:
                            return {
                                "success": True,
                                "direct_url": direct_url,
                                "title": "Media Instagram",
                                "thumbnail": direct_url if not is_video else "",
                                "duration": 0,
                                "platform": "instagram",
                                "ext": "mp4" if is_video else "jpg",
                                "is_image": not is_video,
                                "all_images": []
                            }
    except Exception:
        pass

    return {"success": False, "error": ""}

async def resolve_instagram(url: str) -> dict:
    cookie_file = get_cookie_file("instagram")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best",
        "socket_timeout": 30,
        "extract_flat": False,
    }
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
        ydl_opts["http_headers"] = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                entries = info.get("entries", [])
                valid_entries = [e for e in entries if e and (e.get("url") or e.get("formats"))]
                if not valid_entries:
                    raise Exception("no valid entries")
                info = valid_entries[0]
            ext = info.get("ext", "mp4")
            is_image = ext in ["jpg", "jpeg", "png", "webp"]
            direct_url = info.get("url", "")
            if not direct_url and info.get("formats"):
                formats = [f for f in info["formats"] if f.get("url")]
                if is_image:
                    direct_url = formats[-1].get("url", "") if formats else ""
                else:
                    mp4_formats = [f for f in formats if f.get("ext") == "mp4"]
                    direct_url = mp4_formats[-1]["url"] if mp4_formats else (formats[-1].get("url", "") if formats else "")
            if direct_url:
                return {
                    "success": True,
                    "direct_url": direct_url,
                    "title": info.get("title", "Media Instagram"),
                    "thumbnail": info.get("thumbnail", ""),
                    "duration": int(info.get("duration") or 0),
                    "platform": "instagram",
                    "ext": ext,
                    "is_image": is_image,
                    "all_images": []
                }
    except Exception:
        pass

    result = await resolve_instagram_photo(url)
    if result.get("success"):
        return result

    try:
        shortcode = ""
        parts = url.rstrip("/").split("/")
        for i, p in enumerate(parts):
            if p in ["p", "reel", "tv", "stories"]:
                if i + 1 < len(parts):
                    shortcode = parts[i + 1].split("?")[0]
                    break
        if shortcode:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(
                    f"https://ddinstagram.com/p/{shortcode}/",
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                if resp.status_code == 200:
                    video_urls = re.findall(r'https://[^"\']*\.mp4[^"\']*', resp.text)
                    image_urls = re.findall(r'https://[^"\']*scontent[^"\']*\.jpg[^"\']*', resp.text)
                    if video_urls:
                        return {
                            "success": True,
                            "direct_url": video_urls[0],
                            "title": "Video Instagram",
                            "thumbnail": image_urls[0] if image_urls else "",
                            "duration": 0,
                            "platform": "instagram",
                            "ext": "mp4",
                            "is_image": False,
                            "all_images": []
                        }
                    elif image_urls:
                        return {
                            "success": True,
                            "direct_url": image_urls[0],
                            "title": "Photo Instagram",
                            "thumbnail": image_urls[0],
                            "duration": 0,
                            "platform": "instagram",
                            "ext": "jpg",
                            "is_image": True,
                            "all_images": []
                        }
    except Exception:
        pass

    return {
        "success": False,
        "error": "Impossible de telecharger ce contenu Instagram. Seuls les contenus publics sont supportes."
    }

async def resolve_facebook(url: str) -> dict:
    cookie_file = get_cookie_file("facebook")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best[ext=mp4]/best",
        "socket_timeout": 30,
    }
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
        ydl_opts["http_headers"] = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                entries = info.get("entries", [])
                if entries:
                    info = entries[0]
            ext = info.get("ext", "mp4")
            is_image = ext in ["jpg", "jpeg", "png", "webp"]
            direct_url = info.get("url", "")
            if not direct_url and info.get("formats"):
                formats = info["formats"]
                mp4_formats = [f for f in formats if f.get("ext") == "mp4" and f.get("url")]
                direct_url = mp4_formats[-1]["url"] if mp4_formats else formats[-1].get("url", "")
            if not direct_url:
                return {"success": False, "error": "Impossible d'extraire ce contenu Facebook."}
            return {
                "success": True,
                "direct_url": direct_url,
                "title": info.get("title", "Video Facebook"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": int(info.get("duration") or 0),
                "platform": "facebook",
                "ext": ext,
                "is_image": is_image,
                "all_images": []
            }
    except Exception as e:
        error_msg = str(e)
        if "login" in error_msg.lower() or "cookie" in error_msg.lower():
            return {
                "success": False,
                "error": "Ce contenu Facebook necessite une connexion. Seuls les contenus publics et vos propres stories sont supportes."
            }
        elif "private" in error_msg.lower():
            return {"success": False, "error": "Ce contenu Facebook est prive."}
        else:
            return {"success": False, "error": f"Erreur Facebook: {error_msg[:150]}"}

@app.post("/resolve")
async def resolve_video(req: ResolveRequest):
    url = req.url.strip()

    blocked = ["youtube.com", "youtu.be"]
    if any(b in url.lower() for b in blocked):
        raise HTTPException(status_code=403, detail="YouTube non supporte")

    if "tiktok.com" in url.lower() or "tiktok" in url.lower():
        if "/story/" in url.lower() or "/photo/" in url.lower():
            return await resolve_tiktok_story(url)
        return await resolve_tiktok(url)

    if "twitter.com" in url.lower() or "x.com" in url.lower() or "t.co" in url.lower():
        return await resolve_twitter(url)

    if "instagram.com" in url.lower():
        return await resolve_instagram(url)

    if "facebook.com" in url.lower() or "fb.watch" in url.lower() or "fb.com" in url.lower():
        return await resolve_facebook(url)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best[ext=mp4]/best",
        "socket_timeout": 30,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            ext = info.get("ext", "mp4")
            is_image = ext in ["jpg", "jpeg", "png", "webp"]
            direct_url = ""
            if "url" in info:
                direct_url = info["url"]
            elif "formats" in info and info["formats"]:
                mp4_formats = [f for f in info["formats"] if f.get("ext") == "mp4" and f.get("url")]
                if mp4_formats:
                    direct_url = mp4_formats[-1]["url"]
                else:
                    direct_url = info["formats"][-1].get("url", "")
            if not direct_url:
                return {"success": False, "error": "Impossible de resoudre cette URL"}
            platform = "unknown"
            for p in ["facebook", "instagram", "tiktok", "twitter", "x.com"]:
                if p in url.lower():
                    platform = p
                    break
            return {
                "success": True,
                "direct_url": direct_url,
                "title": info.get("title", "Media sans titre"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": int(info.get("duration") or 0),
                "platform": platform,
                "ext": ext,
                "is_image": is_image,
                "all_images": []
            }
    except Exception as e:
        error_msg = str(e)
        if "private" in error_msg.lower():
            return {"success": False, "error": "Ce contenu est prive ou inaccessible."}
        elif "login" in error_msg.lower():
            return {"success": False, "error": "Connexion requise pour acceder a ce contenu."}
        elif "not found" in error_msg.lower():
            return {"success": False, "error": "Contenu introuvable ou supprime."}
        else:
            return {"success": False, "error": error_msg[:200]}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
