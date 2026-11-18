"""Database Transaction ledger repository."""

from datetime import date
from decimal import Decimal
from typing import TypeGuard
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import AccountModel, TransactionModel
from kash.db.repositories.mappers import transaction_to_domain
from kash.domain.models import Transaction


def _tracks_ledger_balance(account: AccountModel | None) -> TypeGuard[AccountModel]:
    """Whether Kash maintains this account's balance from its transactions.

    Bank-linked accounts get their balance from the bank on every sync, so adding
    imported transactions on top would count them twice. Only manual accounts
    (no bank connection) keep a running balance here.
    """
    return account is not None and account.connection_id is None


async def pending_totals(session: AsyncSession, user_id: UUID) -> dict[UUID, Decimal]:
    """Sum of each account's pending transactions; accounts with none are left out."""
    result = await session.execute(
        select(TransactionModel.account_id, TransactionModel.amount).where(
            TransactionModel.user_id == user_id, TransactionModel.is_pending.is_(True)
        )
    )
    totals: dict[UUID, Decimal] = {}
    for account_id, amount in result.all():
        totals[account_id] = totals.get(account_id, Decimal("0")) + amount
    return totals


class SqlTransactionRepository:
    """SQL-backed implementation of TransactionRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def list_transactions(
        self,
        user_id: UUID,
        account_id: UUID | None = None,
        category_id: UUID | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Transaction]:
        async with self._session_factory() as session:
            stmt = select(TransactionModel).where(TransactionModel.user_id == user_id)

            if account_id is not None:
                stmt = stmt.where(TransactionModel.account_id == account_id)
            if category_id is not None:
                stmt = stmt.where(TransactionModel.category_id == category_id)
            if start_date is not None:
                stmt = stmt.where(TransactionModel.date >= start_date)
            if end_date is not None:
                stmt = stmt.where(TransactionModel.date <= end_date)
            if search:
                pattern = f"%{search.strip()}%"
                stmt = stmt.where(
                    or_(
                        TransactionModel.clean_description.ilike(pattern),
                        TransactionModel.original_description.ilike(pattern),
                        TransactionModel.merchant_name.ilike(pattern),
                        TransactionModel.notes.ilike(pattern),
                    )
                )

            stmt = stmt.order_by(TransactionModel.date.desc(), TransactionModel.created_at.desc())
            stmt = stmt.limit(limit).offset(offset)

            result = await session.execute(stmt)
            return [transaction_to_domain(row) for row in result.scalars().all()]

    async def get_transaction(self, tx_id: UUID) -> Transaction | None:
        async with self._session_factory() as session:
            stmt = select(TransactionModel).where(TransactionModel.id == tx_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return transaction_to_domain(row) if row else None

    async def get_by_external_id(self, account_id: UUID, external_id: str) -> Transaction | None:
        async with self._session_factory() as session:
            stmt = select(TransactionModel).where(
                TransactionModel.account_id == account_id,
                TransactionModel.external_id == external_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return transaction_to_domain(row) if row else None

    async def find_by_fingerprint(self, account_id: UUID, fingerprint: str) -> Transaction | None:
        matches = await self.list_by_fingerprint(account_id, fingerprint)
        return matches[0] if matches else None

    async def list_by_fingerprint(self, account_id: UUID, fingerprint: str) -> list[Transaction]:
        """Every transaction of the account with this fingerprint, oldest first (two
        identical purchases on one day share one)."""
        async with self._session_factory() as session:
            stmt = (
                select(TransactionModel)
                .where(
                    TransactionModel.account_id == account_id,
                    TransactionModel.fingerprint == fingerprint,
                )
                .order_by(TransactionModel.created_at, TransactionModel.id)
            )
            result = await session.execute(stmt)
            return [transaction_to_domain(row) for row in result.scalars().all()]

    async def pending_totals(self, user_id: UUID) -> dict[UUID, Decimal]:
        """Sum of each account's pending transactions; accounts with none are left out."""
        async with self._session_factory() as session:
            return await pending_totals(session, user_id)

    async def create_transaction(self, tx: Transaction) -> Transaction:
        async with self._session_factory() as session:
            model = TransactionModel(
                id=tx.id,
                user_id=tx.user_id,
                account_id=tx.account_id,
                category_id=tx.category_id,
                date=tx.date,
                amount=tx.amount,
                type=tx.type.value,
                original_description=tx.original_description,
                clean_description=tx.clean_description,
                merchant_name=tx.merchant_name,
                notes=tx.notes,
                is_pending=tx.is_pending,
                is_transfer=tx.is_transfer,
                matched_transfer_id=tx.matched_transfer_id,
                is_ignored=tx.is_ignored,
                source=tx.source.value,
                external_id=tx.external_id,
                fingerprint=tx.fingerprint,
            )
            session.add(model)

            account = await session.get(AccountModel, tx.account_id)
            if _tracks_ledger_balance(account) and not tx.is_pending:
                account.current_balance += tx.amount

            await session.commit()
            await session.refresh(model)
            return transaction_to_domain(model)

    async def update_transaction(self, tx: Transaction) -> Transaction:
        async with self._session_factory() as session:
            model = await session.get(TransactionModel, tx.id)
            if not model:
                raise ValueError(f"Transaction {tx.id} not found")

            # Balance delta adjustment if amount or pending status changed
            account = await session.get(AccountModel, model.account_id)
            if _tracks_ledger_balance(account):
                old_effect = Decimal("0.0000") if model.is_pending else model.amount
                new_effect = Decimal("0.0000") if tx.is_pending else tx.amount
                account.current_balance += new_effect - old_effect

            model.category_id = tx.category_id
            model.date = tx.date
            model.amount = tx.amount
            model.type = tx.type.value
            model.original_description = tx.original_description
            model.clean_description = tx.clean_description
            model.merchant_name = tx.merchant_name
            model.notes = tx.notes
            model.is_pending = tx.is_pending
            model.is_transfer = tx.is_transfer
            model.matched_transfer_id = tx.matched_transfer_id
            model.is_ignored = tx.is_ignored
            # Sync re-keys a row when the bank re-sends it under a new id
            model.external_id = tx.external_id
            model.fingerprint = tx.fingerprint

            await session.commit()
            await session.refresh(model)
            return transaction_to_domain(model)

    async def delete_transaction(self, tx_id: UUID) -> bool:
        async with self._session_factory() as session:
            model = await session.get(TransactionModel, tx_id)
            if not model:
                return False

            # Reverse balance impact on account
            account = await session.get(AccountModel, model.account_id)
            if _tracks_ledger_balance(account) and not model.is_pending:
                account.current_balance -= model.amount

            await session.delete(model)
            await session.commit()
            return True

    async def delete_by_account(self, account_id: UUID) -> int:
        async with self._session_factory() as session:
            stmt = delete(TransactionModel).where(TransactionModel.account_id == account_id)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount or 0
