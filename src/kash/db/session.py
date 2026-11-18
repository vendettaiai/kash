"""Database engine, session factory, and transaction management."""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kash.config import get_settings
from kash.db.base import Base
from kash.owner import OWNER_USER_ID

if TYPE_CHECKING:
    from alembic.config import Config

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine_and_factory(
    database_url: str | None = None, echo: bool = False
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create and configure a new AsyncEngine and async_sessionmaker."""
    url = database_url or get_settings().database_url

    engine_kwargs: dict[str, Any] = {
        "echo": echo,
        "future": True,
    }

    if url.startswith("sqlite"):
        # SQLite specific configuration
        engine_kwargs["connect_args"] = {"check_same_thread": False}
        engine = create_async_engine(url, **engine_kwargs)

        # Enforce foreign key constraints on SQLite connections
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    else:
        # PostgreSQL / asyncpg configuration
        engine_kwargs.update(
            {
                "pool_size": 10,
                "max_overflow": 20,
                "pool_pre_ping": True,
            }
        )
        engine = create_async_engine(url, **engine_kwargs)

    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    return engine, session_factory


def get_engine() -> AsyncEngine:
    """Return singleton AsyncEngine instance."""
    global _engine, _session_factory
    if _engine is None:
        _engine, _session_factory = create_engine_and_factory()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return singleton session factory."""
    global _engine, _session_factory
    if _session_factory is None:
        _engine, _session_factory = create_engine_and_factory()
    return _session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for yielding transactional database sessions."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str) -> "Config":
    """Alembic settings for ``url`` without alembic.ini (migrations ship in the package)."""
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


# Installs from before migrations ran at startup were made by ``create_all`` and
# match one of these revisions exactly (newest first): (table that proves it, revision).
LEGACY_BASELINES = (
    ("balance_snapshots", "b4e8c2d17a90"),  # 0002
    ("users", "c36adc0a21cb"),  # 0001
)


async def init_db(engine: AsyncEngine | None = None) -> str:
    """Bring the database schema up to date at startup (ADR-017). Returns what was done.

    - Empty database: run every migration ("created").
    - Tables but no Alembic history (made by ``create_all`` before migrations ran
      at startup): record the revision it matches, then apply the newer ones ("stamped").
    - Otherwise: apply new migrations ("upgraded").
    """
    from alembic import command

    import kash.db.models  # noqa: F401

    eng = engine or get_engine()
    if eng.url.get_backend_name() == "sqlite" and eng.url.database in (None, "", ":memory:"):
        # In-memory (tests): Alembic's own connection would see a different, empty database.
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return "created"
    async with eng.connect() as conn:
        tables = set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))
    config = alembic_config(eng.url.render_as_string(hide_password=False))

    if tables and "alembic_version" not in tables:
        baseline = next(rev for table, rev in LEGACY_BASELINES if table in tables)
        await asyncio.to_thread(command.stamp, config, baseline)
        await asyncio.to_thread(command.upgrade, config, "head")
        return "stamped"
    await asyncio.to_thread(command.upgrade, config, "head")
    return "upgraded" if tables else "created"


