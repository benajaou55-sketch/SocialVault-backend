from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yt_dlp
import uvicorn
import httpx
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ResolveRequest(BaseModel):
    url: str

@app.get("/")
def root():
    return {"status": "SocialVault Backend running"}

@app.get("/health")
def health():
    return {"status": "ok"}

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

            # Videos
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

            # GIFs
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

            # Photos
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

async def resolve_instagram(url: str) -> dict:
    # Essai 1 — yt-dlp sans cookies
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best",
        "socket_timeout": 30,
        "extract_flat": False,
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

    # Essai 2 — API Instagram publique
    try:
        shortcode = url.rstrip("/").split("/")[-1]
        if not shortcode:
            parts = url.rstrip("/").split("/")
            shortcode = parts[-1] if parts else ""

        api_url = f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis"
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(api_url, headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 14)",
                "Accept": "application/json"
            })
            if resp.status_code == 200:
                data = resp.json()
                item = data.get("graphql", {}).get("shortcode_media", {})
                if not item:
                    item = data.get("items", [{}])[0] if data.get("items") else {}

                media_type = item.get("media_type", 1)
                if media_type == 2:  # Video
                    versions = item.get("video_versions", [])
                    if versions:
                        return {
                            "success": True,
                            "direct_url": versions[0].get("url", ""),
                            "title": item.get("caption", {}).get("text", "Video Instagram")[:100] if item.get("caption") else "Video Instagram",
                            "thumbnail": item.get("image_versions2", {}).get("candidates", [{}])[0].get("url", ""),
                            "duration": int(item.get("video_duration", 0)),
                            "platform": "instagram",
                            "ext": "mp4",
                            "is_image": False,
                            "all_images": []
                        }
                else:  # Photo
                    candidates = item.get("image_versions2", {}).get("candidates", [])
                    if candidates:
                        return {
                            "success": True,
                            "direct_url": candidates[0].get("url", ""),
                            "title": item.get("caption", {}).get("text", "Photo Instagram")[:100] if item.get("caption") else "Photo Instagram",
                            "thumbnail": candidates[0].get("url", ""),
                            "duration": 0,
                            "platform": "instagram",
                            "ext": "jpg",
                            "is_image": True,
                            "all_images": []
                        }
    except Exception:
        pass

    # Essai 3 — API ddinstagram
    try:
        shortcode = ""
        parts = url.rstrip("/").split("/")
        for i, p in enumerate(parts):
            if p in ["p", "reel", "tv"]:
                if i + 1 < len(parts):
                    shortcode = parts[i + 1]
                    break

        if shortcode:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(
                    f"https://ddinstagram.com/p/{shortcode}/",
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                if resp.status_code == 200:
                    video_urls = re.findall(r'https://[^"\']*\.mp4[^"\']*', resp.text)
                    image_urls = re.findall(r'https://[^"\']*\.jpg[^"\']*', resp.text)
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
        "error": "Impossible de telecharger ce contenu Instagram. Seuls les contenus publics sont supportes. Les stories et contenus prives necessitent une connexion."
    }

async def resolve_facebook(url: str) -> dict:
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
                return {"success": False, "error": "Impossible d'extraire ce contenu Facebook. Il est peut-etre prive."}
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
            return {"success": False, "error": "Ce contenu Facebook necessite une connexion. Seuls les contenus publics sont supportes."}
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
