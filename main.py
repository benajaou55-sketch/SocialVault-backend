from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import uvicorn

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

@app.post("/resolve")
async def resolve_video(req: ResolveRequest):
    url = req.url.strip()
    blocked = ["youtube.com", "youtu.be"]
    if any(b in url.lower() for b in blocked):
        raise HTTPException(status_code=403, detail="YouTube non supporte")
    ydl_opts = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "format": "best[ext=mp4]/best",
    "socket_timeout": 30,
    "cookiefile": "tiktok.com_cookies.txt",
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
