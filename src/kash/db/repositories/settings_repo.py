"""Database repository for the owner's display settings (``user_settings``)."""

from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import UserSettingsModel
from kash.domain.models import UserSettings


def _to_domain(m: UserSettingsModel) -> UserSettings:
    return UserSettings(
        user_id=m.user_id,
        theme=m.theme,
        currency_code=m.currency_code,
        currency_symbol=m.currency_symbol,
        locale=m.locale,
        font_scale=m.font_scale,
        sync_enabled=m.sync_enabled,
        sync_interval_hours=m.sync_interval_hours,
        sync_timezone=m.sync_timezone,
        sync_mode=m.sync_mode,
        sync_cron=m.sync_cron,
        ai_enabled=m.ai_enabled,
        ai_provider=m.ai_provider,
        ai_base_url=m.ai_base_url,
        ai_model=m.ai_model,
        ai_api_key_encrypted=m.ai_api_key_encrypted,
        ai_auto_apply_threshold=m.ai_auto_apply_threshold,
        ai_monthly_limit=m.ai_monthly_limit,
        ai_run_after_sync=m.ai_run_after_sync,
        ai_options=m.ai_options or {},
        # Older rows hold a list of widget names from before customizing existed.
        dashboard_layout=m.dashboard_widgets if isinstance(m.dashboard_widgets, dict) else {},
    )


class SqlSettingsRepository:
    """SQL-backed implementation of SettingsRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def _model(self, session: AsyncSession, user_id: UUID) -> UserSettingsModel | None:
        result = await session.execute(
            select(UserSettingsModel).where(UserSettingsModel.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_settings(self, user_id: UUID) -> UserSettings:
        """Saved settings, or the defaults if there is no row yet."""
        async with self._session_factory() as session:
            model = await self._model(session, user_id)
            return _to_domain(model) if model else UserSettings(user_id=user_id)

    async def save_settings(self, settings: UserSettings) -> UserSettings:
        async with self._session_factory() as session:
            model = await self._model(session, settings.user_id)
            if model is None:
                model = UserSettingsModel(id=uuid4(), user_id=settings.user_id)
                session.add(model)
            model.theme = settings.theme
            model.currency_code = settings.currency_code
            model.currency_symbol = settings.currency_symbol
            model.locale = settings.locale
            model.font_scale = settings.font_scale
            model.sync_enabled = settings.sync_enabled
            model.sync_interval_hours = settings.sync_interval_hours
            model.sync_timezone = settings.sync_timezone
            model.sync_mode = settings.sync_mode
            model.sync_cron = settings.sync_cron
            model.ai_enabled = settings.ai_enabled
            model.ai_provider = settings.ai_provider
            model.ai_base_url = settings.ai_base_url
            model.ai_model = settings.ai_model
            model.ai_api_key_encrypted = settings.ai_api_key_encrypted
            model.ai_auto_apply_threshold = settings.ai_auto_apply_threshold
            model.ai_monthly_limit = settings.ai_monthly_limit
            model.ai_run_after_sync = settings.ai_run_after_sync
            model.ai_options = settings.ai_options or None
            if settings.dashboard_layout or isinstance(model.dashboard_widgets, dict):
                model.dashboard_widgets = settings.dashboard_layout
            await session.commit()
            return _to_domain(model)

    # The push keys stay off UserSettings: they are never shown or edited with the settings.

    async def get_vapid_keys(self, user_id: UUID) -> tuple[str, str] | None:
        async with self._session_factory() as session:
            model = await self._model(session, user_id)
            if model is None or not model.vapid_public_key:
                return None
            return model.vapid_public_key, model.vapid_private_key_encrypted or ""

    async def claim_vapid_keys(
        self, user_id: UUID, public_key: str, private_key_encrypted: str
    ) -> tuple[str, str]:
        keys = {
            "vapid_public_key": public_key,
            "vapid_private_key_encrypted": private_key_encrypted,
        }
        async with self._session_factory() as session:
            if await self._model(session, user_id) is None:
                session.add(UserSettingsModel(id=uuid4(), user_id=user_id, **keys))
            else:
                await session.execute(
                    update(UserSettingsModel)
                    .where(
                        UserSettingsModel.user_id == user_id,
                        UserSettingsModel.vapid_public_key.is_(None),
                    )
                    .values(**keys)
                )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()  # another request made the row first
        return await self.get_vapid_keys(user_id) or (public_key, private_key_encrypted)

    async def clear_vapid_keys(self, user_id: UUID) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(UserSettingsModel)
                .where(UserSettingsModel.user_id == user_id)
                .values(vapid_public_key=None, vapid_private_key_encrypted=None)
            )
            await session.commit()
