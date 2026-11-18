"""AI categorization: Settings card and the AI page (docs/AI_PLAN.md).

AI is a fallback after rules and transfer linking. Answers at or above the
confidence threshold are applied automatically; everything else stays in the
normal Uncategorized list. The AI page is for looking back (recent AI
categorizations, Undo), approving rule proposals, and running it by hand.
"""

import json
from dataclasses import dataclass, replace
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from kash.domain.models import AISuggestion, Category, SuggestionStatus, Transaction, UserSettings
from kash.services.ai import categorize, rule_proposals
from kash.services.ai import options as ai_options
from kash.services.ai.categorize import (
    accept_guesses,
    ask_again,
    connection_for,
    find_candidates,
    retryable,
    saved_key,
)
from kash.services.ai.categorize import run as run_categorization
from kash.services.ai.categorize import undo as undo_suggestions
from kash.services.ai.prompt import (
    DEFAULT_INSTRUCTIONS,
    OUTPUT_FORMAT,
    build_request,
    is_default,
    system_prompt,
)
from kash.services.ai.providers import (
    PRESETS,
    AIClient,
    AIProviderError,
    Connection,
    check_connection,
    is_plain_http_to_remote,
)
from kash.services.security import encrypt_credentials
from kash.web.deps import OWNER_USER_ID, Provider, get_templates, redirect_to, safe_local_path

router = APIRouter()

THRESHOLD_LABELS = {
    0.95: "95%: only when very sure",
    0.90: "90% (recommended)",
    0.85: "85%",
    0.80: "80%",
    0.70: "70%",
    0.60: "60%: more automatic, more mistakes",
}
RECENT_SHOWN = 10  # recent AI categorizations shown before "Show all"
RECENT_ALL = 200


def settings_context(saved: UserSettings) -> dict[str, Any]:
    """What the Settings page needs to show the AI card (never the key itself)."""
    key, unreadable = saved_key(saved)
    provider = saved.ai_provider or "openai"
    preset = PRESETS.get(provider, PRESETS["openai"])
    base_url = (saved.ai_base_url or preset.base_url) if preset.custom_url else ""
    opts = ai_options.from_stored(saved.ai_options)
    return {
        "ai": saved,
        "ai_presets": PRESETS,
        "ai_provider": provider,
        "ai_base_url": base_url,
        "ai_key_hint": f"••••{key[-4:]}" if len(key) >= 8 else ("saved" if key else ""),
        "ai_key_unreadable": unreadable,
        "ai_threshold": round(ai_options.threshold(saved.ai_auto_apply_threshold) * 100),
        "ai_threshold_choices": [
            (round(t * 100), THRESHOLD_LABELS[t]) for t in ai_options.THRESHOLDS
        ],
        "ai_opts": opts,
        "ai_prompt": opts.system_prompt or DEFAULT_INSTRUCTIONS,
        "ai_default_prompt": DEFAULT_INSTRUCTIONS,
        "ai_output_format": OUTPUT_FORMAT,
        "ai_batch_sizes": ai_options.BATCH_SIZES,
        "ai_tokens_range": ai_options.MAX_TOKENS_RANGE,
        "ai_http_warning": preset.custom_url and is_plain_http_to_remote(base_url),
    }


def _advanced(form: dict[str, str], current: ai_options.AIOptions) -> ai_options.AIOptions | str:
    """Advanced options from the form, or an error message."""
    prompt = form.get("system_prompt", "").replace("\r\n", "\n").strip()
    if is_default(prompt):
        prompt = ""  # unchanged: keep following the built-in prompt
    if len(prompt) > ai_options.MAX_PROMPT_CHARS:
        return f"The prompt is too long (at most {ai_options.MAX_PROMPT_CHARS:,} characters)."
    try:
        temperature = float(form.get("temperature") or ai_options.DEFAULT_TEMPERATURE)
        tokens = int(form["max_tokens"]) if form.get("max_tokens", "").strip() else None
        batch = int(form.get("batch_size") or ai_options.DEFAULT_BATCH_SIZE)
    except ValueError:
        return "Temperature, output tokens and batch size must be numbers."
    low, high = ai_options.MAX_TOKENS_RANGE
    if not 0 <= temperature <= 1:
        return "Temperature must be between 0 and 1."
    if tokens is not None and not low <= tokens <= high:
        return f"Maximum output tokens must be {low:,}–{high:,}, or empty for automatic."
    if batch not in ai_options.BATCH_SIZES:
        return "Unknown batch size."
    return replace(
        current,
        system_prompt=prompt or None,
        temperature=temperature,
        max_tokens=tokens,
        batch_size=batch,
        send_examples=bool(form.get("send_examples")),
    )


