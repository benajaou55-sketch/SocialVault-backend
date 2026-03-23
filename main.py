from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
    except Exception as e:
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
    except Exception as e:
        pass

    return {"success": False, "error": "Impossible de resoudre cette video TikTok"}

@app.post("/resolve")
async def resolve_video(req: ResolveRequest):
    url = req.url.strip()
    blocked = ["youtube.com", "youtu.be"]
    if any(b in url.lower() for b in blocked):
        raise HTTPException(status_code=403, detail="YouTube non supporte")

    if "tiktok.com" in url.lower() or "tiktok" in url.lower():
        return await resolve_tiktok(url)

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
                return {"success": False, "error": "Impossible de resoudre URL"}
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
```

---

## 🟢 Mettre à jour requirements.txt aussi
```
fastapi==0.110.0
uvicorn==0.29.0
yt-dlp
pydantic==2.6.4
httpx==0.27.0
