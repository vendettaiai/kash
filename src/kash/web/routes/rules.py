"""Categorization rules and transfer/recurring matching rules."""

import json
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.datastructures import FormData
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from kash.db.session import reset_rules_to_defaults
from kash.domain.models import Category, CategoryGroup, Rule, RuleAction, RuleCondition
from kash.services.rules import portability, preview_rule
from kash.services.transfers import link_transfers
from kash.web.deps import OWNER_USER_ID, Provider, get_templates, redirect_to

router = APIRouter()

MATCHING_RULE_TYPES = ("transfer", "recurring")
PREVIEW_SAMPLE_SIZE = 500


# ---------------------------------------------------------------------------
# Form parsing helpers
# ---------------------------------------------------------------------------


def _form_list(form: FormData, key: str, default: str) -> list[str]:
    values = [str(v) for v in form.getlist(f"{key}[]")]
    return values or [str(form.get(key) or default)]


def _conditions_from_form(form: FormData) -> list[dict[str, str]]:
    rows = zip(
        _form_list(form, "field", "description"),
        _form_list(form, "op", "contains"),
        _form_list(form, "val", ""),
        strict=False,
    )
    return [{"field": f, "op": o, "value": v} for f, o, v in rows if v.strip()]


def _actions_from_form(form: FormData) -> list[dict[str, str]]:
    rows = zip(
        _form_list(form, "action_op", "set_category"),
        _form_list(form, "action_val", ""),
        strict=False,
    )
    return [{"op": o, "value": v} for o, v in rows if v.strip()]


def _int_field(form: FormData, key: str, default: int) -> int:
    try:
        return int(str(form.get(key) or default))
    except ValueError:
        return default


def _joiner(value: object, fallback: str = "and") -> str:
    text = str(value or fallback).lower()
    return text if text in ("and", "or") else fallback


def _action_attr(action: Any, attr: str) -> str:
    value = action.get(attr, "") if isinstance(action, dict) else getattr(action, attr, "")
    return str(value or "")


def _condition_value(condition: Any) -> str:
    value = condition.get("value", "") if isinstance(condition, dict) else condition.value
    return str(value or "")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@router.get("/rules", response_class=HTMLResponse)
async def rules(
    request: Request,
    provider: Provider,
    cat_id: str | None = None,
    status: str | None = None,
    action: str | None = None,
    sort: str = "priority",
    q: str | None = None,
) -> HTMLResponse:
    """Rule lists with search, filters, and sorting for categorization rules."""
    user_id = OWNER_USER_ID
    all_rules = await provider.rules.list_rules(user_id)
    all_cats = await provider.categories.list_categories(user_id)
    # Names for the rule list, hidden categories too (pickers only offer visible ones)
    category_names = {
        str(c.id): c.name
        for c in await provider.categories.list_categories(user_id, include_hidden=True)
    }
    all_accounts = await provider.accounts.list_accounts(user_id)

    def by_type(rule_type: str) -> list[Rule]:
        return sorted(
            (r for r in all_rules if r.rule_type == rule_type),
            key=lambda r: (r.priority, str(r.id)),
        )

    cat_rules = by_type("categorization")

    if q and q.strip():
        needle = q.strip().lower()
        cat_rules = [
            r
            for r in cat_rules
            if needle in r.name.lower()
            or any(needle in _condition_value(c).lower() for c in r.conditions)
        ]
    if status == "active":
        cat_rules = [r for r in cat_rules if r.is_active]
    elif status == "inactive":
        cat_rules = [r for r in cat_rules if not r.is_active]
    if cat_id and cat_id != "all":
        cat_rules = [
            r for r in cat_rules if any(_action_attr(a, "value") == cat_id for a in r.actions)
        ]
    if action and action != "all":
        cat_rules = [
            r for r in cat_rules if any(_action_attr(a, "op") == action for a in r.actions)
        ]

    if sort == "name":
        cat_rules.sort(key=lambda r: r.name.lower())
    elif sort == "category":
        names = {str(c.id): c.name.lower() for c in all_cats}
        cat_rules.sort(
            key=lambda r: names.get(_action_attr(r.actions[0], "value"), "") if r.actions else ""
        )

    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/rules.html",
        context={
            "active_page": "rules",
            "categorization_rules": cat_rules,
            "transfer_rules": by_type("transfer"),
            "recurring_rules": by_type("recurring"),
            "rules": all_rules,
            "categories": all_cats,
            "category_names": category_names,
            "accounts": all_accounts,
            "current_sort": sort,
            "current_status": status or "all",
            "current_cat_id": cat_id or "all",
            "current_action": action or "all",
            "current_q": q or "",
        },
    )


