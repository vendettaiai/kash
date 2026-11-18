"""Database repository for login accounts (``users``)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import UserModel
from kash.domain.models import User


def _to_domain(model: UserModel) -> User:
    return User(
        id=model.id,
        email=model.email,
        hashed_password=model.hashed_password,
        is_active=model.is_active,
        display_name=model.display_name or "",
        theme=model.theme,
        font_scale=model.font_scale,
        notification_prefs=model.notification_prefs or {},
        invite_token_hash=model.invite_token_hash,
        invite_expires_at=model.invite_expires_at,
    )


class SqlUserRepository:
    """SQL-backed implementation of UserRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def _update(self, user_id: UUID, **values: object) -> None:
        async with self._session_factory() as session:
            model = await session.get(UserModel, user_id)
            if model is None:
                raise ValueError(f"User {user_id} not found")
            for key, value in values.items():
                setattr(model, key, value)
            await session.commit()

    async def get_user(self, user_id: UUID) -> User | None:
        async with self._session_factory() as session:
            model = await session.get(UserModel, user_id)
            return _to_domain(model) if model else None

    async def get_user_by_email(self, email: str) -> User | None:
        async with self._session_factory() as session:
            stmt = select(UserModel).where(func.lower(UserModel.email) == email.strip().lower())
            model = (await session.execute(stmt)).scalars().first()
            return _to_domain(model) if model else None

    async def get_user_by_invite(self, token_hash: str) -> User | None:
        async with self._session_factory() as session:
            stmt = select(UserModel).where(UserModel.invite_token_hash == token_hash)
            model = (await session.execute(stmt)).scalars().first()
            return _to_domain(model) if model else None

    async def list_users(self) -> list[User]:
        async with self._session_factory() as session:
            rows = await session.execute(select(UserModel).order_by(UserModel.created_at))
            return [_to_domain(m) for m in rows.scalars()]

    async def create_user(self, user: User) -> User:
        async with self._session_factory() as session:
            model = UserModel(
                id=user.id,
                email=user.email,
                hashed_password=user.hashed_password,
                is_active=user.is_active,
                display_name=user.display_name or None,
                invite_token_hash=user.invite_token_hash,
                invite_expires_at=user.invite_expires_at,
            )
            session.add(model)
            await session.commit()
            return _to_domain(model)

    async def delete_user(self, user_id: UUID) -> None:
        async with self._session_factory() as session:
            model = await session.get(UserModel, user_id)
            if model is not None:
                await session.delete(model)
                await session.commit()

    async def save_credentials(self, user_id: UUID, email: str, hashed_password: str) -> None:
        await self._update(
            user_id,
            email=email,
            hashed_password=hashed_password,
            invite_token_hash=None,
            invite_expires_at=None,
        )

    async def save_display_name(self, user_id: UUID, display_name: str) -> None:
        await self._update(user_id, display_name=display_name or None)

    async def save_preferences(self, user_id: UUID, theme: str, font_scale: int) -> None:
        await self._update(user_id, theme=theme, font_scale=font_scale)

    async def save_notification_prefs(self, user_id: UUID, prefs: dict[str, Any]) -> None:
        await self._update(user_id, notification_prefs=prefs)

    async def save_invite(self, user_id: UUID, token_hash: str, expires_at: datetime) -> None:
        await self._update(user_id, invite_token_hash=token_hash, invite_expires_at=expires_at)
