

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.logger import setup_logging
from app.laptops.customization_router import router as customization_router
from app.laptops.laptop_router import router as laptops_router
from app.laptops.brand_router import router as brands_router
from app.users.router import router as users_router
from app.users.admin_router import router as users_admin_router
from app.scraper.router import router as scraper_router
from app.processor.router import router as processor_router
from app.benchmark.router import router as benchmark_router
from app.laptops.pickscore_router import router as pickscore_router
from app.embeddings.router import router as embedding_router
from app.recommendation.router import router as recommendation_router
from app.rag.router import router as rag_router
from app.agent.router import router as agent_router
from app.agent.monitoring_router import router as agent_monitoring_router
from app.reviews.router import router as reviews_router
from app.taxonomy.product_type_router import router as product_type_router
from app.taxonomy.category_router import router as category_router
from app.users.questionnaire_router import router as questionnaire_router
from app.saved.router import router as saved_router
from app.common.job_router import router as jobs_router
from app.common.job_service import reset_stale_jobs

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
    # Custom response headers are invisible to browser JS unless explicitly
    # exposed. X-Total-Count carries the row count for list endpoints that
    # deliberately kept a bare-array response body (see /laptops/,
    # /benchmarks/cpu, /benchmarks/gpu) instead of a {items,total} envelope.
    expose_headers=["X-Total-Count"],
)

# declare routes before including routers to avoid circular imports
app.include_router(users_router, prefix=API_PREFIX)
app.include_router(users_admin_router, prefix=API_PREFIX)
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
app.include_router(agent_monitoring_router, prefix=API_PREFIX)
app.include_router(reviews_router, prefix=API_PREFIX)
app.include_router(product_type_router, prefix=API_PREFIX)
app.include_router(category_router, prefix=API_PREFIX)
app.include_router(questionnaire_router, prefix=API_PREFIX)
app.include_router(saved_router, prefix=API_PREFIX)
app.include_router(jobs_router, prefix=API_PREFIX)


@app.on_event("startup")
def _recover_interrupted_jobs() -> None:
    """
    Background jobs run in-process, so a deploy or crash orphans anything still
    running. Fail those rows on boot — otherwise the admin UI polls a
    `processing` job that no longer exists, forever. Never raises.
    """
    reset_stale_jobs()


@app.get("/")
def root():
    return {
        "status": "healthy",
        "version": "2.0.0",
        "service": "pickwise-v2-api",
        "project": "PickWise v2 Backend"
    }
