import asyncio
import sys
from fastapi import FastAPI
from app.laptops.router import router as laptops_router
from app.users.router import router as users_router
from app.scraper.router import router as scraper_router

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

app = FastAPI(
    title="PickWise v2 API",
    description="A Smart Recommendation System Backend",
    version="2.0"
)

# declare routes before including routers to avoid circular imports
app.include_router(laptops_router)
app.include_router(users_router)
app.include_router(scraper_router)

@app.get("/")
def root():
    return {"status": "healthy", "project": "PickWise v2 Backend"}