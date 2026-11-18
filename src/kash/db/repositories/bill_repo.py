"""Database Bill, Occurrence, and Detection Candidate repository."""

from datetime import date, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.models import (
    BillDetectionCandidateModel,
    BillDismissalModel,
    BillModel,
    BillOccurrenceModel,
)
from kash.db.repositories.mappers import (
    bill_occurrence_to_domain,
    bill_to_domain,
    candidate_to_domain,
)
from kash.domain.models import (
    Bill,
    BillDetectionCandidate,
    BillOccurrence,
    BillOccurrenceStatus,
    BillStatus,
    CandidateStatus,
)


class SqlBillRepository:
    """SQL-backed implementation of BillRepository protocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def list_bills(self, user_id: UUID, status: BillStatus | None = None) -> list[Bill]:
        async with self._session_factory() as session:
            stmt = select(BillModel).where(BillModel.user_id == user_id)
            if status is not None:
                stmt = stmt.where(BillModel.status == status.value)
            stmt = stmt.order_by(BillModel.next_due_date.asc())

            result = await session.execute(stmt)
            return [bill_to_domain(row) for row in result.scalars().all()]

    async def get_bill(self, bill_id: UUID) -> Bill | None:
        async with self._session_factory() as session:
            stmt = select(BillModel).where(BillModel.id == bill_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return bill_to_domain(row) if row else None

    async def create_bill(self, bill: Bill) -> Bill:
        async with self._session_factory() as session:
            model = BillModel(
                id=bill.id,
                user_id=bill.user_id,
                account_id=bill.account_id,
                category_id=bill.category_id,
                name=bill.name,
                merchant_name=bill.merchant_name,
                expected_amount=bill.expected_amount,
                is_variable_amount=bill.is_variable_amount,
                frequency=bill.frequency.value,
                due_day=bill.due_day,
                next_due_date=bill.next_due_date,
                status=bill.status.value,
                detected_automatically=bill.detected_automatically,
            )
            session.add(model)

            # Automatically create the next upcoming occurrence
            occurrence = BillOccurrenceModel(
                id=uuid4(),
                bill_id=bill.id,
                due_date=bill.next_due_date,
                expected_amount=bill.expected_amount,
                status=BillOccurrenceStatus.UPCOMING.value,
            )
            session.add(occurrence)

            await session.commit()
            await session.refresh(model)
            return bill_to_domain(model)

    async def update_bill(self, bill: Bill) -> Bill:
        async with self._session_factory() as session:
            model = await session.get(BillModel, bill.id)
            if not model:
                raise ValueError(f"Bill {bill.id} not found")

            model.account_id = bill.account_id
            model.category_id = bill.category_id
            model.name = bill.name
            model.merchant_name = bill.merchant_name
            model.expected_amount = bill.expected_amount
            model.is_variable_amount = bill.is_variable_amount
            model.frequency = bill.frequency.value
            model.due_day = bill.due_day
            model.next_due_date = bill.next_due_date
            model.status = bill.status.value

            await session.commit()
            await session.refresh(model)
            return bill_to_domain(model)

    async def delete_bill(self, bill_id: UUID) -> bool:
        async with self._session_factory() as session:
            stmt = delete(BillModel).where(BillModel.id == bill_id)
            result = await session.execute(stmt)
            await session.commit()
            return bool(result.rowcount and result.rowcount > 0)

    async def list_upcoming_occurrences(
        self, user_id: UUID, days_ahead: int = 14
    ) -> list[tuple[Bill, BillOccurrence]]:
        cutoff_date = date.today() + timedelta(days=days_ahead)
        async with self._session_factory() as session:
            stmt = (
                select(BillModel, BillOccurrenceModel)
                .join(BillOccurrenceModel, BillModel.id == BillOccurrenceModel.bill_id)
                .where(
                    BillModel.user_id == user_id,
                    BillModel.status == BillStatus.ACTIVE.value,
                    BillOccurrenceModel.status == BillOccurrenceStatus.UPCOMING.value,
                    BillOccurrenceModel.due_date <= cutoff_date,
                )
                .order_by(BillOccurrenceModel.due_date.asc())
            )
            result = await session.execute(stmt)
            rows = result.all()
            return [
                (bill_to_domain(bill_model), bill_occurrence_to_domain(occ_model))
                for bill_model, occ_model in rows
            ]

    async def list_detection_candidates(
        self,
        user_id: UUID,
        status: CandidateStatus = CandidateStatus.PENDING_REVIEW,
    ) -> list[BillDetectionCandidate]:
        async with self._session_factory() as session:
            stmt = (
                select(BillDetectionCandidateModel)
                .where(
                    BillDetectionCandidateModel.user_id == user_id,
                    BillDetectionCandidateModel.status == status.value,
                )
                .order_by(BillDetectionCandidateModel.confidence_score.desc())
            )
            result = await session.execute(stmt)
            return [candidate_to_domain(row) for row in result.scalars().all()]

    async def update_candidate_status(self, candidate_id: UUID, status: CandidateStatus) -> bool:
        async with self._session_factory() as session:
            model = await session.get(BillDetectionCandidateModel, candidate_id)
            if not model:
                return False
            model.status = status.value
            await session.commit()
            return True

    async def get_candidate(self, candidate_id: UUID) -> BillDetectionCandidate | None:
        async with self._session_factory() as session:
            model = await session.get(BillDetectionCandidateModel, candidate_id)
            return candidate_to_domain(model) if model else None

    async def save_candidate(self, candidate: BillDetectionCandidate) -> BillDetectionCandidate:
        """Insert or update a detection candidate."""
        async with self._session_factory() as session:
            model = await session.get(BillDetectionCandidateModel, candidate.id)
            if model is None:
                model = BillDetectionCandidateModel(id=candidate.id, user_id=candidate.user_id)
                session.add(model)
            model.merchant_name = candidate.merchant_name
            model.normalized_merchant = candidate.normalized_merchant
            model.detected_frequency = candidate.detected_frequency.value
            model.average_amount = candidate.average_amount
            model.last_amount = candidate.last_amount
            model.is_variable = candidate.is_variable
            model.confidence_score = candidate.confidence_score
            model.evidence_transaction_ids = [str(t) for t in candidate.evidence_transaction_ids]
            model.status = candidate.status.value
            model.suggested_category_id = candidate.suggested_category_id
            model.suggested_account_id = candidate.suggested_account_id
            model.next_estimated_date = candidate.next_estimated_date
            await session.commit()
            await session.refresh(model)
            return candidate_to_domain(model)

    async def delete_candidate(self, candidate_id: UUID) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(BillDetectionCandidateModel).where(
                    BillDetectionCandidateModel.id == candidate_id
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def list_dismissed_merchants(self, user_id: UUID) -> set[str]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(BillDismissalModel.normalized_merchant).where(
                    BillDismissalModel.user_id == user_id
                )
            )
            return set(result.scalars().all())

    async def add_dismissal(self, user_id: UUID, normalized_merchant: str) -> None:
        async with self._session_factory() as session:
            exists = await session.execute(
                select(BillDismissalModel.id).where(
                    BillDismissalModel.user_id == user_id,
                    BillDismissalModel.normalized_merchant == normalized_merchant,
                )
            )
            if exists.first() is None:
                session.add(
                    BillDismissalModel(
                        id=uuid4(), user_id=user_id, normalized_merchant=normalized_merchant
                    )
                )
                await session.commit()

    async def remove_dismissal(self, user_id: UUID, normalized_merchant: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(BillDismissalModel).where(
                    BillDismissalModel.user_id == user_id,
                    BillDismissalModel.normalized_merchant == normalized_merchant,
                )
            )
            await session.commit()

    async def list_occurrences(self, bill_id: UUID) -> list[BillOccurrence]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(BillOccurrenceModel)
                .where(BillOccurrenceModel.bill_id == bill_id)
                .order_by(BillOccurrenceModel.due_date.asc())
            )
            return [bill_occurrence_to_domain(row) for row in result.scalars().all()]

    async def get_occurrence(self, occurrence_id: UUID) -> BillOccurrence | None:
        async with self._session_factory() as session:
            model = await session.get(BillOccurrenceModel, occurrence_id)
            return bill_occurrence_to_domain(model) if model else None

    async def save_occurrence(self, occurrence: BillOccurrence) -> BillOccurrence:
        """Insert or update one due date of a bill."""
        async with self._session_factory() as session:
            model = await session.get(BillOccurrenceModel, occurrence.id)
            if model is None:
                model = BillOccurrenceModel(id=occurrence.id, bill_id=occurrence.bill_id)
                session.add(model)
            model.due_date = occurrence.due_date
            model.expected_amount = occurrence.expected_amount
            model.status = occurrence.status.value
            model.matched_transaction_id = occurrence.matched_transaction_id
            model.paid_at = occurrence.paid_at
            model.paid_amount = occurrence.paid_amount
            await session.commit()
            await session.refresh(model)
            return bill_occurrence_to_domain(model)
