"""
routers/health.py
-----------------
GET /health — liveness + DB connectivity probe.
Used by Arpan's Docker healthcheck and the demo startup script.
"""

from fastapi import APIRouter
from database import ping_db
from schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health():
    db_ok = ping_db()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        db_ok=db_ok,
    )