async def drop_db(engine: AsyncEngine | None = None) -> None:
    """Drop all database tables (used for testing)."""
    import kash.db.models  # noqa: F401

    eng = engine or get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def close_db() -> None:
    """Dispose singleton engine connections."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def _default_rule_models(user_id: uuid.UUID, category_name_map: dict[str, uuid.UUID]) -> list[Any]:
    """Build RuleModel rows for the default categorization and matching rules."""
    from kash.db.models import RuleModel
    from kash.services.rules.default_rules import (
        DEFAULT_MATCHING_RULES,
        UNIVERSAL_DEFAULT_RULES,
    )

    rows: list[Any] = []
    for rule_dict in UNIVERSAL_DEFAULT_RULES:
        actions = []
        for act in rule_dict.get("actions", []):
            if act["op"] == "set_category_named":
                target_cat_id = category_name_map.get(act["value"])
                if target_cat_id:
                    actions.append({"op": "set_category", "value": str(target_cat_id)})
            else:
                actions.append(act)

        rows.append(
            RuleModel(
                id=uuid.uuid4(),
                user_id=user_id,
                name=rule_dict["name"],
                priority=rule_dict.get("priority", 100),
                is_active=True,
                conditions_op=rule_dict.get("conditions_op", "and"),
                conditions=rule_dict.get("conditions", []),
                actions=actions,
            )
        )

    for mr in DEFAULT_MATCHING_RULES:
        rows.append(
            RuleModel(
                id=uuid.uuid4(),
                user_id=user_id,
                name=mr["name"],
                priority=mr["priority"],
                is_active=True,
                rule_type=mr["rule_type"],
                action_mode=mr["action_mode"],
                config=mr["config"],
                conditions_op="and",
                conditions=[],
                actions=[],
            )
        )
    return rows


async def seed_initial_defaults(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    user_id: uuid.UUID = OWNER_USER_ID,
) -> None:
    """Seed the owner user, default categories, and default rules if the database is fresh."""
    from sqlalchemy import select

    from kash.db.models import (
        CategoryGroupModel,
        CategoryModel,
        RuleModel,
        UserModel,
        UserSettingsModel,
    )
    from kash.services.rules.default_rules import (
        DEFAULT_CATEGORY_GROUPS,
        DEFAULT_TRANSFER_CATEGORY_NAMES,
    )

    factory = session_factory or get_session_factory()
    async with factory() as session:
        # 1. Ensure the owner user exists
        user = await session.get(UserModel, user_id)
        if not user:
            session.add(
                UserModel(
                    id=user_id,
                    email="owner@kash.local",
                    hashed_password="!",  # no login yet; "!" can never match a hash
                    is_active=True,
                )
            )
            session.add(
                UserSettingsModel(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    theme="light",
                    currency_code="USD",
                    currency_symbol="$",
                    locale="en_US",
                )
            )
            await session.flush()

        # 2. Default categories
        cat_res = await session.execute(
            select(CategoryModel).where(CategoryModel.user_id == user_id)
        )
        existing_cats = cat_res.scalars().all()
        category_name_map: dict[str, uuid.UUID] = {c.name: c.id for c in existing_cats}

        if not existing_cats:
            for grp_data in DEFAULT_CATEGORY_GROUPS:
                grp_id = uuid.uuid4()
                session.add(
                    CategoryGroupModel(
                        id=grp_id,
                        user_id=user_id,
                        name=grp_data["name"],
                        icon="folder",
                        color="#6366f1",
                        sort_order=grp_data["sort_order"],
                    )
                )
                for cat_data in grp_data.get("categories", []):
                    cat_id = uuid.uuid4()
                    session.add(
                        CategoryModel(
                            id=cat_id,
                            user_id=user_id,
                            group_id=grp_id,
                            name=cat_data["name"],
                            icon=cat_data.get("icon", "tag"),
                            color=cat_data.get("color", "#6366f1"),
                            sort_order=cat_data["sort_order"],
                            is_hidden=False,
                            is_transfer=cat_data.get("is_transfer", False),
                        )
                    )
                    category_name_map[cat_data["name"]] = cat_id
            await session.flush()
        elif not any(c.is_transfer for c in existing_cats):
            # Older installs seeded the transfer categories without the "treat as
            # transfer" flag, so transfers counted as income. Flag them once, but
            # only if the user has not flagged any category themselves.
            for cat in existing_cats:
                if cat.name in DEFAULT_TRANSFER_CATEGORY_NAMES:
                    cat.is_transfer = True

        # 3. Default rules (only on a fresh install)
        rules_res = await session.execute(
            select(RuleModel.id).where(RuleModel.user_id == user_id).limit(1)
        )
        if rules_res.first() is None and category_name_map:
            session.add_all(_default_rule_models(user_id, category_name_map))

        await session.commit()


async def _add_missing_default_categories(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Add default categories introduced after the install was seeded.

    Called when rules are reset, so new default rules have their categories.
    Existing categories (matched by name) are left alone.
    """
    from sqlalchemy import select

    from kash.db.models import CategoryGroupModel, CategoryModel
    from kash.services.rules.default_rules import DEFAULT_CATEGORY_GROUPS

    cat_names = set(
        (await session.execute(select(CategoryModel.name).where(CategoryModel.user_id == user_id)))
        .scalars()
        .all()
    )
    groups = {
        g.name: g
        for g in (
            await session.execute(
                select(CategoryGroupModel).where(CategoryGroupModel.user_id == user_id)
            )
        )
        .scalars()
        .all()
    }
    for grp_data in DEFAULT_CATEGORY_GROUPS:
        missing = [c for c in grp_data.get("categories", []) if c["name"] not in cat_names]
        if not missing:
            continue
        group = groups.get(grp_data["name"])
        if group is None:
            group = CategoryGroupModel(
                id=uuid.uuid4(),
                user_id=user_id,
                name=grp_data["name"],
                icon="folder",
                color="#6366f1",
                sort_order=grp_data["sort_order"],
            )
            session.add(group)
            groups[group.name] = group
        for cat_data in missing:
            session.add(
                CategoryModel(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    group_id=group.id,
                    name=cat_data["name"],
                    icon=cat_data.get("icon", "tag"),
                    color=cat_data.get("color", "#6366f1"),
                    sort_order=cat_data["sort_order"],
                    is_hidden=False,
                    is_transfer=cat_data.get("is_transfer", False),
                )
            )
    await session.flush()


async def reset_rules_to_defaults(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    user_id: uuid.UUID = OWNER_USER_ID,
) -> int:
    """Replace a user's rules with the default rule set. Returns the new rule count."""
    from sqlalchemy import delete, select

    from kash.db.models import CategoryModel, RuleModel

    factory = session_factory or get_session_factory()
    async with factory() as session:
        await _add_missing_default_categories(session, user_id)
        cat_res = await session.execute(
            select(CategoryModel.name, CategoryModel.id).where(CategoryModel.user_id == user_id)
        )
        category_name_map = {name: cat_id for name, cat_id in cat_res.all()}  # noqa: C416

        await session.execute(delete(RuleModel).where(RuleModel.user_id == user_id))
        rows = _default_rule_models(user_id, category_name_map)
        session.add_all(rows)
        await session.commit()
        return len(rows)
