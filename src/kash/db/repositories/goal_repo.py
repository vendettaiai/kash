"""Database Goal repository."""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import GoalModel
from kash.db.repositories.mappers import goal_to_domain
from kash.domain.models import Goal


class SqlGoalRepository:
    """SQL-backed implementation of GoalRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def list_goals(self, user_id: UUID) -> list[Goal]:
        async with self._session_factory() as session:
            stmt = (
                select(GoalModel)
                .where(GoalModel.user_id == user_id)
                .order_by(GoalModel.created_at.asc())
            )
            result = await session.execute(stmt)
            return [goal_to_domain(row) for row in result.scalars().all()]

    async def get_goal(self, goal_id: UUID) -> Goal | None:
        async with self._session_factory() as session:
            stmt = select(GoalModel).where(GoalModel.id == goal_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return goal_to_domain(row) if row else None

    async def create_goal(self, goal: Goal) -> Goal:
        async with self._session_factory() as session:
            model = GoalModel(
                id=goal.id,
                user_id=goal.user_id,
                name=goal.name,
                target_amount=goal.target_amount,
                current_amount=goal.current_amount,
                target_date=goal.target_date,
                icon=goal.icon,
                color=goal.color,
                notes=goal.notes,
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)
            return goal_to_domain(model)

    async def update_goal(self, goal: Goal) -> Goal:
        async with self._session_factory() as session:
            model = await session.get(GoalModel, goal.id)
            if not model:
                raise ValueError(f"Goal {goal.id} not found")

            model.name = goal.name
            model.target_amount = goal.target_amount
            model.current_amount = goal.current_amount
            model.target_date = goal.target_date
            model.icon = goal.icon
            model.color = goal.color
            model.notes = goal.notes

            await session.commit()
            await session.refresh(model)
            return goal_to_domain(model)

    async def delete_goal(self, goal_id: UUID) -> bool:
        async with self._session_factory() as session:
            stmt = delete(GoalModel).where(GoalModel.id == goal_id)
            result = await session.execute(stmt)
            await session.commit()
            return bool(result.rowcount and result.rowcount > 0)