@router.post("/rules/preview", response_class=HTMLResponse)
async def live_preview_rule(request: Request, provider: Provider) -> HTMLResponse:
    """Dry-run an unsaved rule against recent transactions."""
    user_id = OWNER_USER_ID
    form = await request.form()
    result = preview_rule(
        transactions=await provider.transactions.list_transactions(
            user_id, limit=PREVIEW_SAMPLE_SIZE
        ),
        categories=await provider.categories.list_categories(user_id),
        conditions_op=_joiner(form.get("conditions_op")),
        conditions=_conditions_from_form(form),
        actions=_actions_from_form(form),
        is_active=True,
        apply_to_existing=True,
        overwrite_existing_categories=bool(form.get("overwrite_existing")),
        limit=20,
    )
    return get_templates(request).TemplateResponse(
        request=request, name="components/rule_preview.html", context={"result": result}
    )


# ---------------------------------------------------------------------------
# Categorization rules
# ---------------------------------------------------------------------------


@router.post("/rules/new")
async def create_rule(request: Request, provider: Provider) -> RedirectResponse:
    """Create a categorization rule and optionally re-run all rules."""
    user_id = OWNER_USER_ID
    form = await request.form()
    conditions = _conditions_from_form(form)
    if not conditions:
        return redirect_to("/rules", error="A rule needs at least one condition.")

    await provider.rules.create_rule(
        Rule(
            id=uuid4(),
            user_id=user_id,
            name=str(form.get("name") or "New Rule").strip(),
            priority=_int_field(form, "priority", 10),
            is_active=True,
            rule_type="categorization",
            action_mode="links",
            conditions_op=_joiner(form.get("conditions_op")),  # type: ignore[arg-type]
            conditions=[RuleCondition(**c) for c in conditions],
            actions=[RuleAction(**a) for a in _actions_from_form(form)],
        )
    )
    if form.get("apply_to_existing"):
        await provider.rules.apply_all_rules(user_id)
    return redirect_to("/rules")


@router.post("/rules/{rule_id}/update")
async def update_rule(request: Request, rule_id: UUID, provider: Provider) -> RedirectResponse:
    """Edit a categorization rule."""
    rule = await provider.rules.get_rule(rule_id)
    if not rule:
        return redirect_to("/rules")

    form = await request.form()
    rule.name = str(form.get("name") or rule.name).strip()
    rule.priority = _int_field(form, "priority", rule.priority)
    rule.conditions_op = _joiner(form.get("conditions_op"), rule.conditions_op)  # type: ignore[assignment]
    conditions = _conditions_from_form(form)
    if conditions:
        rule.conditions = [RuleCondition(**c) for c in conditions]
    actions = _actions_from_form(form)
    if actions:
        rule.actions = [RuleAction(**a) for a in actions]

    await provider.rules.update_rule(rule)
    if form.get("apply_to_existing"):
        await provider.rules.apply_all_rules(OWNER_USER_ID)
    return redirect_to("/rules")


# ---------------------------------------------------------------------------
# Transfer / recurring matching rules
# ---------------------------------------------------------------------------


@router.post("/rules/matching/new")
async def create_matching_rule(request: Request, provider: Provider) -> RedirectResponse:
    """Create a transfer or recurring matching rule."""
    user_id = OWNER_USER_ID
    form = await request.form()
    rule_type = str(form.get("rule_type") or "transfer")
    if rule_type not in MATCHING_RULE_TYPES:
        return redirect_to("/rules", error="Unknown matching rule type.")
    is_transfer = rule_type == "transfer"

    existing = await provider.rules.list_rules(user_id, rule_type=rule_type)
    config = {
        "direction": str(form.get("direction") or "either"),
        "currencies": [
            c.strip() for c in str(form.get("currencies") or "").split(",") if c.strip()
        ],
        "different_accounts": bool(form.get("different_accounts")) or is_transfer,
        "description_names_other_account": bool(form.get("desc_names_account")),
        "closest_date_wins": bool(form.get("closest_date_wins")),
        "unique_candidate": bool(form.get("unique_candidate")),
        "amount_match_mode": str(form.get("amount_match_mode") or "exact"),
        "days_early": _int_field(form, "days_early", 2 if is_transfer else 3),
        "days_late": _int_field(form, "days_late", 2 if is_transfer else 5),
        "description_similarity": str(form.get("description_similarity") or "off"),
        "account_types": [str(v) for v in form.getlist("account_types[]")],
    }
    await provider.rules.create_rule(
        Rule(
            id=uuid4(),
            user_id=user_id,
            name=str(form.get("name") or "New Matching Rule").strip(),
            priority=max((r.priority for r in existing), default=0) + 1,
            is_active=True,
            rule_type=rule_type,  # type: ignore[arg-type]
            action_mode="suggests" if form.get("action_mode") == "suggests" else "links",
            config=config,
        )
    )
    return redirect_to("/rules")


