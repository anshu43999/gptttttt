"""
GPT Register — FastAPI Application Entry
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path

from infrastructure.db import init_db

PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    activation_service = None
    try:
        from services.upi_activation_service import get_upi_activation_service
        activation_service = get_upi_activation_service()
        activation_service.ensure_worker()
        yield
    finally:
        if activation_service is not None:
            activation_service.shutdown()

app = FastAPI(
    title="GPT Register",
    version="2.0.0",
    description="ChatGPT Phone Registration + Codex OAuth Binding + ICEAIX Plus Activation",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Compress large JSON (accounts list can be tens of MB uncompressed).
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ── API routes ────────────────────────────────────────────
from api import accounts, register, tasks, providers, config, email_webhook, stats, resources

app.include_router(accounts.router, prefix="/api", tags=["accounts"])
app.include_router(register.router, prefix="/api", tags=["register"])
app.include_router(tasks.router, prefix="/api", tags=["tasks"])
app.include_router(providers.router, prefix="/api", tags=["providers"])
app.include_router(config.router, prefix="/api", tags=["config"])
app.include_router(email_webhook.router, prefix="/api", tags=["email"])
app.include_router(stats.router, prefix="/api", tags=["stats"])
app.include_router(resources.router, prefix="/api", tags=["resources"])

# ── Health check ──────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}

# ── Static assets + SPA fallback ──────────────────────────
if FRONTEND_DIST.exists():
    # JS/CSS/static assets first
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")
    
    # SPA fallback for all non-API routes
    from fastapi.responses import FileResponse
    index = FRONTEND_DIST / "index.html"
    
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        return FileResponse(index)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
