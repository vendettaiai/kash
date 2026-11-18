"""Database Dashboard metrics and summary repository."""

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import (
    AccountModel,
    BillModel,
    BillOccurrenceModel,
    CategoryGroupModel,
    CategoryModel,
    TransactionModel,
)
from kash.db.repositories.budget_repo import effective_budgets
from kash.db.repositories.mappers import account_to_domain, transaction_to_domain
from kash.db.repositories.transaction_repo import pending_totals
from kash.domain.models import (
    AccountType,
    BillOccurrenceStatus,
    BillStatus,
    DashboardSummary,
    Transaction,
)
from kash.services.ledger import (
    balances_after_pending,
    calculate_cashflow,
    cash_flow_transactions,
    quantize_currency,
    spending_by_category,
)


def _pct_change(current: Decimal, previous: Decimal) -> Decimal | None:
    """Percent change from previous to current; None when there is no baseline."""
    if previous <= 0:
        return None
    return quantize_currency((current - previous) / previous * Decimal("100"), Decimal("0.1"))


class SqlDashboardRepository:
    """SQL-backed implementation of DashboardRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def get_summary(
        self,
        user_id: UUID,
        start: date | None = None,
        end: date | None = None,
        compare_start: date | None = None,
        compare_end: date | None = None,
    ) -> DashboardSummary:
        """Balances, cash flow for ``start``..``end`` (vs. the compare range), and budget.

        Defaults to month-to-date compared with the same days last month.
        Budget figures always describe the current calendar month.
        """
        today = date.today()
        current_month = today.strftime("%Y-%m")
        month_start = today.replace(day=1)
        start = start or month_start
        end = end or today
        if compare_start is None or compare_end is None:
            prev_month_end = month_start - timedelta(days=1)
            compare_start = prev_month_end.replace(day=1)
            compare_end = min(compare_start + timedelta(days=today.day - 1), prev_month_end)

        async with self._session_factory() as session:
            # 1. Accounts & Balance Breakdowns
            acc_stmt = select(AccountModel).where(AccountModel.user_id == user_id)
            acc_res = await session.execute(acc_stmt)
            accounts = [account_to_domain(a) for a in acc_res.scalars().all()]

            banking_accounts = [
                a
                for a in accounts
                if a.is_active
                and a.account_type in (AccountType.CHECKING, AccountType.SAVINGS, AccountType.CASH)
            ]
            investment_accounts = [
                a for a in accounts if a.is_active and a.account_type == AccountType.INVESTMENT
            ]
            liability_accounts = [
                a
                for a in accounts
                if a.is_active and a.account_type in (AccountType.CREDIT_CARD, AccountType.LOAN)
            ]

            # Balances after pending, as on the Accounts page. Net worth counts every
            # banking account; the dashboard's balance and "cash available" leave out
            # accounts the user hid from it.
            balance = balances_after_pending(accounts, await pending_totals(session, user_id))
            all_banking = sum((balance[a.id] for a in banking_accounts), Decimal("0.0000"))
            banking_accounts = [a for a in banking_accounts if a.show_in_balance]
            total_banking_balance = sum(
                (balance[a.id] for a in banking_accounts), Decimal("0.0000")
            )
            total_investments = sum((balance[a.id] for a in investment_accounts), Decimal("0.0000"))
            total_liabilities = sum(
                (abs(balance[a.id]) for a in liability_accounts), Decimal("0.0000")
            )
            total_net_worth = (all_banking + total_investments) - total_liabilities

            # Available funds: Liquid asset accounts (checking, savings, cash with positive balance)
            available_funds = sum(
                (balance[a.id] for a in banking_accounts if balance[a.id] > 0),
                Decimal("0.0000"),
            )

            # 2. Month-to-date cash flow vs the same span of last month (inactive
            #    accounts left out; investment accounts add only their income)
            active_ids = [a.id for a in accounts if a.is_active]
            transfer_cat_res = await session.execute(
                select(CategoryModel.id).where(
                    CategoryModel.user_id == user_id, CategoryModel.is_transfer.is_(True)
                )
            )
            transfer_cats = set(transfer_cat_res.scalars().all())
            income_cat_res = await session.execute(
                select(CategoryModel.id)
                .join(CategoryGroupModel, CategoryModel.group_id == CategoryGroupModel.id)
                .where(
                    CategoryModel.user_id == user_id,
                    func.lower(func.trim(CategoryGroupModel.name)) == "income",
                )
            )
            income_cats = set(income_cat_res.scalars().all())

            window_start = min(start, compare_start, month_start)
            window_end = max(end, compare_end, today)
            transactions: list[Transaction] = []
            if active_ids:
                tx_stmt = select(TransactionModel).where(
                    TransactionModel.user_id == user_id,
                    TransactionModel.account_id.in_(active_ids),
                    TransactionModel.is_ignored.is_(False),
                    TransactionModel.date >= window_start,
                    TransactionModel.date <= window_end,
                )
                tx_res = await session.execute(tx_stmt)
                transactions = cash_flow_transactions(
                    (transaction_to_domain(t) for t in tx_res.scalars().all()),
                    accounts,
                    income_cats,
                )

            def between(lo: date, hi: date) -> list[Transaction]:
                return [t for t in transactions if lo <= t.date <= hi]

            cashflow = calculate_cashflow(
                between(start, end),
                transfer_category_ids=transfer_cats,
                income_category_ids=income_cats,
            )
            prev_cashflow = calculate_cashflow(
                between(compare_start, compare_end),
                transfer_category_ids=transfer_cats,
                income_category_ids=income_cats,
            )

            # 3. Budget totals for the current month (net of refunds)
            budgets = [
                b
                for b in await effective_budgets(session, user_id, current_month)
                if b.budgeted_amount > 0
            ]
            total_budgeted = sum(
                (
                    b.budgeted_amount + (b.rollover_balance if b.enable_rollover else Decimal("0"))
                    for b in budgets
                ),
                Decimal("0.0000"),
            )
            month_spending = spending_by_category(
                (
                    t
                    for t in transactions
                    if t.date.strftime("%Y-%m") == current_month and not t.is_pending
                ),
                transfer_cats,
                income_cats,
            )
            month_expenses = sum(
                (
                    max(month_spending.get(b.category_id, Decimal("0")), Decimal("0"))
                    for b in budgets
                ),
                Decimal("0.0000"),
            )

            pct_used = (
                quantize_currency(
                    (month_expenses / total_budgeted) * Decimal("100"), Decimal("0.01")
                )
                if total_budgeted > 0
                else Decimal("0.00")
            )

            # 4. Upcoming bills
            bill_stmt = (
                select(BillOccurrenceModel)
                .join(BillModel, BillOccurrenceModel.bill_id == BillModel.id)
                .where(
                    BillModel.user_id == user_id,
                    BillModel.status == BillStatus.ACTIVE.value,
                    BillOccurrenceModel.status == BillOccurrenceStatus.UPCOMING.value,
                )
            )
            bill_res = await session.execute(bill_stmt)
            upcoming_occurrences = bill_res.scalars().all()
            upcoming_bills_count = len(upcoming_occurrences)
            upcoming_bills_total = sum(
                (o.expected_amount for o in upcoming_occurrences), Decimal("0.0000")
            )

            return DashboardSummary(
                total_net_worth=quantize_currency(total_net_worth),
                monthly_net_worth_delta=cashflow.income - cashflow.expenses,
                monthly_net_worth_delta_pct=None,
                monthly_income=cashflow.income,
                monthly_income_delta_pct=_pct_change(cashflow.income, prev_cashflow.income),
                monthly_expenses=cashflow.expenses,
                monthly_expenses_delta_pct=_pct_change(cashflow.expenses, prev_cashflow.expenses),
                monthly_earned=cashflow.earned,
                monthly_other_income=cashflow.other_income,
                monthly_refunds=cashflow.refunds,
                compare_income=prev_cashflow.income,
                compare_expenses=prev_cashflow.expenses,
                available_funds=quantize_currency(available_funds),
                budget_total_budgeted=quantize_currency(total_budgeted),
                budget_total_spent=quantize_currency(month_expenses),
                budget_pct_used=pct_used,
                upcoming_bills_count=upcoming_bills_count,
                upcoming_bills_total=quantize_currency(upcoming_bills_total),
                total_banking_balance=quantize_currency(total_banking_balance),
                total_investments=quantize_currency(total_investments),
                total_liabilities=quantize_currency(total_liabilities),
            )
