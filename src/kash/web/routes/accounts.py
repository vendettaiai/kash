"""Accounts screen and account management."""

from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from kash.domain.models import Account, AccountCashFlow, AccountType
from kash.services.ledger import balances_after_pending
from kash.services.transfers import mark_debt_payments
from kash.web.deps import OWNER_USER_ID, Provider, get_templates, redirect_to

router = APIRouter()

BANKING_TYPES = (AccountType.CHECKING, AccountType.SAVINGS, AccountType.CASH)
LIABILITY_TYPES = (AccountType.CREDIT_CARD, AccountType.LOAN)


def _parse_account_type(value: str) -> AccountType | None:
    try:
        return AccountType(value.strip().lower())
    except ValueError:
        return None


@router.get("/accounts", response_class=HTMLResponse)
async def accounts(request: Request, provider: Provider) -> HTMLResponse:
    """Net worth, account groups, and bank connections."""
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
    # for the "replacing which account?" choice on a new bank account (excluded ones too)
    by_name = sorted(account_list, key=lambda a: a.name.lower())

    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/accounts.html",
        context={
            "active_page": "accounts",
            "accounts": account_list,
            "banking_accounts": banking,
            "investment_accounts": investments,
            "liability_accounts": liabilities,
            "balances": balances,
            "pending": pending,
            "total_net_worth": total_banking + total_investments - total_liabilities,
            "total_banking": total_banking,
            "total_investments": total_investments,
            "total_liabilities": total_liabilities,
            "connections": conns,
            "all_accounts": by_name,
            "accounts_by_id": {a.id: a for a in by_name},
        },
    )


@router.post("/accounts/new")
async def create_account(
    provider: Provider,
    name: Annotated[str, Form()],
    account_type: Annotated[str, Form()],
    starting_balance: Annotated[Decimal, Form()] = Decimal("0.00"),
) -> RedirectResponse:
    """Create a manual (not bank-linked) account."""
    parsed_type = _parse_account_type(account_type)
    if parsed_type is None or not name.strip():
        return redirect_to("/accounts", error="Please enter a name and a valid account type.")
    await provider.accounts.create_account(
        Account(
            id=uuid4(),
            user_id=OWNER_USER_ID,
            name=name.strip(),
            account_type=parsed_type,
            current_balance=starting_balance,
        )
    )
    return redirect_to("/accounts")


@router.post("/accounts/{account_id}/toggle-sync")
async def toggle_account_sync(account_id: UUID, provider: Provider) -> RedirectResponse:
    """Include or exclude an account.

    Excluded accounts are skipped by bank sync and hidden from totals and lists.
    Their transaction history is kept, so re-enabling an account restores it.
    """
    acc = await provider.accounts.get_account(account_id)
    if acc:
        acc.is_active = not acc.is_active
        await provider.accounts.update_account(acc)
    return redirect_to("/accounts")


@router.post("/accounts/{account_id}/update")
async def update_account(
    account_id: UUID,
    provider: Provider,
    name: Annotated[str, Form()],
    account_type: Annotated[str, Form()],
    balance_option: Annotated[str, Form()] = "",
    show_in_balance: Annotated[str, Form()] = "",
    cash_flow: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Rename an account, change its type, hide it from the dashboard balance, or set
    what it adds to income and spending."""
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
                    "/accounts",
                    notice=f"{marked} {noun} into this account no longer count as income.",
                )
    return redirect_to("/accounts")


@router.post("/accounts/{account_id}/delete")
async def delete_account(account_id: UUID, provider: Provider) -> RedirectResponse:
    """Delete an account and its transactions."""
    await provider.accounts.delete_account(account_id)
    return redirect_to("/accounts")
