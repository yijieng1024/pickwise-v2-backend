from fastapi import FastAPI
from app.database import init_db

from app.api.endpoints import laptops

app = FastAPI(
    title="PickWise API v2",
    description="Agentic Backend for PickWise Laptop Recommendation System",
    version="2.0.0"
)

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/")
def read_root():
    return {"message": "Welcome to PickWise v2 Backend! System is online."}

# Routers
# prefix="/laptops" means all routes in laptops.router will be prefixed with /laptops
app.include_router(laptops.router, prefix="/laptops", tags=["Laptops"])

# app.include_router(users.router, prefix="/users", tags=["Users"])
# app.include_router(chat.router, prefix="/chat", tags=["AI Agent"])
# app.include_router(scraper.router, prefix="/scraper", tags=["ETL Pipeline"])