"""User prep list endpoints (all require authentication).

Routes
------
GET    /api/preparations                -> list my prep items (with competition info)
POST   /api/preparations/{competition_id} -> add (idempotent)
DELETE /api/preparations/{competition_id} -> remove
GET    /api/preparations/check/{competition_id} -> {"prepared": bool}

Note: the /check/... path is declared before the generic
/{competition_id} handlers is NOT required because methods differ,
but keep the literal prefix stable to avoid future shadowing.
"""

from fastapi import APIRouter, Depends, HTTPException, Path, status

import db
from security import get_current_user

router = APIRouter(prefix="/api/preparations", tags=["preparations"])


@router.get("")
def list_my_preparations(current_user: dict = Depends(get_current_user)):
    """Return the current user's prep list, newest first."""
    items = db.list_preparations(current_user["id"])
    return {"code": 0, "total": len(items), "items": items}


@router.get("/check/{competition_id}")
def check_prepared(
    competition_id: int = Path(..., ge=1),
    current_user: dict = Depends(get_current_user),
):
    """Whether a given competition is in my prep list."""
    return {
        "code": 0,
        "competition_id": competition_id,
        "prepared": db.is_prepared(current_user["id"], competition_id),
    }


@router.post("/{competition_id}", status_code=status.HTTP_200_OK)
def add_to_preparations(
    competition_id: int = Path(..., ge=1),
    current_user: dict = Depends(get_current_user),
):
    """Add a competition to my prep list. Idempotent."""
    try:
        result = db.add_preparation(current_user["id"], competition_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="竞赛不存在",
        )
    return {
        "code": 0,
        "competition_id": competition_id,
        "prep_id": result["id"],
        "created": result["created"],
        "message": "已加入备赛" if result["created"] else "已在备赛中",
    }


@router.delete("/{competition_id}")
def remove_from_preparations(
    competition_id: int = Path(..., ge=1),
    current_user: dict = Depends(get_current_user),
):
    """Remove a competition from my prep list."""
    removed = db.remove_preparation(current_user["id"], competition_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该竞赛不在备赛清单中",
        )
    return {"code": 0, "competition_id": competition_id, "message": "已移出备赛"}
