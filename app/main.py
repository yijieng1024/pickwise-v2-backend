from fastapi import FastAPI
from app.laptops.router import router as laptops_router
from app.users.router import router as users_router

app = FastAPI(
    title="PickWise v2 API",
    description="A Smart Laptop Recommendation System Backend",
    version="2.0.0"
)

# declare routes before including routers to avoid circular imports
app.include_router(laptops_router)
app.include_router(users_router)

@app.get("/")
def root():
    return {"status": "healthy", "project": "PickWise v2 Backend"}