@router.post("/rules/{rule_id}/matching/update")
async def update_matching_rule(
    request: Request, rule_id: UUID, provider: Provider
) -> RedirectResponse:
    """Edit a transfer or recurring matching rule."""
    rule = await provider.rules.get_rule(rule_id)
    if not rule:
        return redirect_to("/rules")

    form = await request.form()
    rule.name = str(form.get("name") or rule.name).strip()
    if form.get("action_mode") in ("links", "suggests"):
        rule.action_mode = str(form.get("action_mode"))  # type: ignore[assignment]

    config = dict(rule.config)
    config.update(
        {
            "direction": str(form.get("direction") or config.get("direction", "either")),
            "amount_match_mode": str(
                form.get("amount_match_mode") or config.get("amount_match_mode", "exact")
            ),
            "days_early": _int_field(form, "days_early", int(config.get("days_early", 2))),
            "days_late": _int_field(form, "days_late", int(config.get("days_late", 2))),
            "text_must_contain": str(form.get("text_must_contain") or "").strip(),
            "description_names_other_account": bool(form.get("desc_names_account")),
            "closest_date_wins": bool(form.get("closest_date_wins")),
            "unique_candidate": bool(form.get("unique_candidate")),
        }
    )
    rule.config = config
    await provider.rules.update_rule(rule)
    return redirect_to("/rules")


# ---------------------------------------------------------------------------
# Shared rule actions
# ---------------------------------------------------------------------------


@router.post("/rules/{rule_id}/reorder")
async def reorder_rule(request: Request, rule_id: UUID, provider: Provider) -> RedirectResponse:
    """Swap a rule's priority with its neighbour (direction=up|down)."""
    form = await request.form()
    rule = await provider.rules.get_rule(rule_id)
    if not rule:
        return redirect_to("/rules")

    section = sorted(
        await provider.rules.list_rules(OWNER_USER_ID, rule_type=rule.rule_type),
        key=lambda r: (r.priority, str(r.id)),
    )
    idx = next((i for i, r in enumerate(section) if r.id == rule.id), -1)
    target = idx - 1 if form.get("direction", "up") == "up" else idx + 1
    if idx < 0 or not 0 <= target < len(section):
        return redirect_to("/rules")

    current, other = section[idx], section[target]
    renumber = current.priority == other.priority
    if renumber:
        # Equal priorities would make a swap a no-op, so spread the section out first.
        for position, r in enumerate(section):
            r.priority = position * 10
    current.priority, other.priority = other.priority, current.priority
    for r in section if renumber else (current, other):
        await provider.rules.update_rule(r)
    return redirect_to("/rules")


