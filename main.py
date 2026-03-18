from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import uvicorn

app = FastAPI(title="SocialVault Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ResolveRequest(BaseModel):
    url: str

class VideoResponse(BaseModel):
    success: bool
    direct_url: str = ""
    title: str = ""
    thumbnail: str = ""
    duration: int = 0
    platform: str = ""
    ext: str = "mp4"
    error: str = ""

@app.get("/")
def root():
    return {"status": "SocialVault Backend running"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/resolve", response_model=VideoResponse)
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
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            direct_url = ""
            if "url" in info:
                direct_url = info["url"]
            elif "formats" in info and info["formats"]:
                mp4_formats = [
                    f for f in info["formats"]
                    if f.get("ext") == "mp4" and f.get("url")
                ]
                if mp4_formats:
                    direct_url = mp4_formats[-1]["url"]
                else:
                    direct_url = info["formats"][-1].get("url", "")

            if not direct_url:
                return VideoResponse(
                    success=False,
                    error="Impossible de resoudre URL"
                )

            platform = "unknown"
            for p in ["facebook", "instagram", "tiktok", "twitter", "x.com"]:
                if p in url.lower():
                    platform = p
                    break

            return VideoResponse(
                success=True,
                direct_url=direct_url,
                title=info.get("title", "Video sans titre"),
                thumbnail=info.get("thumbnail", ""),
                duration=int(info.get("duration") or 0),
                platform=platform,
                ext=info.get("ext", "mp4")
            )

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        if "Private" in error_msg or "private" in error_msg:
            msg = "Contenu prive"
        elif "login" in error_msg.lower():
            msg = "Connexion requise"
        elif "not found" in error_msg.lower():
            msg = "Video introuvable"
        else:
            msg = "Erreur: " + error_msg[:200]
        return VideoResponse(success=False, error=msg)

    except Exception as e:
        return VideoResponse(success=False, error="Erreur serveur: " + str(e)[:200])

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)