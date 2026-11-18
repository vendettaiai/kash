"""SQLAlchemy declarative models for Kash.

Maps 1-to-1 with docs/DATA_MODEL.md specifications using strict Decimal / NUMERIC(18, 4),
UUID v4 primary keys, and comprehensive relationship cascades.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kash.db.base import Base, JsonType, TimestampMixin


class UserModel(Base, TimestampMixin):
    """A login. The owner row (OWNER_USER_ID) holds the household's data; other rows are
    people the owner invited, who log in to the same data (0016)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 0004
    # Per-person look (0016); empty = the household's setting in user_settings.
    theme: Mapped[str | None] = mapped_column(String(10), nullable=True)
    font_scale: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Which alerts this person gets (0021, services/notifications/prefs.py); empty = defaults
    notification_prefs: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    # One-time join / password link (0016): SHA-256 of the token, never the token itself.
    invite_token_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    invite_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    settings: Mapped[UserSettingsModel | None] = relationship(
        "UserSettingsModel", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    bank_connections: Mapped[list[BankConnectionModel]] = relationship(
        "BankConnectionModel", back_populates="user", cascade="all, delete-orphan"
    )
    accounts: Mapped[list[AccountModel]] = relationship(
        "AccountModel", back_populates="user", cascade="all, delete-orphan"
    )
    category_groups: Mapped[list[CategoryGroupModel]] = relationship(
        "CategoryGroupModel", back_populates="user", cascade="all, delete-orphan"
    )
    categories: Mapped[list[CategoryModel]] = relationship(
        "CategoryModel", back_populates="user", cascade="all, delete-orphan"
    )
    transactions: Mapped[list[TransactionModel]] = relationship(
        "TransactionModel", back_populates="user", cascade="all, delete-orphan"
    )
    rules: Mapped[list[RuleModel]] = relationship(
        "RuleModel", back_populates="user", cascade="all, delete-orphan"
    )
    bills: Mapped[list[BillModel]] = relationship(
        "BillModel", back_populates="user", cascade="all, delete-orphan"
    )
    bill_detection_candidates: Mapped[list[BillDetectionCandidateModel]] = relationship(
        "BillDetectionCandidateModel", back_populates="user", cascade="all, delete-orphan"
    )
    bill_dismissals: Mapped[list[BillDismissalModel]] = relationship(
        "BillDismissalModel", back_populates="user", cascade="all, delete-orphan"
    )
    recurring_transactions: Mapped[list[RecurringTransactionModel]] = relationship(
        "RecurringTransactionModel", back_populates="user", cascade="all, delete-orphan"
    )
    budgets: Mapped[list[BudgetModel]] = relationship(
        "BudgetModel", back_populates="user", cascade="all, delete-orphan"
    )
    goals: Mapped[list[GoalModel]] = relationship(
        "GoalModel", back_populates="user", cascade="all, delete-orphan"
    )


class UserSettingsModel(Base, TimestampMixin):
    """User preferences and interface configuration."""

    __tablename__ = "user_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    theme: Mapped[str] = mapped_column(String(50), default="light", nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    currency_symbol: Mapped[str] = mapped_column(String(5), default="$", nullable=False)
    locale: Mapped[str] = mapped_column(String(20), default="en_US", nullable=False)
    # Text size in % (0012)
    font_scale: Mapped[int] = mapped_column(
        Integer, default=100, server_default="100", nullable=False
    )
    # Automatic bank sync (0003). NULL = use SYNC_SCHEDULE_HOURS / SYNC_TIMEZONE.
    sync_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sync_interval_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sync_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Custom schedule (0018): mode "cron" uses sync_cron instead of the interval.
    sync_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sync_cron: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Optional AI categorization (0005, docs/AI_PLAN.md). Off unless enabled.
    ai_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    ai_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ai_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_auto_apply_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_monthly_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Categorize new transactions after each bank sync (0007)
    ai_run_after_sync: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    # Advanced AI settings and dismissed rule proposals (0008, services/ai/options.py)
    ai_options: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    # The household's push notification keys (0020); the private one is encrypted
    vapid_public_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vapid_private_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Dashboard layout per device (services/dashboard_layout.py); a list = defaults
    dashboard_widgets: Mapped[Any] = mapped_column(
        JsonType,
        default=lambda: [
            "net_worth",
            "cash_flow",
            "budget_summary",
            "upcoming_bills",
            "recent_transactions",
        ],
        nullable=False,
    )

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="settings")


