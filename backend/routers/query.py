"""
routers/query.py
----------------
GET /query — complex business intelligence queries.
Includes logic to attribute activity events to UBIDs.
"""

from __future__ import annotations

from typing import Optional, List
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session
from database import get_db

router = APIRouter()

@router.post("/refresh-attribution", status_code=200)
def refresh_event_attribution(db: Session = Depends(get_db)):
    """
    SQL logic to map activity events to UBIDs based on current membership.
    Run this after significant merge activity.
    """
    query = """
        UPDATE activity_events ae
        SET ubid = um.ubid
        FROM ubid_members um
        WHERE ae.source::text = um.source::text
          AND ae.source_record_id = um.source_record_id
          AND um.removed_at IS NULL
          AND (ae.ubid IS NULL OR ae.ubid != um.ubid);
    """
    result = db.execute(text(query))
    db.commit()
    return {"message": f"Attributed {result.rowcount} events to UBIDs"}

@router.get("", response_model=List[dict])
def complex_query(
    pin_code: Optional[str] = Query(None, description="Filter by pin code"),
    sector: Optional[str] = Query(None, description="Filter by sector (e.g. FACTORIES)"),
    status: Optional[str] = Query(None, description="Filter by business status (ACTIVE, DORMANT, CLOSED)"),
    no_event_type: Optional[str] = Query(None, description="Filter for UBIDs lacking this event type (e.g. INSPECTION)"),
    db: Session = Depends(get_db)
):
    """
    Advanced query endpoint for the demo.
    Example: Find active factories in pin code 560058 without recent inspections.
    """
    
    # Base query joining registry with members and normalised records to get canonical-like info if registry is empty
    # In a real system, we'd use the canonical_* columns in ubid_registry.
    # For now, we'll derive it from the members.
    
    sql = """
        WITH ubid_info AS (
            SELECT 
                r.ubid,
                r.status,
                COALESCE(r.canonical_name, MAX(nr.name_normalised)) as name,
                COALESCE(r.canonical_pin_code, MAX(nr.addr_pin_code)) as pin_code,
                COALESCE(r.canonical_sector, MAX(nr.sector)) as sector
            FROM ubid_registry r
            JOIN ubid_members m ON r.ubid = m.ubid
            JOIN normalised_records nr ON m.normalised_id = nr.id
            WHERE m.removed_at IS NULL
            GROUP BY r.ubid, r.status, r.canonical_name, r.canonical_pin_code, r.canonical_sector
        )
        SELECT * FROM ubid_info ui
        WHERE 1=1
    """
    params = {}
    
    if pin_code:
        sql += " AND ui.pin_code = :pin_code"
        params["pin_code"] = pin_code
        
    if sector:
        sql += " AND ui.sector ILIKE :sector"
        params["sector"] = f"%{sector}%"
        
    if status:
        sql += " AND ui.status = :status"
        params["status"] = status
        
    if no_event_type:
        sql += """
            AND NOT EXISTS (
                SELECT 1 FROM activity_events ae 
                WHERE ae.ubid = ui.ubid 
                AND ae.event_category::text = :no_event_type
            )
        """
        params["no_event_type"] = no_event_type.upper()

    sql += " LIMIT 100"
    
    rows = db.execute(text(sql), params).mappings().all()
    return [dict(r) for r in rows]
