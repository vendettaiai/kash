"""Accounts screen and account management."""

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from kash.domain.models import Account, AccountCashFlow, AccountType, Transaction
from kash.services.ledger import (
    as_counted,
    balances_after_pending,
    calculate_cashflow,
    income_category_ids,
)
from kash.services.transfers import mark_debt_payments
from kash.web.deps import (
    OWNER_USER_ID,
    Provider,
    get_templates,
    redirect_to,
    safe_local_path,
    transfer_category_ids,
)

router = APIRouter()

BANKING_TYPES = (AccountType.CHECKING, AccountType.SAVINGS, AccountType.CASH)
LIABILITY_TYPES = (AccountType.CREDIT_CARD, AccountType.LOAN)
MANAGE_PAGE = "/settings?section=accounts"  # adding, editing and pausing accounts


async def monthly_net(
    provider: Provider, accounts: list[Account], today: date | None = None
) -> dict[UUID, Decimal]:
    """Each account's surplus (+) or deficit (-) so far this month: its income minus
    its spending, counted like the dashboard's Surplus card (posted transactions only;
    transfers between your accounts, ignored transactions and accounts set to "Don't
    count" left out). Accounts that don't count are missing from the result."""
    today = today or date.today()
    groups = await provider.categories.list_groups(OWNER_USER_ID)
    categories = await provider.categories.list_categories(OWNER_USER_ID, include_hidden=True)
    income_cats = income_category_ids(groups, categories)
    transfer_cats = transfer_category_ids(categories)
    by_id = {a.id: a for a in accounts}
    per_account: dict[UUID, list[Transaction]] = {
        a.id: [] for a in accounts if a.cash_flow != AccountCashFlow.NONE
    }
    for tx in await provider.transactions.list_transactions(
        OWNER_USER_ID, start_date=today.replace(day=1), end_date=today, limit=100_000
    ):
        if tx.account_id in per_account and as_counted(tx, by_id[tx.account_id], income_cats):
            per_account[tx.account_id].append(tx)
    return {
        account_id: calculate_cashflow(
            txs, transfer_category_ids=transfer_cats, income_category_ids=income_cats
        ).surplus
        for account_id, txs in per_account.items()
    }


def _parse_account_type(value: str) -> AccountType | None:
    try:
        return AccountType(value.strip().lower())
    except ValueError:
        return None


@router.get("/accounts", response_class=HTMLResponse)
async def accounts(request: Request, provider: Provider) -> HTMLResponse:
    """Net worth and balances. Bank connections and account settings are in Settings."""
    user_id = OWNER_USER_ID
    account_list = await provider.accounts.list_accounts(user_id, include_inactive=True)
    conns = await provider.bank_connections.list_connections(user_id)
    # Shown balances include pending transactions; the bank's own (posted) balance
    # is shown next to them
    pending = await provider.transactions.pending_totals(user_id)
    balances = balances_after_pending(account_list, pending)

    banking = [a for a in account_list if a.account_type in BANKING_TYPES]
    investments = [a for a in account_list if a.account_type == AccountType.INVESTMENT]
    liabilities = [a for a in account_list if a.account_type in LIABILITY_TYPES]

    def total(items: list[Account], absolute: bool = False) -> Decimal:
        return sum(
            (abs(balances[a.id]) if absolute else balances[a.id] for a in items if a.is_active),
            Decimal("0.00"),
        )

    total_banking = total(banking)
    total_investments = total(investments)
    total_liabilities = total(liabilities, absolute=True)
    shown_banking = [a for a in banking if a.is_active]

    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/accounts.html",
        context={
            "active_page": "accounts",
            "banking_accounts": shown_banking,
            "investment_accounts": [a for a in investments if a.is_active],
            "liability_accounts": [a for a in liabilities if a.is_active],
            "balances": balances,
            "pending": pending,
            "month_net": await monthly_net(provider, shown_banking),
            "month_label": date.today().strftime("%B"),
            "total_net_worth": total_banking + total_investments - total_liabilities,
            "total_banking": total_banking,
            "total_investments": total_investments,
            "total_liabilities": total_liabilities,
            # bank connections waiting for an answer or with a problem (fixed in Settings)
            "connections_attention": sum(
                1 for c in conns if c.pending_accounts or c.status in ("reauth_required", "error")
            ),
            "paused_count": sum(1 for a in account_list if not a.is_active),
        },
    )


