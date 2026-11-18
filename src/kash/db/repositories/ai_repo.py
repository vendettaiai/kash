"""Database repository for AI suggestions and the AI run log."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import AIRunModel, AISuggestionModel
from kash.domain.models import AIRun, AISuggestion, SuggestionStatus


def _suggestion(m: AISuggestionModel) -> AISuggestion:
    return AISuggestion(
        id=m.id,
        user_id=m.user_id,
        transaction_id=m.transaction_id,
        category_id=m.category_id,
        confidence=m.confidence,
        reason=m.reason,
        rule_pattern=m.rule_pattern,
        status=SuggestionStatus(m.status),
        provider=m.provider,
        model=m.model,
        created_at=m.created_at,
        resolved_at=m.resolved_at,
    )


def _run(m: AIRunModel) -> AIRun:
    return AIRun(
        id=m.id,
        user_id=m.user_id,
        started_at=m.started_at,
        trigger=m.trigger,
        provider=m.provider,
        model=m.model,
        items_sent=m.items_sent,
        transactions_covered=m.transactions_covered,
        requests=m.requests,
        prompt_tokens=m.prompt_tokens,
        completion_tokens=m.completion_tokens,
        status=m.status,
        error=m.error,
    )


class SqlAIRepository:
    """SQL-backed implementation of AIRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def list_suggestions(
        self, user_id: UUID, status: str | None = None
    ) -> list[AISuggestion]:
        async with self._session_factory() as session:
            stmt = select(AISuggestionModel).where(AISuggestionModel.user_id == user_id)
            if status:
                stmt = stmt.where(AISuggestionModel.status == status)
            result = await session.execute(stmt.order_by(AISuggestionModel.created_at.desc()))
            return [_suggestion(m) for m in result.scalars().all()]

    async def get_suggestions(self, suggestion_ids: list[UUID]) -> list[AISuggestion]:
        if not suggestion_ids:
            return []
        async with self._session_factory() as session:
            result = await session.execute(
                select(AISuggestionModel).where(AISuggestionModel.id.in_(suggestion_ids))
            )
            return [_suggestion(m) for m in result.scalars().all()]

    async def suggestions_for_transactions(self, transaction_ids: list[UUID]) -> list[AISuggestion]:
        if not transaction_ids:
            return []
        async with self._session_factory() as session:
            result = await session.execute(
                select(AISuggestionModel).where(
                    AISuggestionModel.transaction_id.in_(transaction_ids)
                )
            )
            return [_suggestion(m) for m in result.scalars().all()]

    async def sent_transaction_ids(self, user_id: UUID) -> set[UUID]:
        """Transactions that already have a suggestion (they are never sent again)."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(AISuggestionModel.transaction_id).where(AISuggestionModel.user_id == user_id)
            )
            return set(result.scalars().all())

    async def save_suggestions(self, suggestions: list[AISuggestion]) -> None:
        """Insert or update suggestions (one per transaction)."""
        async with self._session_factory() as session:
            for s in suggestions:
                model = await session.get(AISuggestionModel, s.id)
                if model is None:
                    model = AISuggestionModel(id=s.id, user_id=s.user_id, created_at=s.created_at)
                    session.add(model)
                model.transaction_id = s.transaction_id
                model.category_id = s.category_id
                model.confidence = s.confidence
                model.reason = s.reason
                model.rule_pattern = s.rule_pattern
                model.status = s.status.value
                model.provider = s.provider
                model.model = s.model
                model.resolved_at = s.resolved_at
            await session.commit()

    async def delete_suggestions(self, suggestion_ids: list[UUID]) -> None:
        async with self._session_factory() as session:
            for suggestion_id in suggestion_ids:
                model = await session.get(AISuggestionModel, suggestion_id)
                if model is not None:
                    await session.delete(model)
            await session.commit()

    async def save_run(self, run: AIRun) -> AIRun:
        async with self._session_factory() as session:
            model = await session.get(AIRunModel, run.id)
            if model is None:
                model = AIRunModel(id=run.id, user_id=run.user_id)
                session.add(model)
            for field in (
                "started_at", "trigger", "provider", "model", "items_sent",
                "transactions_covered", "requests", "prompt_tokens", "completion_tokens",
                "status", "error",
            ):  # fmt: skip
                setattr(model, field, getattr(run, field))
            await session.commit()
            return run

    async def list_runs(self, user_id: UUID, limit: int = 20) -> list[AIRun]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(AIRunModel)
                .where(AIRunModel.user_id == user_id)
                .order_by(AIRunModel.started_at.desc())
                .limit(limit)
            )
            return [_run(m) for m in result.scalars().all()]

    async def items_sent_since(self, user_id: UUID, since: datetime) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(AIRunModel.items_sent), 0)).where(
                    AIRunModel.user_id == user_id, AIRunModel.started_at >= since
                )
            )
            return int(result.scalar_one())
