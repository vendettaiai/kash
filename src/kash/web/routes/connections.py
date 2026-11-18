"""Bank connections (SimpleFIN) and sync, managed on the Accounts page."""

import logging
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Form
from fastapi.responses import RedirectResponse

from kash.domain.models import BankConnection, BankConnectionStatus, BankSyncResult
from kash.services.ai.categorize import run_after_sync
from kash.services.bank import BankProviderError, BankSyncService
from kash.services.bills.service import after_import_hook
from kash.services.reports import record_snapshots
from kash.services.security import encrypt_credentials
from kash.web.deps import (
    OWNER_USER_ID,
    BankProviders,
    Provider,
    redirect_to,
    safe_local_path,
)

logger = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_PROVIDER = "simplefin"
ACCOUNTS_PAGE = "/accounts"  # where connections are managed


@router.get("/connections")
async def connections_page() -> RedirectResponse:
    """Bank connections are managed on Accounts; old links and bookmarks land there."""
    return redirect_to(ACCOUNTS_PAGE)


@router.post("/connections/create")
async def create_connection(
    provider: Provider,
    bank_providers: BankProviders,
    background: BackgroundTasks,
    token: Annotated[str, Form()] = "",
    provider_type: Annotated[str, Form()] = DEFAULT_PROVIDER,
    redirect_url: Annotated[str, Form()] = ACCOUNTS_PAGE,
) -> RedirectResponse:
    """Claim a setup token, store the encrypted credentials, and run the first sync."""
    target = safe_local_path(redirect_url, ACCOUNTS_PAGE)
    token = token.strip()
    if not token:
        return redirect_to(target, error="Setup token cannot be empty.")

    provider_name = provider_type if provider_type in bank_providers else DEFAULT_PROVIDER
    bank_provider = bank_providers.get(provider_name)
    if bank_provider is None:
        return redirect_to(target, error=f"Bank provider '{provider_name}' is not available.")

    try:
        institution_name, credentials = await bank_provider.exchange_token(token)
    except BankProviderError as exc:
        return redirect_to(target, error=str(exc))
    except Exception:
        logger.exception("Token exchange failed")
        return redirect_to(target, error="Connection failed. Check the setup token and try again.")

    saved = await provider.bank_connections.create_connection(
        BankConnection(
            id=uuid4(),
            user_id=OWNER_USER_ID,
            provider_name=provider_name,
            institution_name=institution_name,
            encrypted_credentials=encrypt_credentials(credentials),
            status=BankConnectionStatus.ACTIVE,
        )
    )
    result = await BankSyncService(
        provider, providers=bank_providers, on_transactions_imported=after_import_hook(provider)
    ).sync_connection(saved.id, OWNER_USER_ID, trigger="initial")
    await record_snapshots(provider, OWNER_USER_ID)  # balances just came from the bank
    background.add_task(run_after_sync, provider, OWNER_USER_ID)  # after the redirect
    if result.errors:
        return redirect_to(target, error="; ".join(result.errors))
    return redirect_to(target, synced="1")


@router.post("/connections/{connection_id}/sync")
async def sync_connection(
    connection_id: UUID,
    provider: Provider,
    bank_providers: BankProviders,
    background: BackgroundTasks,
    redirect_url: Annotated[str, Form()] = ACCOUNTS_PAGE,
) -> RedirectResponse:
    """Sync one connection now."""
    target = safe_local_path(redirect_url, ACCOUNTS_PAGE)
    try:
        result = await BankSyncService(
            provider, providers=bank_providers, on_transactions_imported=after_import_hook(provider)
        ).sync_connection(connection_id, OWNER_USER_ID, trigger="manual")
    except ValueError as exc:
        return redirect_to(target, error=str(exc))
    await record_snapshots(provider, OWNER_USER_ID)  # balances just came from the bank
    background.add_task(run_after_sync, provider, OWNER_USER_ID)  # after the redirect
    if result.errors:
        return redirect_to(target, error="; ".join(result.errors))
    return redirect_to(target, synced="1")


@router.post("/connections/{connection_id}/accounts/link")
async def link_held_account(
    connection_id: UUID,
    provider: Provider,
    bank_providers: BankProviders,
    background: BackgroundTasks,
    external_id: Annotated[str, Form()],
    account_id: Annotated[UUID, Form()],
) -> RedirectResponse:
    """A new account replaces one Kash already has (e.g. a new card number): that
    account continues with it."""
    service = BankSyncService(
        provider, providers=bank_providers, on_transactions_imported=after_import_hook(provider)
    )
    try:
        result = await service.link_pending_account(
            connection_id, OWNER_USER_ID, external_id, account_id
        )
    except ValueError as exc:
        return redirect_to(ACCOUNTS_PAGE, error=str(exc))
    return await _after_answer(provider, background, result)


@router.post("/connections/{connection_id}/accounts/add")
async def add_held_account(
    connection_id: UUID,
    provider: Provider,
    bank_providers: BankProviders,
    background: BackgroundTasks,
    external_id: Annotated[str, Form()],
) -> RedirectResponse:
    """A held account is a new one: add it."""
    service = BankSyncService(
        provider, providers=bank_providers, on_transactions_imported=after_import_hook(provider)
    )
    try:
        result = await service.add_pending_account(connection_id, OWNER_USER_ID, external_id)
    except ValueError as exc:
        return redirect_to(ACCOUNTS_PAGE, error=str(exc))
    return await _after_answer(provider, background, result)


async def _after_answer(
    provider: Provider, background: BackgroundTasks, result: BankSyncResult
) -> RedirectResponse:
    await record_snapshots(provider, OWNER_USER_ID)
    background.add_task(run_after_sync, provider, OWNER_USER_ID)
    if result.errors:
        return redirect_to(
            ACCOUNTS_PAGE,
            error="Saved, but the sync that fetches its history failed: "
            + "; ".join(result.errors),
        )
    return redirect_to(ACCOUNTS_PAGE, synced="1")


@router.post("/connections/{connection_id}/delete")
async def delete_connection(connection_id: UUID, provider: Provider) -> RedirectResponse:
    """Remove a connection. Its accounts and transaction history are kept as manual accounts."""
    await provider.bank_connections.delete_connection(connection_id)
    return redirect_to(ACCOUNTS_PAGE)
