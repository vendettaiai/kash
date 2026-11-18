"""Database repository for custom merchant logos."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import MerchantIconModel
from kash.domain.models import MerchantIcon


def _icon(m: MerchantIconModel, with_data: bool = True) -> MerchantIcon:
    return MerchantIcon(
        id=m.id,
        user_id=m.user_id,
        merchant=m.merchant,
        content_type=m.content_type,
        data=m.data if with_data else b"",
        updated_at=m.updated_at,
    )


class SqlMerchantIconRepository:
    """SQL-backed implementation of MerchantIconRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def list_icons(self, user_id: UUID) -> list[MerchantIcon]:
        """All of the user's icons, without the image bytes."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    MerchantIconModel.id,
                    MerchantIconModel.user_id,
                    MerchantIconModel.merchant,
                    MerchantIconModel.content_type,
                    MerchantIconModel.updated_at,
                ).where(MerchantIconModel.user_id == user_id)
            )
            return [
                MerchantIcon(
                    id=r.id,
                    user_id=r.user_id,
                    merchant=r.merchant,
                    content_type=r.content_type,
                    updated_at=r.updated_at,
                )
                for r in result.all()
            ]

    async def get_icon(self, icon_id: UUID) -> MerchantIcon | None:
        async with self._session_factory() as session:
            model = await session.get(MerchantIconModel, icon_id)
            return _icon(model) if model else None

    async def save_icon(self, icon: MerchantIcon) -> MerchantIcon:
        """Insert, or replace the image of the merchant's existing icon."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(MerchantIconModel).where(
                    MerchantIconModel.user_id == icon.user_id,
                    MerchantIconModel.merchant == icon.merchant,
                )
            )
            model = result.scalar_one_or_none()
            if model is None:
                model = MerchantIconModel(id=icon.id, user_id=icon.user_id, merchant=icon.merchant)
                session.add(model)
            model.content_type = icon.content_type
            model.data = icon.data
            model.updated_at = datetime.now(UTC)
            await session.commit()
            return _icon(model)

    async def delete_icon(self, icon_id: UUID) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(MerchantIconModel).where(MerchantIconModel.id == icon_id)
            )
            await session.commit()
            return bool(result.rowcount)  # type: ignore[attr-defined]
