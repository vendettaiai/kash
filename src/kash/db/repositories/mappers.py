"""Mappers between SQLAlchemy ORM models and Pydantic domain models."""

from uuid import UUID

from kash.db.models import (
    AccountModel,
    BankConnectionModel,
    BillDetectionCandidateModel,
    BillModel,
    BillOccurrenceModel,
    BudgetModel,
    CategoryGroupModel,
    CategoryModel,
    GoalModel,
    RuleModel,
    TransactionModel,
    TransactionSplitModel,
)
from kash.domain.models import (
    Account,
    AccountCashFlow,
    AccountType,
    BankConnection,
    BankConnectionStatus,
    Bill,
    BillDetectionCandidate,
    BillFrequency,
    BillOccurrence,
    BillOccurrenceStatus,
    BillStatus,
    Budget,
    CandidateStatus,
    Category,
    CategoryGroup,
    Goal,
    PendingAccount,
    Rule,
    Transaction,
    TransactionSource,
    TransactionSplit,
    TransactionType,
)


def account_to_domain(m: AccountModel) -> Account:
    return Account(
        id=m.id,
        user_id=m.user_id,
        connection_id=m.connection_id,
        name=m.name,
        account_type=AccountType(m.account_type),
        currency=m.currency,
        current_balance=m.current_balance,
        available_balance=m.available_balance,
        mask=m.mask,
        is_active=m.is_active,
        include_in_net_worth=m.include_in_net_worth,
        show_in_balance=m.show_in_balance,
        cash_flow=AccountCashFlow(m.cash_flow),
        external_id=m.external_id,
        last_reconciled_at=m.last_reconciled_at,
        last_reconciled_balance=m.last_reconciled_balance,
        balance_as_of=m.balance_as_of,
        provider_account_name=m.provider_account_name,
        institution_id=m.institution_id,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def category_group_to_domain(m: CategoryGroupModel) -> CategoryGroup:
    return CategoryGroup(
        id=m.id,
        user_id=m.user_id,
        name=m.name,
        icon=getattr(m, "icon", "folder"),
        color=getattr(m, "color", "#64748b"),
        sort_order=m.sort_order,
        created_at=m.created_at,
    )


def category_to_domain(m: CategoryModel) -> Category:
    return Category(
        id=m.id,
        user_id=m.user_id,
        group_id=m.group_id,
        name=m.name,
        icon=m.icon,
        color=m.color,
        is_system=m.is_system,
        is_hidden=m.is_hidden,
        is_transfer=getattr(m, "is_transfer", False),
        sort_order=m.sort_order,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def _transaction_source(value: str) -> TransactionSource:
    """Map a stored source string, treating unknown legacy values as manual."""
    try:
        return TransactionSource(value)
    except ValueError:
        return TransactionSource.MANUAL


def transaction_to_domain(m: TransactionModel) -> Transaction:
    return Transaction(
        id=m.id,
        user_id=m.user_id,
        account_id=m.account_id,
        category_id=m.category_id,
        date=m.date,
        amount=m.amount,
        type=TransactionType(m.type),
        original_description=m.original_description,
        clean_description=m.clean_description,
        merchant_name=m.merchant_name,
        notes=m.notes,
        is_pending=m.is_pending,
        is_transfer=m.is_transfer,
        matched_transfer_id=m.matched_transfer_id,
        is_ignored=m.is_ignored,
        source=_transaction_source(m.source),
        external_id=m.external_id,
        fingerprint=m.fingerprint,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def transaction_split_to_domain(m: TransactionSplitModel) -> TransactionSplit:
    return TransactionSplit(
        id=m.id,
        transaction_id=m.transaction_id,
        category_id=m.category_id,
        amount=m.amount,
        notes=m.notes,
        created_at=m.created_at,
    )


def rule_to_domain(m: RuleModel) -> Rule:
    return Rule(
        id=m.id,
        user_id=m.user_id,
        name=m.name,
        priority=m.priority,
        is_active=m.is_active,
        rule_type=getattr(m, "rule_type", "categorization"),
        action_mode=getattr(m, "action_mode", "links"),
        config=getattr(m, "config", {}) or {},
        conditions_op=m.conditions_op,
        conditions=m.conditions,
        actions=m.actions,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def bill_to_domain(m: BillModel) -> Bill:
    return Bill(
        id=m.id,
        user_id=m.user_id,
        account_id=m.account_id,
        category_id=m.category_id,
        name=m.name,
        merchant_name=m.merchant_name,
        expected_amount=m.expected_amount,
        is_variable_amount=m.is_variable_amount,
        frequency=BillFrequency(m.frequency),
        due_day=m.due_day,
        next_due_date=m.next_due_date,
        status=BillStatus(m.status),
        detected_automatically=m.detected_automatically,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def bill_occurrence_to_domain(m: BillOccurrenceModel) -> BillOccurrence:
    return BillOccurrence(
        id=m.id,
        bill_id=m.bill_id,
        due_date=m.due_date,
        expected_amount=m.expected_amount,
        status=BillOccurrenceStatus(m.status),
        matched_transaction_id=m.matched_transaction_id,
        paid_at=m.paid_at,
        paid_amount=m.paid_amount,
        created_at=m.created_at,
    )


def candidate_to_domain(m: BillDetectionCandidateModel) -> BillDetectionCandidate:
    return BillDetectionCandidate(
        id=m.id,
        user_id=m.user_id,
        merchant_name=m.merchant_name,
        normalized_merchant=m.normalized_merchant,
        detected_frequency=BillFrequency(m.detected_frequency),
        average_amount=m.average_amount,
        last_amount=m.last_amount,
        is_variable=m.is_variable,
        confidence_score=m.confidence_score,
        evidence_transaction_ids=[
            UUID(tid) if isinstance(tid, str) else tid for tid in m.evidence_transaction_ids
        ],
        status=CandidateStatus(m.status),
        suggested_category_id=m.suggested_category_id,
        suggested_account_id=m.suggested_account_id,
        next_estimated_date=m.next_estimated_date,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def budget_to_domain(m: BudgetModel) -> Budget:
    return Budget(
        id=m.id,
        user_id=m.user_id,
        category_id=m.category_id,
        period=m.period,
        budgeted_amount=m.budgeted_amount,
        enable_rollover=m.enable_rollover,
        rollover_balance=m.rollover_balance,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def goal_to_domain(m: GoalModel) -> Goal:
    return Goal(
        id=m.id,
        user_id=m.user_id,
        name=m.name,
        target_amount=m.target_amount,
        current_amount=m.current_amount,
        target_date=m.target_date,
        icon=m.icon,
        color=m.color,
        notes=m.notes,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def bank_connection_to_domain(m: BankConnectionModel) -> BankConnection:
    return BankConnection(
        id=m.id,
        user_id=m.user_id,
        provider_name=m.provider_name,
        institution_name=m.institution_name,
        encrypted_credentials=m.encrypted_credentials,
        status=BankConnectionStatus(m.status),
        last_synced_at=m.last_synced_at,
        last_error_message=m.last_error_message,
        pending_accounts=[PendingAccount.model_validate(p) for p in m.pending_accounts or []],
        retired_account_ids=[str(i) for i in m.retired_account_ids or []],
        created_at=m.created_at,
        updated_at=m.updated_at,
    )
