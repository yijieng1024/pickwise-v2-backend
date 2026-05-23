from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/register")
def register():
    return {"message": "User registration endpoint skeleton"}

@router.post("/login")
def login():
    return {"message": "User login endpoint skeleton"}