@router.post("/settings/ai")
async def save_ai_settings(request: Request, provider: Provider) -> RedirectResponse:
    """Save the AI settings. An empty key field keeps the saved key."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    ai_provider = form.get("ai_provider", "")
    model, base_url = form.get("model", ""), form.get("base_url", "")
    api_key, enabled = form.get("api_key", ""), form.get("enabled", "")
    saved = await provider.settings.get_settings(OWNER_USER_ID)
    try:
        threshold = int(form.get("threshold", "90")) / 100
    except ValueError:
        threshold = -1.0
    if ai_provider not in PRESETS or threshold not in ai_options.THRESHOLDS:
        return redirect_to("/settings", error="Unknown AI provider or confidence level.")
    advanced = _advanced(form, ai_options.from_stored(saved.ai_options))
    if isinstance(advanced, str):
        return redirect_to("/settings", error=advanced)
    key = api_key.strip() or saved_key(saved)[0]
    preset = PRESETS[ai_provider]
    if enabled:  # only a complete setup can be switched on
        try:
            conn = Connection.build(ai_provider, base_url, model, key)
        except AIProviderError as exc:
            return redirect_to("/settings", error=f"AI not enabled: {exc}")
        if not conn.model:
            return redirect_to("/settings", error="AI not enabled: choose a model.")
    updated = saved.model_copy(
        update={
            "ai_enabled": bool(enabled),
            "ai_provider": ai_provider,
            "ai_base_url": base_url.strip().rstrip("/") if preset.custom_url else None,
            "ai_model": model.strip() or None,
            "ai_api_key_encrypted": encrypt_credentials(key) if key else None,
            "ai_auto_apply_threshold": threshold,
            "ai_run_after_sync": bool(form.get("after_sync")),
            "ai_options": ai_options.to_stored(advanced),
        }
    )
    await provider.settings.save_settings(updated)
    state = "on" if enabled else "off"
    return redirect_to("/settings", notice=f"AI settings saved (AI categorization is {state}).")


@router.post("/settings/ai/test", response_class=HTMLResponse)
async def test_ai_connection(
    request: Request,
    provider: Provider,
    ai_provider: Annotated[str, Form()] = "",
    model: Annotated[str, Form()] = "",
    base_url: Annotated[str, Form()] = "",
    api_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Check the values currently in the form (before saving). No transaction data is sent."""
    saved = await provider.settings.get_settings(OWNER_USER_ID)
    key = api_key.strip() or saved_key(saved)[0]
    try:
        conn = Connection.build(ai_provider, base_url, model, key)
        result = await check_connection(conn)
        ok, message, models, seconds = result.ok, result.message, result.models, result.seconds
    except AIProviderError as exc:
        ok, message, models, seconds = False, str(exc), [], 0.0
    return get_templates(request).TemplateResponse(
        request=request,
        name="components/ai_test_result.html",
        context={"ok": ok, "message": message, "models": models, "seconds": seconds},
    )


@router.post("/settings/ai/key/remove")
async def remove_ai_key(provider: Provider) -> RedirectResponse:
    """Forget the API key (and turn AI off if the provider needs one)."""
    saved = await provider.settings.get_settings(OWNER_USER_ID)
    preset = PRESETS.get(saved.ai_provider or "")
    enabled = saved.ai_enabled and not (preset and preset.needs_key)
    await provider.settings.save_settings(
        saved.model_copy(update={"ai_api_key_encrypted": None, "ai_enabled": enabled})
    )
    return redirect_to("/settings", notice="API key removed.")


# --- AI page: history, rule proposals, run by hand -----------------------------------


@dataclass
class HistoryRow:
    suggestion: AISuggestion
    transaction: Transaction
    category: Category | None  # what the AI chose
    current: Category | None  # the transaction's category now

    @property
    def active(self) -> bool:
        """The AI's category is still in place (so Undo is offered)."""
        return (
            self.suggestion.status == SuggestionStatus.AUTO_APPLIED
            and self.transaction.category_id == self.suggestion.category_id
        )

    @property
    def percent(self) -> int:
        return round(self.suggestion.confidence * 100)


