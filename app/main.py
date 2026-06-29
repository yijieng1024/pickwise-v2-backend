

from fastapi import FastAPI
from app.logger import setup_logging
from app.laptops.customization_router import router as customization_router
from app.laptops.laptop_router import router as laptops_router
from app.laptops.brand_router import router as brands_router
from app.users.router import router as users_router
from app.scraper.router import router as scraper_router
from app.processor.router import router as processor_router
from app.benchmark.router import router as benchmark_router
from app.laptops.pickscore_router import router as pickscore_router
from app.embeddings.router import router as embedding_router
from app.recommendation.router import router as recommendation_router
from app.conversations.router import router as conversations_router

setup_logging()

app = FastAPI(
    title="PickWise v2 API",
    description="A Smart Recommendation System Backend",
    version="2.0",
)

# declare routes before including routers to avoid circular imports
app.include_router(users_router)
app.include_router(laptops_router)
app.include_router(brands_router)
app.include_router(customization_router)
app.include_router(scraper_router)
app.include_router(processor_router)
app.include_router(benchmark_router)
app.include_router(pickscore_router)
app.include_router(embedding_router)
app.include_router(recommendation_router)
app.include_router(conversations_router)


@app.get("/")
def root():
    return {"status": "healthy", "project": "PickWise v2 Backend"}
