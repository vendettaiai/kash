"""Database Bank Connection repository."""

from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import BankConnectionModel
from kash.db.repositories.mappers import bank_connection_to_domain
from kash.domain.models import BankConnection


def _pending_json(connection: BankConnection) -> list[dict[str, Any]] | None:
    return [p.model_dump(mode="json") for p in connection.pending_accounts] or None


class SqlBankConnectionRepository:
    """SQL-backed implementation of BankConnectionRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def list_connections(self, user_id: UUID) -> list[BankConnection]:
        async with self._session_factory() as session:
            stmt = (
                select(BankConnectionModel)
                .where(BankConnectionModel.user_id == user_id)
                .order_by(BankConnectionModel.created_at.desc())
            )
            result = await session.execute(stmt)
            return [bank_connection_to_domain(row) for row in result.scalars().all()]

    async def get_connection(self, connection_id: UUID) -> BankConnection | None:
        async with self._session_factory() as session:
            stmt = select(BankConnectionModel).where(BankConnectionModel.id == connection_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return bank_connection_to_domain(row) if row else None

    async def create_connection(self, connection: BankConnection) -> BankConnection:
        async with self._session_factory() as session:
            model = BankConnectionModel(
                id=connection.id,
                user_id=connection.user_id,
                provider_name=connection.provider_name,
                institution_name=connection.institution_name,
                encrypted_credentials=connection.encrypted_credentials,
                status=connection.status.value,
                last_synced_at=connection.last_synced_at,
                last_error_message=connection.last_error_message,
                pending_accounts=_pending_json(connection),
                retired_account_ids=connection.retired_account_ids or None,
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)
            return bank_connection_to_domain(model)

    async def update_connection(self, connection: BankConnection) -> BankConnection:
        async with self._session_factory() as session:
            model = await session.get(BankConnectionModel, connection.id)
            if not model:
                raise ValueError(f"BankConnection {connection.id} not found")

            model.institution_name = connection.institution_name
            model.encrypted_credentials = connection.encrypted_credentials
            model.status = connection.status.value
            model.last_synced_at = connection.last_synced_at
            model.last_error_message = connection.last_error_message
            model.pending_accounts = _pending_json(connection)
            model.retired_account_ids = connection.retired_account_ids or None

            await session.commit()
            await session.refresh(model)
            return bank_connection_to_domain(model)

    async def delete_connection(self, connection_id: UUID) -> bool:
        async with self._session_factory() as session:
            stmt = delete(BankConnectionModel).where(BankConnectionModel.id == connection_id)
            result = await session.execute(stmt)
            await session.commit()
            return bool(result.rowcount and result.rowcount > 0)
