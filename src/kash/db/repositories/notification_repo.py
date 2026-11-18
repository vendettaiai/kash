"""Database repository for the devices that accept push notifications."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import NotificationSentModel, PushSubscriptionModel
from kash.domain.models import PushSubscription


def _to_domain(m: PushSubscriptionModel) -> PushSubscription:
    return PushSubscription(
        id=m.id, user_id=m.user_id, endpoint=m.endpoint, p256dh=m.p256dh, auth=m.auth
    )


class SqlNotificationRepository:
    """SQL-backed implementation of NotificationRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def _update_subscription(
        self, session: AsyncSession, subscription: PushSubscription
    ) -> bool:
        result = await session.execute(
            select(PushSubscriptionModel).where(
                PushSubscriptionModel.endpoint == subscription.endpoint
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            return False
        model.user_id = subscription.user_id
        model.p256dh = subscription.p256dh
        model.auth = subscription.auth
        await session.commit()
        return True

    async def save_subscription(self, subscription: PushSubscription) -> None:
        async with self._session_factory() as session:
            if await self._update_subscription(session, subscription):
                return
            session.add(
                PushSubscriptionModel(
                    id=subscription.id,
                    user_id=subscription.user_id,
                    endpoint=subscription.endpoint,
                    p256dh=subscription.p256dh,
                    auth=subscription.auth,
                )
            )
            try:
                await session.commit()
            except IntegrityError:  # sent twice at once: the other request added it
                await session.rollback()
                await self._update_subscription(session, subscription)

    async def delete_subscription(self, user_id: UUID, endpoint: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(PushSubscriptionModel).where(
                    PushSubscriptionModel.user_id == user_id,
                    PushSubscriptionModel.endpoint == endpoint,
                )
            )
            await session.commit()

    async def delete_endpoint(self, endpoint: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(PushSubscriptionModel).where(PushSubscriptionModel.endpoint == endpoint)
            )
            await session.commit()

    async def delete_all_subscriptions(self) -> None:
        async with self._session_factory() as session:
            await session.execute(delete(PushSubscriptionModel))
            await session.commit()

    async def list_subscriptions(self, user_id: UUID) -> list[PushSubscription]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PushSubscriptionModel)
                .where(PushSubscriptionModel.user_id == user_id)
                .order_by(PushSubscriptionModel.created_at)
            )
            return [_to_domain(m) for m in result.scalars()]

    async def list_all_subscriptions(self) -> list[PushSubscription]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PushSubscriptionModel).order_by(PushSubscriptionModel.created_at)
            )
            return [_to_domain(m) for m in result.scalars()]

    async def list_claimed(self, user_id: UUID, kind: str) -> list[str]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(NotificationSentModel.dedupe_key).where(
                    NotificationSentModel.user_id == user_id, NotificationSentModel.kind == kind
                )
            )
            return list(result.scalars())

    async def claim(self, user_id: UUID, kind: str, keys: Sequence[str]) -> list[str]:
        seen = set(await self.list_claimed(user_id, kind))
        claimed = []
        # One commit each, so one key someone else just took doesn't lose the others.
        for key in dict.fromkeys(keys):
            if key in seen:
                continue
            async with self._session_factory() as session:
                session.add(
                    NotificationSentModel(id=uuid4(), user_id=user_id, kind=kind, dedupe_key=key)
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    continue
            claimed.append(key)
        return claimed

    async def release(self, user_id: UUID, kind: str, keys: Sequence[str]) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(NotificationSentModel).where(
                    NotificationSentModel.user_id == user_id,
                    NotificationSentModel.kind == kind,
                    NotificationSentModel.dedupe_key.in_(keys),
                )
            )
            await session.commit()

    async def prune_sent(self, before: datetime) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(NotificationSentModel).where(NotificationSentModel.sent_at < before)
            )
            await session.commit()
