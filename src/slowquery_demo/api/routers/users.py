"""Routes for User."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/")
async def users_list() -> dict[str, str]:
    """GET /users"""
    return {"handler": "users.list"}


@router.get("/{id}")
async def users_get() -> dict[str, str]:
    """GET /users/{id}"""
    return {"handler": "users.get"}
