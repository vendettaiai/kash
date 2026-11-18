"""Database Category and Group repository."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import CategoryGroupModel, CategoryModel, RuleModel
from kash.db.repositories.mappers import category_group_to_domain, category_to_domain
from kash.domain.models import Category, CategoryGroup


class SqlCategoryRepository:
    """SQL-backed implementation of CategoryRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def list_groups(self, user_id: UUID) -> list[CategoryGroup]:
        async with self._session_factory() as session:
            stmt = (
                select(CategoryGroupModel)
                .where(CategoryGroupModel.user_id == user_id)
                .order_by(CategoryGroupModel.sort_order, CategoryGroupModel.name)
            )
            result = await session.execute(stmt)
            return [category_group_to_domain(row) for row in result.scalars().all()]

    async def get_group(self, group_id: UUID) -> CategoryGroup | None:
        async with self._session_factory() as session:
            stmt = select(CategoryGroupModel).where(CategoryGroupModel.id == group_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return category_group_to_domain(row) if row else None

    async def create_group(self, group: CategoryGroup) -> CategoryGroup:
        async with self._session_factory() as session:
            model = CategoryGroupModel(
                id=group.id,
                user_id=group.user_id,
                name=group.name,
                icon=group.icon,
                color=group.color,
                sort_order=group.sort_order,
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)
            return category_group_to_domain(model)

    async def update_group(self, group: CategoryGroup) -> CategoryGroup:
        async with self._session_factory() as session:
            model = await session.get(CategoryGroupModel, group.id)
            if not model:
                raise ValueError(f"CategoryGroup {group.id} not found")

            model.name = group.name
            model.icon = group.icon
            model.color = group.color
            model.sort_order = group.sort_order

            await session.commit()
            await session.refresh(model)
            return category_group_to_domain(model)

    async def list_categories(self, user_id: UUID, include_hidden: bool = False) -> list[Category]:
        async with self._session_factory() as session:
            stmt = select(CategoryModel).where(CategoryModel.user_id == user_id)
            if not include_hidden:
                stmt = stmt.where(CategoryModel.is_hidden.is_(False))
            stmt = stmt.order_by(CategoryModel.sort_order, CategoryModel.name)

            result = await session.execute(stmt)
            return [category_to_domain(row) for row in result.scalars().all()]

    async def get_category(self, category_id: UUID) -> Category | None:
        async with self._session_factory() as session:
            stmt = select(CategoryModel).where(CategoryModel.id == category_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return category_to_domain(row) if row else None

    async def create_category(self, category: Category) -> Category:
        async with self._session_factory() as session:
            model = CategoryModel(
                id=category.id,
                user_id=category.user_id,
                group_id=category.group_id,
                name=category.name,
                icon=category.icon,
                color=category.color,
                is_system=category.is_system,
                is_hidden=category.is_hidden,
                is_transfer=category.is_transfer,
                sort_order=category.sort_order,
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)
            return category_to_domain(model)

    async def update_category(self, category: Category) -> Category:
        async with self._session_factory() as session:
            model = await session.get(CategoryModel, category.id)
            if not model:
                raise ValueError(f"Category {category.id} not found")

            model.group_id = category.group_id
            model.name = category.name
            model.icon = category.icon
            model.color = category.color
            model.is_hidden = category.is_hidden
            model.is_transfer = category.is_transfer
            model.sort_order = category.sort_order

            await session.commit()
            await session.refresh(model)
            return category_to_domain(model)

    async def delete_category(self, category_id: UUID) -> bool:
        """Delete a category.

        Transactions and bills in it become uncategorized (FK ``SET NULL``). Rules
        that assigned it lose that action; rules left with no actions are paused.
        """
        async with self._session_factory() as session:
            model = await session.get(CategoryModel, category_id)
            if not model:
                return False

            target = str(category_id)
            rules = await session.execute(
                select(RuleModel).where(RuleModel.user_id == model.user_id)
            )
            for rule in rules.scalars().all():
                actions = list(rule.actions or [])
                kept = [
                    a
                    for a in actions
                    if not (a.get("op") == "set_category" and str(a.get("value")) == target)
                ]
                if len(kept) != len(actions):
                    rule.actions = kept
                    if not kept:
                        rule.is_active = False

            await session.delete(model)
            await session.commit()
            return True