HISTORY_STATUSES = (
    SuggestionStatus.AUTO_APPLIED,
    SuggestionStatus.ACCEPTED,  # the user took the AI's category (e.g. from an older version)
    SuggestionStatus.CHANGED,
    SuggestionStatus.REJECTED,
    SuggestionStatus.UNKNOWN,  # the AI looked and had no answer
    SuggestionStatus.PENDING,  # the AI had a guess, below the confidence threshold
)


async def _history(provider: Provider, limit: int) -> tuple[list[HistoryRow], int]:
    """What the AI categorized or passed on, newest first: (rows, total)."""
    categories = {
        c.id: c
        for c in await provider.categories.list_categories(OWNER_USER_ID, include_hidden=True)
    }
    rows = [
        s for s in await provider.ai.list_suggestions(OWNER_USER_ID) if s.status in HISTORY_STATUSES
    ]
    rows.sort(key=lambda s: s.resolved_at or s.created_at, reverse=True)
    history = []
    for s in rows[:limit]:
        tx = await provider.transactions.get_transaction(s.transaction_id)
        if tx is not None:
            history.append(
                HistoryRow(
                    s,
                    tx,
                    categories.get(s.category_id) if s.category_id else None,
                    categories.get(tx.category_id) if tx.category_id else None,
                )
            )
    return history, len(rows)


@router.get("/ai/review", response_class=HTMLResponse)
async def ai_review(request: Request, provider: Provider, show: str = "") -> HTMLResponse:
    """Recent AI categorizations (with Undo), rule proposals, and a manual run."""
    saved = await provider.settings.get_settings(OWNER_USER_ID)
    candidates = await find_candidates(provider, OWNER_USER_ID)
    history, total = await _history(provider, RECENT_ALL if show == "all" else RECENT_SHOWN)
    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/ai_review.html",
        context={
            "active_page": "ai_review",
            "ai_ready": connection_for(saved) is not None,
            "threshold": round(ai_options.threshold(saved.ai_auto_apply_threshold) * 100),
            "after_sync": saved.ai_run_after_sync,
            "running": categorize.is_running(),
            "history": history,
            "history_total": total,
            "show_all": show == "all",
            "proposals": await rule_proposals.find_proposals(provider, OWNER_USER_ID),
            "ai_conditions": await rule_proposals.ai_added_conditions(provider, OWNER_USER_ID),
            "candidates": candidates,
            "retry_count": len(await retryable(provider, OWNER_USER_ID)),
            "runs": await provider.ai.list_runs(OWNER_USER_ID, limit=10),
        },
    )


@router.get("/ai/run/preview", response_class=HTMLResponse)
async def ai_run_preview(request: Request, provider: Provider) -> HTMLResponse:
    """Exactly what the first request would contain (nothing is sent)."""
    saved = await provider.settings.get_settings(OWNER_USER_ID)
    opts = ai_options.from_stored(saved.ai_options)
    candidates = await find_candidates(provider, OWNER_USER_ID)
    batch = candidates.groups[: opts.batch_size]
    request_data = (
        build_request(
            batch,
            candidates.categories,
            candidates.income_ids,
            candidates.account_types,
            candidates.examples if opts.send_examples else [],
            system_prompt(opts.system_prompt),
        )
        if batch
        else None
    )
    return get_templates(request).TemplateResponse(
        request=request,
        name="components/ai_preview.html",
        context={
            "payload": json.dumps(request_data.payload, indent=2, ensure_ascii=False)
            if request_data
            else "",
            "system": request_data.system if request_data else "",
            "groups": len(batch),
            "total_groups": len(candidates.groups),
        },
    )


@router.post("/ai/run")
async def ai_run(provider: Provider, background: BackgroundTasks) -> RedirectResponse:
    """Categorize every eligible uncategorized transaction now (in the background)."""
    conn = connection_for(await provider.settings.get_settings(OWNER_USER_ID))
    if conn is None:
        return redirect_to("/ai/review", error="Turn on AI categorization in Settings first.")
    if categorize.is_running():
        return redirect_to("/ai/review", notice="AI is already categorizing. Refresh in a moment.")
    groups = len((await find_candidates(provider, OWNER_USER_ID)).groups)
    if not groups:
        return redirect_to("/ai/review", notice="Nothing to categorize.")
    background.add_task(run_categorization, provider, OWNER_USER_ID, AIClient(conn))
    noun = "merchant" if groups == 1 else "merchants"
    return redirect_to(
        "/ai/review", notice=f"Categorizing {groups} {noun}. Refresh in a moment to see results."
    )


