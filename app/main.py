

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
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
from app.rag.router import router as rag_router
from app.agent.router import router as agent_router
from app.reviews.router import router as reviews_router
from app.taxonomy.product_type_router import router as product_type_router
from app.taxonomy.category_router import router as category_router
from app.users.questionnaire_router import router as questionnaire_router

setup_logging()

app = FastAPI(
    title="PickWise v2 API",
    description="Backend for PickWise v2 — a LangGraph ReAct agent that reasons over laptop search, " \
    "PickScore ranking, and pricing to deliver conversational recommendations",
    version="2.0",
)

API_PREFIX = "/api/v2"

# Browser clients (the Next.js frontend) need CORS headers; requests are
# authenticated with bearer tokens, not cookies, so no credentials needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

# declare routes before including routers to avoid circular imports
app.include_router(users_router, prefix=API_PREFIX)
app.include_router(laptops_router, prefix=API_PREFIX)
app.include_router(brands_router, prefix=API_PREFIX)
app.include_router(customization_router, prefix=API_PREFIX)
app.include_router(scraper_router, prefix=API_PREFIX)
app.include_router(processor_router, prefix=API_PREFIX)
app.include_router(benchmark_router, prefix=API_PREFIX)
app.include_router(pickscore_router, prefix=API_PREFIX)
app.include_router(embedding_router, prefix=API_PREFIX)
app.include_router(recommendation_router, prefix=API_PREFIX)
app.include_router(rag_router, prefix=API_PREFIX)
app.include_router(agent_router, prefix=API_PREFIX)
app.include_router(reviews_router, prefix=API_PREFIX)
app.include_router(product_type_router, prefix=API_PREFIX)
app.include_router(category_router, prefix=API_PREFIX)
app.include_router(questionnaire_router, prefix=API_PREFIX)


@app.get("/")
def root():
    return {
        "status": "healthy",
        "version": "2.0.0",
        "service": "pickwise-v2-api",
        "project": "PickWise v2 Backend"
    }
