"""Top-level HTML router: combines the per-feature route modules."""

from fastapi import APIRouter

from kash.web.deps import get_bank_providers, get_data_provider
from kash.web.routes import (
    accounts,
    ai,
    auth,
    bills,
    budget,
    categories,
    connections,
    dashboard,
    goals,
    merchant_icons,
    notifications,
    pages,
    people,
    pwa,
    reports,
    rules,
    settings,
    transactions,
)

__all__ = ["get_bank_providers", "get_data_provider", "router"]

router = APIRouter()
for module in (
    auth,
    people,
    dashboard,
    accounts,
    transactions,
    bills,
    budget,
    goals,
    categories,
    rules,
    reports,
    connections,
    settings,
    ai,
    notifications,
    merchant_icons,
    pwa,
    pages,
):
    router.include_router(module.router)