class PushSubscriptionModel(Base, TimestampMixin):
    """One device that accepts push notifications, for the person who turned them on (0020)."""

    __tablename__ = "push_subscriptions"
    __table_args__ = (Index("idx_push_subscriptions_user", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The login, not the household: each person gets their own alerts (ADR-021).
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False)
    p256dh: Mapped[str] = mapped_column(String(128), nullable=False)
    auth: Mapped[str] = mapped_column(String(64), nullable=False)


class NotificationSentModel(Base):
    """What a person was already told about (0021), so a restart or a second process can't
    repeat it. Also holds the mark that the day's check ran (kind "daily", key the date)."""

    __tablename__ = "notifications_sent"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "dedupe_key", name="uq_notification_sent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(120), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BankConnectionModel(Base, TimestampMixin):
    """External bank synchronization provider link (e.g. SimpleFIN Bridge)."""

    __tablename__ = "bank_connections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_name: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. 'simplefin'
    institution_name: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), default="active", nullable=False
    )  # 'active', 'reauth_required', 'error', 'paused'
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # New accounts waiting for the user: new, or replacing one? (0017;
    # services/bank/identity.py). NULL = none.
    pending_accounts: Mapped[list[Any] | None] = mapped_column(JsonType, nullable=True)
    # Provider account ids to ignore: replaced cards the bank may still report (0017)
    retired_account_ids: Mapped[list[Any] | None] = mapped_column(JsonType, nullable=True)

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="bank_connections")
    accounts: Mapped[list[AccountModel]] = relationship("AccountModel", back_populates="connection")


class AccountModel(Base, TimestampMixin):
    """Financial account (checking, savings, credit card, cash, investment, loan)."""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id", name="uq_account_external"),
        Index("idx_accounts_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bank_connections.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # checking, savings, credit_card, cash, investment, loan
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    current_balance: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0.0000"), nullable=False
    )
    available_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    mask: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    include_in_net_worth: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Counted in the dashboard's banking balance (0010); net worth always counts it
    show_in_balance: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    # What counts toward income/spending: all | income | none (0011)
    cash_flow: Mapped[str] = mapped_column(
        String(10), default="all", server_default="all", nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reconciled_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    # When the bank last reported this balance, per SimpleFIN's balance-date (0014)
    balance_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Bank identity, to recognize the account under a new provider id (0017)
    provider_account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    institution_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="accounts")
    connection: Mapped[BankConnectionModel | None] = relationship(
        "BankConnectionModel", back_populates="accounts"
    )
    transactions: Mapped[list[TransactionModel]] = relationship(
        "TransactionModel", back_populates="account", cascade="all, delete-orphan"
    )


