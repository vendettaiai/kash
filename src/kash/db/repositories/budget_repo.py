"""Database Budget and Progress tracking repository.

Budgets carry forward: a month with no budgets of its own uses the most recent
earlier month's budgets. Changing a budget in such a month first copies the
carried-forward budgets into it, so later months keep inheriting the full set.
A budget of 0 means "no budget" for that category from that month on.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import BudgetModel, CategoryModel, TransactionModel
from kash.db.repositories.mappers import (
    budget_to_domain,
    category_to_domain,
    transaction_to_domain,
)
from kash.domain.models import Budget, CategoryBudgetProgress
from kash.services.ledger import calculate_budget_progress


async def _source_period(session: AsyncSession, user_id: UUID, period: str) -> str | None:
    """The month whose budgets apply to ``period`` (itself, or the latest earlier one)."""
    res = await session.execute(
        select(func.max(BudgetModel.period)).where(
            BudgetModel.user_id == user_id, BudgetModel.period <= period
        )
    )
    return res.scalar_one_or_none()


async def effective_budgets(session: AsyncSession, user_id: UUID, period: str) -> list[Budget]:
    """Budgets in effect for ``period`` (``YYYY-MM``), including carried-forward ones."""
    source = await _source_period(session, user_id, period)
    if source is None:
        return []
    res = await session.execute(
        select(BudgetModel).where(BudgetModel.user_id == user_id, BudgetModel.period == source)
    )
    budgets = [budget_to_domain(row) for row in res.scalars().all()]
    if source != period:
        budgets = [
            b.model_copy(update={"period": period, "rollover_balance": Decimal("0")})
            for b in budgets
        ]
    return budgets


class SqlBudgetRepository:
    """SQL-backed implementation of BudgetRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def get_budget(self, user_id: UUID, period: str) -> list[Budget]:
        async with self._session_factory() as session:
            return await effective_budgets(session, user_id, period)

    async def set_category_budget(
        self,
        user_id: UUID,
        category_id: UUID,
        period: str,
        amount: Decimal,
        rollover: bool = False,
    ) -> Budget:
        async with self._session_factory() as session:
            source = await _source_period(session, user_id, period)
            if source is not None and source != period:
                # Materialize the carried-forward budgets for this month first.
                inherited = await session.execute(
                    select(BudgetModel).where(
                        BudgetModel.user_id == user_id, BudgetModel.period == source
                    )
                )
                for row in inherited.scalars().all():
                    session.add(
                        BudgetModel(
                            id=uuid4(),
                            user_id=user_id,
                            category_id=row.category_id,
                            period=period,
                            budgeted_amount=row.budgeted_amount,
                            enable_rollover=row.enable_rollover,
                            rollover_balance=Decimal("0.0000"),
                        )
                    )
                await session.flush()

            stmt = select(BudgetModel).where(
                BudgetModel.user_id == user_id,
                BudgetModel.category_id == category_id,
                BudgetModel.period == period,
            )
            model = (await session.execute(stmt)).scalar_one_or_none()

            if model:
                model.budgeted_amount = amount
                model.enable_rollover = rollover
            else:
                model = BudgetModel(
                    id=uuid4(),
                    user_id=user_id,
                    category_id=category_id,
                    period=period,
                    budgeted_amount=amount,
                    enable_rollover=rollover,
                    rollover_balance=Decimal("0.0000"),
                )
                session.add(model)

            await session.commit()
            await session.refresh(model)
            return budget_to_domain(model)

    async def get_budget_progress(self, user_id: UUID, period: str) -> list[CategoryBudgetProgress]:
        async with self._session_factory() as session:
            budgets = [
                b
                for b in await effective_budgets(session, user_id, period)
                if b.budgeted_amount > 0
            ]
            if not budgets:
                return []

            cat_ids = [b.category_id for b in budgets]
            c_res = await session.execute(
                select(CategoryModel).where(CategoryModel.id.in_(cat_ids))
            )
            categories = [category_to_domain(c) for c in c_res.scalars().all()]

            year, month = (int(part) for part in period.split("-", 1))
            first_day = date(year, month, 1)
            next_first = date(year + (month == 12), month % 12 + 1, 1)
            t_stmt = select(TransactionModel).where(
                TransactionModel.user_id == user_id,
                TransactionModel.category_id.in_(cat_ids),
                TransactionModel.is_transfer.is_(False),
                TransactionModel.is_ignored.is_(False),
                TransactionModel.date >= first_day,
                TransactionModel.date < next_first,
            )
            t_res = await session.execute(t_stmt)
            transactions = [transaction_to_domain(t) for t in t_res.scalars().all()]

            return calculate_budget_progress(
                budgets=budgets,
                transactions=transactions,
                categories=categories,
                period=period,
            )
