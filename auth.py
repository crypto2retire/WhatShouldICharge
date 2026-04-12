import os
import secrets
import bcrypt
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import Request, HTTPException
from sqlalchemy import select

from database import AsyncSessionLocal
from models import User, Session as SessionModel, TeamMember, TeamSession, PasswordReset

logger = logging.getLogger("wsic.auth")


async def get_current_user(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        return None
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SessionModel).where(SessionModel.token == token)
        )
        sess = result.scalar_one_or_none()
        if not sess or sess.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
            return None
        result = await db.execute(select(User).where(User.id == sess.user_id))
        return result.scalar_one_or_none()


async def require_user(request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return user


async def require_admin(request: Request):
    user = await require_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


async def get_team_member(request: Request):
    token = request.cookies.get("team_token")
    if not token:
        return None, None, None
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TeamSession).where(TeamSession.token == token)
        )
        tsess = result.scalar_one_or_none()
        if not tsess or tsess.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
            return None, None, None
        result = await db.execute(
            select(TeamMember).where(TeamMember.id == tsess.team_member_id)
        )
        member = result.scalar_one_or_none()
        if not member or not member.is_active:
            return None, None, None
        result = await db.execute(
            select(User).where(User.id == tsess.owner_user_id)
        )
        owner = result.scalar_one_or_none()
        return member, owner, tsess


async def require_team_member(request: Request):
    member, owner, tsess = await get_team_member(request)
    if not member:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return member, owner, tsess
