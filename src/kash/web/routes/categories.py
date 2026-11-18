"""Category and category-group management."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from kash.domain.models import Category, CategoryGroup
from kash.web.deps import OWNER_USER_ID, Provider, get_templates, redirect_to

router = APIRouter()

DEFAULT_COLOR = "#64748b"


@router.get("/categories", response_class=HTMLResponse)
async def categories(request: Request, provider: Provider) -> HTMLResponse:
    """All category groups and categories (including hidden ones)."""
    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/categories.html",
        context={
            "active_page": "categories",
            "groups": await provider.categories.list_groups(OWNER_USER_ID),
            "categories": await provider.categories.list_categories(
                OWNER_USER_ID, include_hidden=True
            ),
        },
    )


@router.post("/categories/new")
async def create_category(
    provider: Provider,
    name: Annotated[str, Form()],
    group_id: Annotated[UUID, Form()],
    icon: Annotated[str, Form()] = "tag",
    color: Annotated[str, Form()] = DEFAULT_COLOR,
) -> RedirectResponse:
    """Create a category inside a group."""
    if name.strip():
        await provider.categories.create_category(
            Category(
                id=uuid4(),
                user_id=OWNER_USER_ID,
                group_id=group_id,
                name=name.strip(),
                icon=icon.strip() or "tag",
                color=color.strip() or DEFAULT_COLOR,
            )
        )
    return redirect_to("/categories")


@router.post("/categories/groups/new")
async def create_category_group(
    provider: Provider,
    name: Annotated[str, Form()],
    icon: Annotated[str, Form()] = "folder",
    color: Annotated[str, Form()] = DEFAULT_COLOR,
) -> RedirectResponse:
    """Create a category group at the end of the list."""
    if name.strip():
        groups = await provider.categories.list_groups(OWNER_USER_ID)
        await provider.categories.create_group(
            CategoryGroup(
                id=uuid4(),
                user_id=OWNER_USER_ID,
                name=name.strip(),
                icon=icon.strip() or "folder",
                color=color.strip() or DEFAULT_COLOR,
                sort_order=max((g.sort_order for g in groups), default=0) + 1,
            )
        )
    return redirect_to("/categories")


@router.post("/categories/{cat_id}/update")
async def update_category(request: Request, cat_id: UUID, provider: Provider) -> RedirectResponse:
    """Edit a category's name, icon, color, group, and flags."""
    cat = await provider.categories.get_category(cat_id)
    if not cat:
        return redirect_to("/categories")

    form = await request.form()
    cat.name = str(form.get("name") or cat.name).strip()
    cat.icon = str(form.get("icon") or cat.icon).strip()
    cat.color = str(form.get("color") or cat.color).strip()
    if form.get("group_id"):
        try:
            cat.group_id = UUID(str(form.get("group_id")))
        except ValueError:
            pass
    cat.is_transfer = bool(form.get("is_transfer"))
    cat.is_hidden = bool(form.get("is_hidden"))
    await provider.categories.update_category(cat)
    return redirect_to("/categories")


@router.post("/categories/{cat_id}/hide")
async def toggle_hide_category(cat_id: UUID, provider: Provider) -> RedirectResponse:
    """Toggle whether a category is hidden."""
    cat = await provider.categories.get_category(cat_id)
    if cat:
        cat.is_hidden = not cat.is_hidden
        await provider.categories.update_category(cat)
    return redirect_to("/categories")


@router.post("/categories/{cat_id}/delete")
async def delete_category(cat_id: UUID, provider: Provider) -> RedirectResponse:
    """Delete a category; its transactions become uncategorized."""
    await provider.categories.delete_category(cat_id)
    return redirect_to("/categories")


@router.post("/categories/groups/{group_id}/update")
async def update_category_group(
    group_id: UUID,
    provider: Provider,
    name: Annotated[str, Form()],
) -> RedirectResponse:
    """Rename a category group."""
    group = await provider.categories.get_group(group_id)
    if group and name.strip():
        group.name = name.strip()
        await provider.categories.update_group(group)
    return redirect_to("/categories")