@router.post("/ai/ask-again")
async def ai_ask_again(
    provider: Provider, background: BackgroundTasks, ids: Annotated[str, Form()] = ""
) -> RedirectResponse:
    """Ask the AI again about "No answer" transactions (the ones in ``ids``, or all of
    them when empty), with the current prompt and settings."""
    conn = connection_for(await provider.settings.get_settings(OWNER_USER_ID))
    if conn is None:
        return redirect_to("/ai/review", error="Turn on AI categorization in Settings first.")
    try:
        suggestion_ids = [UUID(part) for part in ids.split(",") if part] or None
    except ValueError:
        return redirect_to("/ai/review", error="Unknown suggestion.")
    if categorize.is_running():
        return redirect_to("/ai/review", notice="AI is already categorizing. Refresh in a moment.")
    count = len(await retryable(provider, OWNER_USER_ID, suggestion_ids))
    if not count:
        return redirect_to("/ai/review", notice="Nothing to ask again.")
    background.add_task(ask_again, provider, OWNER_USER_ID, AIClient(conn), suggestion_ids)
    noun = "transaction" if count == 1 else "transactions"
    return redirect_to(
        "/ai/review", notice=f"Asking the AI again about {count} {noun}. Refresh in a moment."
    )


@router.post("/ai/accept")
async def ai_accept_guess(
    provider: Provider, ids: Annotated[str, Form()], next: Annotated[str, Form()] = ""
) -> RedirectResponse:
    """Use the AI's guess for these transactions (it was below the confidence threshold).
    ``next``: where to go afterwards (a page on this site); default the AI page."""
    target = safe_local_path(next, "/ai/review")
    try:
        suggestion_ids = [UUID(part) for part in ids.split(",") if part]
    except ValueError:
        return redirect_to(target, error="Unknown suggestion.")
    accepted = await accept_guesses(provider, OWNER_USER_ID, suggestion_ids)
    if not accepted:
        return redirect_to(target, notice="Nothing to apply; it was already categorized.")
    noun = "transaction" if len(accepted) == 1 else "transactions"
    message = f"Categorized {len(accepted)} {noun} with the AI's guess."
    added = await rule_proposals.auto_apply(provider, OWNER_USER_ID, set(accepted))
    if added:
        message = f"{message} {rule_proposals.added_notice(added)}"
    return redirect_to(target, notice=message)


@router.post("/ai/undo")
async def ai_undo(provider: Provider, ids: Annotated[str, Form()]) -> RedirectResponse:
    """Take back categories the AI applied; the transactions are uncategorized again."""
    try:
        suggestion_ids = [UUID(part) for part in ids.split(",") if part]
    except ValueError:
        return redirect_to("/ai/review", error="Unknown suggestion.")
    undone = await undo_suggestions(provider, OWNER_USER_ID, suggestion_ids)
    noun = "transaction" if undone == 1 else "transactions"
    return redirect_to("/ai/review", notice=f"Undone for {undone} {noun}; now uncategorized.")


@router.post("/ai/proposals/{key}/apply")
async def ai_apply_proposal(key: str, provider: Provider) -> RedirectResponse:
    """Approve a rule proposal."""
    proposal = await rule_proposals.apply_proposal(provider, OWNER_USER_ID, key)
    if proposal is None:
        return redirect_to("/ai/review", error="That proposal no longer applies.")
    target = (
        f"added to rule “{proposal.rule.name}”"
        if proposal.rule
        else f"new rule “{proposal.category.name}”"
    )
    return redirect_to(
        "/ai/review",
        notice=f"Rule updated: {proposal.condition['field']} contains "
        f"“{proposal.condition['value']}” {target}.",
    )


@router.post("/ai/proposals/{key}/dismiss")
async def ai_dismiss_proposal(key: str, provider: Provider) -> RedirectResponse:
    await rule_proposals.dismiss_proposal(provider, OWNER_USER_ID, key)
    return redirect_to("/ai/review", notice="Proposal dismissed.")


@router.post("/ai/rules/{rule_id}/conditions/{index}/remove")
async def ai_remove_condition(rule_id: UUID, index: int, provider: Provider) -> RedirectResponse:
    """Remove a condition the AI added (never one the user wrote)."""
    if await rule_proposals.remove_ai_condition(provider, OWNER_USER_ID, rule_id, index):
        return redirect_to("/ai/review", notice="AI condition removed.")
    return redirect_to("/ai/review", error="That condition no longer exists.")