class CategoryGroupModel(Base):
    """Logical grouping for categories (e.g. Living Expenses, Discretionary)."""

    __tablename__ = "category_groups"
    __table_args__ = (Index("idx_category_groups_user", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    icon: Mapped[str] = mapped_column(String(50), default="folder", nullable=False)
    color: Mapped[str] = mapped_column(String(20), default="#64748b", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="category_groups")
    categories: Mapped[list[CategoryModel]] = relationship(
        "CategoryModel", back_populates="group", cascade="all, delete-orphan"
    )


class CategoryModel(Base, TimestampMixin):
    """Spending or income classification category."""

    __tablename__ = "categories"
    __table_args__ = (
        Index("idx_categories_user", "user_id"),
        Index("idx_categories_group", "group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("category_groups.id", ondelete="CASCADE"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    icon: Mapped[str] = mapped_column(String(50), default="tag", nullable=False)
    color: Mapped[str] = mapped_column(String(20), default="#64748b", nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_transfer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="categories")
    group: Mapped[CategoryGroupModel | None] = relationship(
        "CategoryGroupModel", back_populates="categories"
    )
    transactions: Mapped[list[TransactionModel]] = relationship(
        "TransactionModel", back_populates="category"
    )
    budgets: Mapped[list[BudgetModel]] = relationship(
        "BudgetModel", back_populates="category", cascade="all, delete-orphan"
    )


class TransactionModel(Base, TimestampMixin):
    """The financial ledger entry."""

    __tablename__ = "transactions"
    __table_args__ = (
        Index("idx_transactions_user_date", "user_id", "date"),
        Index("idx_transactions_account", "account_id"),
        Index("idx_transactions_category", "category_id"),
        Index("idx_transactions_fingerprint", "fingerprint"),
        Index("idx_transactions_external_id", "external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False
    )  # Negative for debits/expenses, positive for credits/income
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # 'debit', 'credit'
    original_description: Mapped[str] = mapped_column(Text, nullable=False)
    clean_description: Mapped[str] = mapped_column(Text, nullable=False)
    merchant_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_transfer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    matched_transfer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_ignored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(
        String(50), default="manual", nullable=False
    )  # 'manual', 'sync', 'csv'
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="transactions")
    account: Mapped[AccountModel] = relationship("AccountModel", back_populates="transactions")
    category: Mapped[CategoryModel | None] = relationship(
        "CategoryModel", back_populates="transactions"
    )
    splits: Mapped[list[TransactionSplitModel]] = relationship(
        "TransactionSplitModel", back_populates="transaction", cascade="all, delete-orphan"
    )
    matched_transfer: Mapped[TransactionModel | None] = relationship(
        "TransactionModel",
        remote_side=[id],
        foreign_keys=[matched_transfer_id],
        post_update=True,
    )
    matched_occurrence: Mapped[BillOccurrenceModel | None] = relationship(
        "BillOccurrenceModel", back_populates="matched_transaction", uselist=False
    )


class TransactionSplitModel(Base):
    """Splits a single transaction across multiple categories."""

    __tablename__ = "transaction_splits"
    __table_args__ = (Index("idx_splits_tx", "transaction_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    transaction: Mapped[TransactionModel] = relationship(
        "TransactionModel", back_populates="splits"
    )
    category: Mapped[CategoryModel | None] = relationship("CategoryModel")


class RuleModel(Base, TimestampMixin):
    """User categorization and normalization rule."""

    __tablename__ = "rules"
    __table_args__ = (Index("idx_rules_user_priority", "user_id", "priority"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rule_type: Mapped[str] = mapped_column(
        String(50), default="categorization", nullable=False
    )  # categorization, transfer, recurring
    action_mode: Mapped[str] = mapped_column(
        String(50), default="links", nullable=False
    )  # links, suggests
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    conditions_op: Mapped[str] = mapped_column(
        String(10), default="and", nullable=False
    )  # 'and', 'or'
    conditions: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list, nullable=False)
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list, nullable=False)

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="rules")


class BillModel(Base, TimestampMixin):
    """Recurring liability or regular bill."""

    __tablename__ = "bills"
    __table_args__ = (Index("idx_bills_user_status", "user_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    merchant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    is_variable_amount: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    frequency: Mapped[str] = mapped_column(
        String(50), default="monthly", nullable=False
    )  # weekly, biweekly, monthly, quarterly, annual
    due_day: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1-31
    next_due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), default="active", nullable=False
    )  # active, paused, archived
    detected_automatically: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="bills")
    account: Mapped[AccountModel | None] = relationship("AccountModel")
    category: Mapped[CategoryModel | None] = relationship("CategoryModel")
    occurrences: Mapped[list[BillOccurrenceModel]] = relationship(
        "BillOccurrenceModel", back_populates="bill", cascade="all, delete-orphan"
    )


class BillOccurrenceModel(Base):
    """Specific scheduled instance of a bill."""

    __tablename__ = "bill_occurrences"
    __table_args__ = (
        Index("idx_bill_occurrences_bill", "bill_id"),
        Index("idx_bill_occurrences_due_date", "due_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bills.id", ondelete="CASCADE"),
        nullable=False,
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), default="upcoming", nullable=False
    )  # upcoming, paid, missed, dismissed
    matched_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    bill: Mapped[BillModel] = relationship("BillModel", back_populates="occurrences")
    matched_transaction: Mapped[TransactionModel | None] = relationship(
        "TransactionModel", back_populates="matched_occurrence"
    )


class BillDetectionCandidateModel(Base, TimestampMixin):
    """Auto-detected recurring pattern waiting for user review."""

    __tablename__ = "bill_detection_candidates"
    __table_args__ = (Index("idx_detection_user_status", "user_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    merchant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_merchant: Mapped[str] = mapped_column(String(255), nullable=False)
    detected_frequency: Mapped[str] = mapped_column(String(50), nullable=False)
    average_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    last_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    is_variable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    evidence_transaction_ids: Mapped[list[str]] = mapped_column(
        JsonType, default=list, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(50), default="pending_review", nullable=False
    )  # pending_review, accepted, dismissed, ignored
    suggested_category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    suggested_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    next_estimated_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="bill_detection_candidates")
    suggested_category: Mapped[CategoryModel | None] = relationship("CategoryModel")
    suggested_account: Mapped[AccountModel | None] = relationship("AccountModel")


class BillDismissalModel(Base):
    """Tracks normalized merchants that the user dismissed to avoid nagging."""

    __tablename__ = "bill_dismissals"
    __table_args__ = (
        UniqueConstraint("user_id", "normalized_merchant", name="uq_user_dismissed_merchant"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    normalized_merchant: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(String(50), default="user_dismissed", nullable=False)
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="bill_dismissals")


class RecurringTransactionModel(Base, TimestampMixin):
    """General recurring income, transfer, or expense rules."""

    __tablename__ = "recurring_transactions"
    __table_args__ = (Index("idx_recurring_user", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # income, expense, transfer
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    frequency: Mapped[str] = mapped_column(String(50), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="recurring_transactions")
    account: Mapped[AccountModel] = relationship("AccountModel")
    category: Mapped[CategoryModel | None] = relationship("CategoryModel")


class BalanceSnapshotModel(Base):
    """An account's balance on one day, saved as Kash sees it (net worth history)."""

    __tablename__ = "balance_snapshots"
    __table_args__ = (
        UniqueConstraint("account_id", "date", name="uq_snapshot_account_date"),
        Index("idx_snapshots_date", "date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)


class AISuggestionModel(Base):
    """What the AI proposed for one transaction, and what happened to it (0006)."""

    __tablename__ = "ai_suggestions"
    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_ai_suggestion_transaction"),
        Index("idx_ai_suggestions_user_status", "user_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    rule_pattern: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AIRunModel(Base):
    """One AI categorization run: activity log and monthly-limit counter (0006)."""

    __tablename__ = "ai_runs"
    __table_args__ = (Index("idx_ai_runs_user_started", "user_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)  # manual | sync
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    items_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transactions_covered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # ok | error | partial
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)


class BudgetModel(Base, TimestampMixin):
    """Monthly budget envelope for a category."""

    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint("user_id", "category_id", "period", name="uq_budget_period_category"),
        Index("idx_budgets_user_period", "user_id", "period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # 'YYYY-MM'
    budgeted_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0.0000"), nullable=False
    )
    enable_rollover: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rollover_balance: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0.0000"), nullable=False
    )

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="budgets")
    category: Mapped[CategoryModel] = relationship("CategoryModel", back_populates="budgets")


class GoalModel(Base, TimestampMixin):
    """Savings goal target."""

    __tablename__ = "goals"
    __table_args__ = (Index("idx_goals_user", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    current_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0.0000"), nullable=False
    )
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    icon: Mapped[str] = mapped_column(String(50), default="target", nullable=False)
    color: Mapped[str] = mapped_column(String(20), default="#059669", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    user: Mapped[UserModel] = relationship("UserModel", back_populates="goals")


class MerchantIconModel(Base):
    """A custom logo the user uploaded for a merchant (0009, services/merchant_icons.py)."""

    __tablename__ = "merchant_icons"
    __table_args__ = (UniqueConstraint("user_id", "merchant", name="uq_merchant_icon_merchant"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    merchant: Mapped[str] = mapped_column(String(80), nullable=False)  # normalized name
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
