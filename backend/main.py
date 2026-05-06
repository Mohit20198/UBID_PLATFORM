"""
main.py
-------
UBID Platform — FastAPI application entry point.

Day 2 deliverables:
  GET  /review-queue        → feeds Kavyansh's UI with candidate pairs
  POST /decision            → accepts merge/reject clicks, writes immutably to audit_log
  GET  /health              → liveness probe

Run:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import review, decision, health, query
from database import engine, Base

# ── Create tables if they don't exist (non-destructive) ──────
# In production, Alembic handles migrations. This is a safety net.
# Base.metadata.create_all(bind=engine)  # uncomment if needed

app = FastAPI(
    title="UBID Platform API",
    description="Unified Business Identity — entity resolution backend",
    version="0.2.0",
)

# ── CORS (Kavyansh's React UI runs on a different port) ──────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────
app.include_router(health.router, tags=["ops"])
app.include_router(review.router, prefix="/review-queue", tags=["review"])
app.include_router(decision.router, prefix="/decision", tags=["decision"])
app.include_router(query.router, prefix="/query", tags=["query"])
