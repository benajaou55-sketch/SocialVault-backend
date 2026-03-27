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
            play_url = video_data.get("hdplay") or video_data.get("play")
            if play_url:
                return {
                    "success": True,
                    "direct_url": play_url,
                    "title": video_data.get("title", "TikTok Video"),
                    "thumbnail": video_data.get("cover", ""),
                    "duration": video_data.get("duration", 0),
                    "platform": "tiktok",
                    "ext": "mp4"
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
                "ext": "mp4"
            }
    except Exception:
        pass

    return {"success": False, "error": "Impossible de resoudre cette video TikTok"}

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
                        "ext": "mp4"
                    }
            return {
                "success": False,
                "error": "Ce tweet ne contient pas de video"
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
            if media.get("type") == "video":
                return {
                    "success": True,
                    "direct_url": media.get("url", ""),
                    "title": data.get("text", "Video X")[:100],
                    "thumbnail": data.get("tweetThumbnailUrl", ""),
                    "duration": 0,
                    "platform": "twitter",
                    "ext": "mp4"
                }
        return {
            "success": False,
            "error": "Ce tweet ne contient pas de video"
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
                        "ext": "mp4"
                    }
    except Exception:
        pass

    return {
        "success": False,
        "error": "Impossible de telecharger cette video X. Verifiez que le tweet est public et contient une video."
    }

@app.post("/resolve")
async def resolve_video(req: ResolveRequest):
    url = req.url.strip()

    blocked = ["youtube.com", "youtu.be"]
    if any(b in url.lower() for b in blocked):
        raise HTTPException(status_code=403, detail="YouTube non supporte")

    if "tiktok.com" in url.lower() or "tiktok" in url.lower():
        return await resolve_tiktok(url)

    if "twitter.com" in url.lower() or "x.com" in url.lower() or "t.co" in url.lower():
        return await resolve_twitter(url)

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
                "title": info.get("title", "Video sans titre"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": int(info.get("duration") or 0),
                "platform": platform,
                "ext": info.get("ext", "mp4")
            }
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
