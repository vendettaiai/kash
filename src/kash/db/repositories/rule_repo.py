"""Database Rule repository for categorization rules."""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import RuleModel
from kash.db.repositories.mappers import rule_to_domain
from kash.domain.models import Rule


class SqlRuleRepository:
    """SQL-backed implementation of RuleRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def list_rules(self, user_id: UUID, rule_type: str | None = None) -> list[Rule]:
        async with self._session_factory() as session:
            stmt = select(RuleModel).where(RuleModel.user_id == user_id)
            if rule_type:
                stmt = stmt.where(RuleModel.rule_type == rule_type)
            stmt = stmt.order_by(RuleModel.priority.asc(), RuleModel.id.asc())
            result = await session.execute(stmt)
            return [rule_to_domain(row) for row in result.scalars().all()]

    async def get_rule(self, rule_id: UUID) -> Rule | None:
        async with self._session_factory() as session:
            stmt = select(RuleModel).where(RuleModel.id == rule_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return rule_to_domain(row) if row else None

    async def create_rule(self, rule: Rule) -> Rule:
        async with self._session_factory() as session:
            model = RuleModel(
                id=rule.id,
                user_id=rule.user_id,
                name=rule.name,
                priority=rule.priority,
                is_active=rule.is_active,
                rule_type=rule.rule_type,
                action_mode=rule.action_mode,
                config=rule.config,
                conditions_op=rule.conditions_op,
                conditions=[
                    c.model_dump() if hasattr(c, "model_dump") else c for c in rule.conditions
                ],
                actions=[a.model_dump() if hasattr(a, "model_dump") else a for a in rule.actions],
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)
            return rule_to_domain(model)

    async def update_rule(self, rule: Rule) -> Rule:
        async with self._session_factory() as session:
            model = await session.get(RuleModel, rule.id)
            if not model:
                raise ValueError(f"Rule {rule.id} not found")

            model.name = rule.name
            model.priority = rule.priority
            model.is_active = rule.is_active
            model.rule_type = rule.rule_type
            model.action_mode = rule.action_mode
            model.config = rule.config
            model.conditions_op = rule.conditions_op
            model.conditions = [
                c.model_dump() if hasattr(c, "model_dump") else c for c in rule.conditions
            ]
            model.actions = [
                a.model_dump() if hasattr(a, "model_dump") else a for a in rule.actions
            ]

            await session.commit()
            await session.refresh(model)
            return rule_to_domain(model)

    async def delete_rule(self, rule_id: UUID) -> bool:
        async with self._session_factory() as session:
            stmt = delete(RuleModel).where(RuleModel.id == rule_id)
            result = await session.execute(stmt)
            await session.commit()
            return bool(result.rowcount and result.rowcount > 0)

    async def apply_all_rules(self, user_id: UUID) -> int:
        """Apply all active rules across the user's transaction ledger.

        Returns the number of transactions that were modified.
        """
        from kash.db.models import CategoryModel, TransactionModel
        from kash.db.repositories.mappers import transaction_to_domain
        from kash.services.rules.engine import apply_rules_to_transaction

        async with self._session_factory() as session:
            # 1. Fetch active rules ordered by priority ASC, id ASC
            rule_stmt = (
                select(RuleModel)
                .where(
                    RuleModel.user_id == user_id,
                    RuleModel.is_active.is_(True),
                )
                .order_by(RuleModel.priority.asc(), RuleModel.id.asc())
            )
            rules_res = await session.execute(rule_stmt)
            rules = [rule_to_domain(r) for r in rules_res.scalars().all()]
            if not rules:
                return 0

            # 2. Fetch hidden categories
            hidden_stmt = select(CategoryModel.id).where(
                CategoryModel.user_id == user_id, CategoryModel.is_hidden.is_(True)
            )
            hidden_res = await session.execute(hidden_stmt)
            hidden_cat_ids = set(hidden_res.scalars().all())

            # 3. Fetch all user transactions
            tx_stmt = select(TransactionModel).where(TransactionModel.user_id == user_id)
            tx_res = await session.execute(tx_stmt)
            tx_models = tx_res.scalars().all()

            changed_count = 0
            for tx_m in tx_models:
                tx_domain = transaction_to_domain(tx_m)
                before = (
                    tx_domain.category_id,
                    tx_domain.clean_description,
                    tx_domain.merchant_name,
                    tx_domain.notes,
                    tx_domain.is_ignored,
                )

                apply_rules_to_transaction(
                    rules,
                    tx_domain,
                    hidden_category_ids=hidden_cat_ids,
                )

                after = (
                    tx_domain.category_id,
                    tx_domain.clean_description,
                    tx_domain.merchant_name,
                    tx_domain.notes,
                    tx_domain.is_ignored,
                )

                if before != after:
                    tx_m.category_id = tx_domain.category_id
                    tx_m.clean_description = tx_domain.clean_description
                    tx_m.merchant_name = tx_domain.merchant_name
                    tx_m.notes = tx_domain.notes
                    tx_m.is_ignored = tx_domain.is_ignored
                    changed_count += 1

            if changed_count > 0:
                await session.commit()
            return changed_count