@router.post("/accounts/new")
async def create_account(
    provider: Provider,
    name: Annotated[str, Form()],
    account_type: Annotated[str, Form()],
    starting_balance: Annotated[Decimal, Form()] = Decimal("0.00"),
    redirect_url: Annotated[str, Form()] = MANAGE_PAGE,
) -> RedirectResponse:
    """Create a manual (not bank-linked) account."""
    target = safe_local_path(redirect_url, MANAGE_PAGE)
    parsed_type = _parse_account_type(account_type)
    if parsed_type is None or not name.strip():
        return redirect_to(target, error="Please enter a name and a valid account type.")
    await provider.accounts.create_account(
        Account(
            id=uuid4(),
            user_id=OWNER_USER_ID,
            name=name.strip(),
            account_type=parsed_type,
            current_balance=starting_balance,
        )
    )
    return redirect_to(target)


@router.post("/accounts/{account_id}/toggle-sync")
async def toggle_account_sync(
    account_id: UUID,
    provider: Provider,
    redirect_url: Annotated[str, Form()] = MANAGE_PAGE,
) -> RedirectResponse:
    """Include or exclude an account.

    Excluded accounts are skipped by bank sync and hidden from totals and lists.
    Their transaction history is kept, so re-enabling an account restores it.
    """
    acc = await provider.accounts.get_account(account_id)
    if acc:
        acc.is_active = not acc.is_active
        await provider.accounts.update_account(acc)
    return redirect_to(safe_local_path(redirect_url, MANAGE_PAGE))


@router.post("/accounts/{account_id}/update")
async def update_account(
    account_id: UUID,
    provider: Provider,
    name: Annotated[str, Form()],
    account_type: Annotated[str, Form()],
    balance_option: Annotated[str, Form()] = "",
    show_in_balance: Annotated[str, Form()] = "",
    cash_flow: Annotated[str, Form()] = "",
    redirect_url: Annotated[str, Form()] = MANAGE_PAGE,
) -> RedirectResponse:
    """Rename an account, change its type, hide it from the dashboard balance, or set
    what it adds to income and spending."""
    target = safe_local_path(redirect_url, MANAGE_PAGE)
    acc = await provider.accounts.get_account(account_id)
    parsed_type = _parse_account_type(account_type)
    if acc and parsed_type is not None:
        became_debt = acc.account_type not in LIABILITY_TYPES and parsed_type in LIABILITY_TYPES
        acc.name = name.strip() or acc.name
        acc.account_type = parsed_type
        if balance_option:  # the checkbox was on the form (unticked sends nothing)
            acc.show_in_balance = bool(show_in_balance)
        if cash_flow in {mode.value for mode in AccountCashFlow}:
            acc.cash_flow = AccountCashFlow(cash_flow)
        await provider.accounts.update_account(acc)
        if became_debt:  # its payments were counted as income until now
            marked = await mark_debt_payments(provider, OWNER_USER_ID, acc.id)
            if marked:
                noun = "payment" if marked == 1 else "payments"
                return redirect_to(
                    target,
                    notice=f"{marked} {noun} into this account no longer count as income.",
                )
    return redirect_to(target)


@router.post("/accounts/{account_id}/delete")
async def delete_account(account_id: UUID, provider: Provider) -> RedirectResponse:
    """Delete an account and its transactions."""
    await provider.accounts.delete_account(account_id)
    return redirect_to(MANAGE_PAGE)