@router.get("/rules/export")
async def export_rules(provider: Provider) -> Response:
    """Download all rules as JSON (with category names, so they import anywhere)."""
    data = portability.export_rules(
        await provider.rules.list_rules(OWNER_USER_ID),
        await provider.categories.list_categories(OWNER_USER_ID, include_hidden=True),
        await provider.categories.list_groups(OWNER_USER_ID),
    )
    return Response(
        content=json.dumps(data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="kash_rules.json"'},
    )


async def _plan_import(provider: Provider, raw: list[dict[str, Any]]) -> portability.ImportPlan:
    return portability.plan_import(
        raw,
        OWNER_USER_ID,
        await provider.rules.list_rules(OWNER_USER_ID),
        await provider.categories.list_categories(OWNER_USER_ID, include_hidden=True),
        await provider.accounts.list_accounts(OWNER_USER_ID, include_inactive=True),
    )


@router.post("/rules/import/preview", response_class=HTMLResponse, response_model=None)
async def import_preview(
    request: Request,
    provider: Provider,
    file: UploadFile | None = File(None),
    text: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """Read an exported rules file and show what importing it would do (nothing saved)."""
    content: bytes | str = text
    if file is not None and file.filename:
        content = await file.read(portability.MAX_FILE_BYTES + 1)
    try:
        raw = portability.parse_file(content)
    except portability.ImportFileError as exc:
        return redirect_to("/rules", error=str(exc))
    plan = await _plan_import(provider, raw)
    if not plan.rules:
        return redirect_to("/rules", error="None of the rules in that file could be read.")
    existing = await provider.rules.list_rules(OWNER_USER_ID)
    preview = portability.resolve(
        plan, {u.key: u.suggestion for u in plan.unknown if u.suggestion}, existing
    )
    categories = await provider.categories.list_categories(OWNER_USER_ID, include_hidden=True)
    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/rules_import.html",
        context={
            "active_page": "rules",
            "plan": plan,
            "preview": preview,
            "payload": json.dumps(plan.raw),
            "categories": sorted(categories, key=lambda c: c.name.lower()),
            "choice_none": portability.NO_CATEGORY,
            "choice_create": portability.CREATE,
        },
    )


@router.post("/rules/import")
async def import_rules(request: Request, provider: Provider) -> RedirectResponse:
    """Import the previewed rules, with the user's choice for each unknown category."""
    form = await request.form()
    try:
        raw = portability.parse_file(str(form.get("payload") or ""))
    except portability.ImportFileError as exc:
        return redirect_to("/rules", error=str(exc))
    plan = await _plan_import(provider, raw)
    categories = {
        c.id: c
        for c in await provider.categories.list_categories(OWNER_USER_ID, include_hidden=True)
    }
    choices: dict[str, UUID | None] = {}
    created: list[str] = []
    for ref in plan.unknown:
        picked = str(form.get(f"map:{ref.key}") or "")
        if picked == portability.NO_CATEGORY:
            choices[ref.key] = None
        elif picked == portability.CREATE and ref.name:
            category = await _create_category(provider, ref.name, ref.group)
            choices[ref.key] = category.id
            created.append(category.name)
        else:
            try:
                choices[ref.key] = UUID(picked)
            except ValueError:
                return redirect_to(
                    "/rules", error="Choose a category for each one the file refers to."
                )
            if choices[ref.key] not in categories:
                return redirect_to("/rules", error="That category no longer exists.")

    existing = await provider.rules.list_rules(OWNER_USER_ID)
    changes = portability.resolve(plan, choices, existing)
    for rule in changes.add:
        await provider.rules.create_rule(rule)
    for rule in changes.update:
        await provider.rules.update_rule(rule)

    parts = [f"{len(changes.add)} added", f"{len(changes.update)} updated"]
    if changes.unchanged:
        parts.append(f"{changes.unchanged} already the same")
    notice = "Rules imported: " + ", ".join(parts) + "."
    if created:
        notice += f" Categories created: {', '.join(created)}."
    if changes.paused:
        notice += f" Paused (no category): {', '.join(changes.paused)}."
    if changes.add or changes.update:
        notice += " Use “Reset and reapply” to run them on existing transactions."
    return redirect_to("/rules", notice=notice)


async def _create_category(provider: Provider, name: str, group_name: str | None) -> Category:
    """A category from an imported file, in its group (made if missing) or "Imported"."""
    wanted = (group_name or "Imported").strip()
    groups = await provider.categories.list_groups(OWNER_USER_ID)
    group = next((g for g in groups if g.name.strip().lower() == wanted.lower()), None)
    if group is None:
        group = await provider.categories.create_group(
            CategoryGroup(
                id=uuid4(),
                user_id=OWNER_USER_ID,
                name=wanted,
                sort_order=max((g.sort_order for g in groups), default=0) + 1,
            )
        )
    return await provider.categories.create_category(
        Category(id=uuid4(), user_id=OWNER_USER_ID, group_id=group.id, name=name.strip())
    )


@router.post("/rules/reset-defaults")
async def reset_to_defaults(provider: Provider) -> RedirectResponse:
    """Replace all rules with the built-in defaults, then re-run them."""
    await reset_rules_to_defaults(user_id=OWNER_USER_ID)
    await provider.rules.apply_all_rules(OWNER_USER_ID)
    await link_transfers(provider, OWNER_USER_ID)
    return redirect_to("/rules")


@router.post("/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: UUID, provider: Provider) -> RedirectResponse:
    """Pause or resume a rule."""
    rule = await provider.rules.get_rule(rule_id)
    if rule:
        rule.is_active = not rule.is_active
        await provider.rules.update_rule(rule)
    return redirect_to("/rules")


@router.post("/rules/{rule_id}/delete")
async def delete_rule(request: Request, rule_id: UUID, provider: Provider) -> Response:
    """Delete a rule."""
    await provider.rules.delete_rule(rule_id)
    if request.headers.get("hx-request") and not request.headers.get("hx-boosted"):
        return Response(content="", status_code=200)
    return redirect_to("/rules")


@router.post("/rules/apply-all", response_class=HTMLResponse)
async def run_all_rules(request: Request, provider: Provider) -> Response:
    """Re-run all active rules over existing transactions."""
    changed = await provider.rules.apply_all_rules(OWNER_USER_ID)
    linked = await link_transfers(provider, OWNER_USER_ID)
    if request.headers.get("hx-request") and not request.headers.get("hx-boosted"):
        return HTMLResponse(
            '<div class="alert alert-success bg-emerald-500/10 text-emerald-700 text-xs py-2 px-3 '
            'border border-emerald-500/20 rounded-xl flex items-center justify-between">'
            f"<span>Rules applied: <strong>{changed}</strong> transactions updated, "
            f"<strong>{linked}</strong> transfers paired.</span>"
            "</div>"
        )
    return redirect_to(
        "/rules",
        notice=f"Rules applied: {changed} transactions updated, {linked} transfers paired.",
    )
