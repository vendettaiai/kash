"""Database Account repository."""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import AccountModel, BalanceSnapshotModel
from kash.db.repositories.mappers import account_to_domain
from kash.domain.models import Account, BalanceSnapshot


class SqlAccountRepository:
    """SQL-backed implementation of AccountRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def list_accounts(self, user_id: UUID, include_inactive: bool = False) -> list[Account]:
        async with self._session_factory() as session:
            stmt = select(AccountModel).where(AccountModel.user_id == user_id)
            if not include_inactive:
                stmt = stmt.where(AccountModel.is_active.is_(True))
            stmt = stmt.order_by(AccountModel.account_type, AccountModel.name)
            result = await session.execute(stmt)
            return [account_to_domain(row) for row in result.scalars().all()]

    async def get_account(self, account_id: UUID) -> Account | None:
        async with self._session_factory() as session:
            stmt = select(AccountModel).where(AccountModel.id == account_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return account_to_domain(row) if row else None

    async def get_by_external_id(self, connection_id: UUID, external_id: str) -> Account | None:
        async with self._session_factory() as session:
            stmt = select(AccountModel).where(
                AccountModel.connection_id == connection_id,
                AccountModel.external_id == external_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return account_to_domain(row) if row else None

    async def create_account(self, account: Account) -> Account:
        async with self._session_factory() as session:
            model = AccountModel(
                id=account.id,
                user_id=account.user_id,
                connection_id=account.connection_id,
                name=account.name,
                account_type=account.account_type.value,
                currency=account.currency,
                current_balance=account.current_balance,
                available_balance=account.available_balance,
                mask=account.mask,
                is_active=account.is_active,
                include_in_net_worth=account.include_in_net_worth,
                show_in_balance=account.show_in_balance,
                cash_flow=account.cash_flow.value,
                external_id=account.external_id,
                last_reconciled_at=account.last_reconciled_at,
                last_reconciled_balance=account.last_reconciled_balance,
                balance_as_of=account.balance_as_of,
                provider_account_name=account.provider_account_name,
                institution_id=account.institution_id,
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)
            return account_to_domain(model)

    async def update_account(self, account: Account) -> Account:
        async with self._session_factory() as session:
            model = await session.get(AccountModel, account.id)
            if not model:
                raise ValueError(f"Account {account.id} not found")

            model.name = account.name
            model.account_type = account.account_type.value
            model.currency = account.currency
            model.current_balance = account.current_balance
            model.available_balance = account.available_balance
            model.mask = account.mask
            model.is_active = account.is_active
            model.include_in_net_worth = account.include_in_net_worth
            model.show_in_balance = account.show_in_balance
            model.cash_flow = account.cash_flow.value
            model.last_reconciled_at = account.last_reconciled_at
            model.last_reconciled_balance = account.last_reconciled_balance
            model.balance_as_of = account.balance_as_of
            model.provider_account_name = account.provider_account_name
            model.institution_id = account.institution_id

            await session.commit()
            await session.refresh(model)
            return account_to_domain(model)

    async def relink_account(
        self, account_id: UUID, connection_id: UUID, external_id: str
    ) -> Account:
        """Point an existing account at a new provider id (its history stays). The
        ``uq_account_external`` constraint refuses an id another account already has."""
        async with self._session_factory() as session:
            model = await session.get(AccountModel, account_id)
            if not model:
                raise ValueError(f"Account {account_id} not found")
            model.connection_id = connection_id
            model.external_id = external_id
            await session.commit()
            await session.refresh(model)
            return account_to_domain(model)

    async def delete_account(self, account_id: UUID) -> bool:
        async with self._session_factory() as session:
            stmt = delete(AccountModel).where(AccountModel.id == account_id)
            result = await session.execute(stmt)
            await session.commit()
            return bool(result.rowcount and result.rowcount > 0)

    async def list_balance_snapshots(self, user_id: UUID) -> list[BalanceSnapshot]:
        """Every saved daily balance of the user's accounts, oldest first."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(BalanceSnapshotModel)
                .join(AccountModel, AccountModel.id == BalanceSnapshotModel.account_id)
                .where(AccountModel.user_id == user_id)
                .order_by(BalanceSnapshotModel.date.asc())
            )
            return [
                BalanceSnapshot(account_id=m.account_id, date=m.date, balance=m.balance)
                for m in result.scalars().all()
            ]

    async def save_balance_snapshot(self, snapshot: BalanceSnapshot) -> None:
        """Insert or replace one account's balance for one day."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(BalanceSnapshotModel).where(
                    BalanceSnapshotModel.account_id == snapshot.account_id,
                    BalanceSnapshotModel.date == snapshot.date,
                )
            )
            model = result.scalar_one_or_none()
            if model is None:
                session.add(
                    BalanceSnapshotModel(
                        account_id=snapshot.account_id, date=snapshot.date, balance=snapshot.balance
                    )
                )
            elif model.balance != snapshot.balance:
                model.balance = snapshot.balance
            else:
                return
            await session.commit()
