from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _utc_now() -> datetime:
    """Return current timezone-aware UTC datetime."""
    return datetime.now(UTC)


class BaseDomainModel(BaseModel):
    """Base domain model with strict configuration."""

    model_config = ConfigDict(
        from_attributes=True,
        validate_assignment=True,
        populate_by_name=True,
    )


# --- Accounts ---


class AccountType(StrEnum):
    CHECKING = "checking"
    SAVINGS = "savings"
    CREDIT_CARD = "credit_card"
    CASH = "cash"
    INVESTMENT = "investment"
    LOAN = "loan"


class AccountCashFlow(StrEnum):
    """What an account adds to income and spending totals (a setting per account)."""

    ALL = "all"  # every transaction; rules and categories decide
    INCOME = "income"  # only money in with an Income-group category
    NONE = "none"  # nothing: the account is only in balances and net worth


def default_cash_flow(account_type: "AccountType") -> AccountCashFlow:
    """New investment accounts stay out of income/spending; everything else counts."""
    return AccountCashFlow.NONE if account_type == AccountType.INVESTMENT else AccountCashFlow.ALL


class Account(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    connection_id: UUID | None = None
    name: str
    account_type: AccountType
    currency: str = "USD"
    current_balance: Decimal = Field(default=Decimal("0.0000"))
    available_balance: Decimal | None = None
    mask: str | None = None
    is_active: bool = True
    include_in_net_worth: bool = True
    show_in_balance: bool = True  # in the dashboard's banking balance (still an asset)
    # What counts toward income/spending; when not given, set from the type
    # (investment: don't count, others: count everything)
    cash_flow: AccountCashFlow = AccountCashFlow.ALL
    external_id: str | None = None
    last_reconciled_at: datetime | None = None
    last_reconciled_balance: Decimal | None = None
    balance_as_of: datetime | None = None  # SimpleFIN "balance-date": when the bank reported it
    # Who the bank says this is (0017), for recognizing it when the provider issues a new
    # id (e.g. after a bank is removed and re-added in SimpleFIN). Set by sync only.
    provider_account_name: str | None = None  # the provider's name, before any rename
    institution_id: str | None = None  # SimpleFIN org id (or domain)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="before")
    @classmethod
    def _default_cash_flow(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("cash_flow") is None and "account_type" in data:
            data = {**data, "cash_flow": default_cash_flow(AccountType(data["account_type"]))}
        return data


class BalanceSnapshot(BaseDomainModel):
    """An account's balance at the end of one day (for net worth history)."""

    account_id: UUID
    date: date
    balance: Decimal


# --- Bank Connections & Synchronization ---


class BankConnectionStatus(StrEnum):
    ACTIVE = "active"
    REAUTH_REQUIRED = "reauth_required"
    ERROR = "error"
    PAUSED = "paused"


class BankConnection(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    provider_name: str  # e.g. 'simplefin'
    institution_name: str
    encrypted_credentials: str
    status: BankConnectionStatus = BankConnectionStatus.ACTIVE
    last_synced_at: datetime | None = None
    last_error_message: str | None = None
    # New accounts the user is asked about (new, or replacing one?); nothing is
    # imported for them until answered (Accounts page)
    pending_accounts: list["PendingAccount"] = Field(default_factory=list)
    # Provider account ids to ignore: replaced cards the bank may still report
    retired_account_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class ExternalAccount(BaseDomainModel):
    external_id: str
    name: str
    account_type: AccountType = AccountType.CHECKING
    currency: str = "USD"
    current_balance: Decimal = Field(default=Decimal("0.0000"))
    available_balance: Decimal | None = None
    mask: str | None = None
    institution_name: str | None = None
    institution_id: str | None = None  # stable id of the bank (SimpleFIN org id or domain)
    balance_as_of: datetime | None = None  # when the bank last reported this balance


class PendingAccount(BaseDomainModel):
    """A new account the user is asked about: is it new, or replacing one they have?"""

    account: ExternalAccount
    suggested_ids: list[UUID]  # existing accounts it most likely replaces, best first


BankConnection.model_rebuild()


# Description of a transaction the bank sent no text for; a later sync replaces it
NO_DESCRIPTION = "No description from bank"


class ExternalTransaction(BaseDomainModel):
    external_id: str
    account_external_id: str
    posted_at: datetime
    transacted_at: datetime | None = None
    amount: Decimal  # Negative for debits/expenses, positive for credits/income
    description: str
    payee: str | None = None
    memo: str | None = None
    is_pending: bool = False


class ExternalAccountSet(BaseDomainModel):
    """Accounts with their balances and transactions, from one provider response."""

    accounts: list[ExternalAccount] = Field(default_factory=list)
    transactions: list[ExternalTransaction] = Field(default_factory=list)
    # Problems the provider reported alongside the data (e.g. one bank needs attention)
    warnings: list[str] = Field(default_factory=list)
    rejected_transactions: int = 0  # unreadable entries left out (no id, bad amount)


class BankSyncResult(BaseDomainModel):
    connection_id: UUID
    trigger: str = "manual"  # manual | scheduled | initial
    synced_accounts_count: int = 0
    new_accounts_count: int = 0
    relinked_accounts_count: int = 0  # existing accounts the provider gave a new id
    received_transactions_count: int = 0
    new_transactions_count: int = 0
    updated_transactions_count: int = 0
    duplicate_transactions_count: int = 0
    skipped_transactions_count: int = 0  # for accounts excluded from sync
    rejected_transactions_count: int = 0
    removed_pending_count: int = 0  # pending ones the bank dropped or re-posted
    applied_rules_count: int = 0
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)  # saved what came, but not all is well

    @property
    def outcome(self) -> str:
        """``ok``, ``partial`` (data saved, with warnings) or ``failed``."""
        if self.errors:
            return "failed"
        return "partial" if self.warnings else "ok"


# --- Categories ---


class CategoryGroup(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    name: str
    icon: str = "folder"
    color: str = "#64748b"
    sort_order: int = 0
    created_at: datetime = Field(default_factory=_utc_now)


class Category(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    group_id: UUID | None = None
    name: str
    icon: str = "tag"  # Lucide icon identifier
    color: str = "#64748b"  # Hex color code
    is_system: bool = False
    is_hidden: bool = False
    is_transfer: bool = False
    sort_order: int = 0
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


# --- Transactions ---


class TransactionType(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class TransactionSource(StrEnum):
    MANUAL = "manual"
    SYNC = "sync"
    CSV = "csv"


class TransactionSplit(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    transaction_id: UUID
    category_id: UUID | None = None
    amount: Decimal
    notes: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)


class Transaction(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    account_id: UUID
    category_id: UUID | None = None
    date: date
    amount: Decimal  # Negative for debits/expenses, positive for credits/income
    type: TransactionType
    original_description: str
    clean_description: str
    merchant_name: str | None = None
    notes: str | None = None
    is_pending: bool = False
    is_transfer: bool = False
    matched_transfer_id: UUID | None = None
    is_ignored: bool = False
    source: TransactionSource = TransactionSource.MANUAL
    external_id: str | None = None
    fingerprint: str | None = None
    splits: list[TransactionSplit] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


# --- Rules ---


class RuleCondition(BaseDomainModel):
    # Supported fields: description, original_description, merchant, amount, account_id, date, type
    field: str
    # Supported ops: contains, not_contains, starts_with, ends_with, equals, not_equals, regex,
    # gt, gte, lt, lte
    op: str
    value: Any


class RuleAction(BaseDomainModel):
    op: str  # 'set_category', 'set_description', 'set_payee', 'append_notes', 'ignore'
    value: str | None = None


class Rule(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    name: str
    priority: int = 0
    is_active: bool = True
    rule_type: Literal["categorization", "transfer", "recurring"] = "categorization"
    action_mode: Literal["links", "suggests"] = "links"
    config: dict[str, Any] = Field(default_factory=dict)
    conditions_op: Literal["and", "or"] = "and"
    conditions: list[RuleCondition | dict[str, Any]] = Field(default_factory=list)
    actions: list[RuleAction] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @property
    def criteria_bullets(self) -> list[str]:
        from kash.services.rules.matching import generate_rule_criteria_summary

        return generate_rule_criteria_summary(self)

    @property
    def condition_summary(self) -> list[Any]:
        """Conditions as short lines for the Rules page (see describe_conditions)."""
        from kash.services.rules.matching import describe_conditions

        return describe_conditions(self)

    @property
    def conditions_data(self) -> list[dict[str, Any]]:
        res = []
        for c in self.conditions:
            if hasattr(c, "model_dump"):
                res.append(c.model_dump())
            elif isinstance(c, dict):
                res.append(c)
            else:
                res.append(
                    {
                        "field": getattr(c, "field", ""),
                        "op": getattr(c, "op", ""),
                        "value": getattr(c, "value", ""),
                    }
                )
        return res

    @property
    def actions_data(self) -> list[dict[str, Any]]:
        res = []
        for a in self.actions:
            if hasattr(a, "model_dump"):
                res.append(a.model_dump())
            elif isinstance(a, dict):
                res.append(a)
            else:
                res.append({"op": getattr(a, "op", ""), "value": getattr(a, "value", "")})
        return res


# --- Bills & Recurring ---


class BillFrequency(StrEnum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    SEMI_MONTHLY = "semi_monthly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class BillStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class BillOccurrenceStatus(StrEnum):
    UPCOMING = "upcoming"
    PAID = "paid"
    MISSED = "missed"
    DISMISSED = "dismissed"


class Bill(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    account_id: UUID | None = None
    category_id: UUID | None = None
    name: str
    merchant_name: str
    expected_amount: Decimal
    is_variable_amount: bool = False
    frequency: BillFrequency = BillFrequency.MONTHLY
    due_day: int | None = None  # 1-31 for monthly bills
    next_due_date: date
    status: BillStatus = BillStatus.ACTIVE
    detected_automatically: bool = False
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class BillOccurrence(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    bill_id: UUID
    due_date: date
    expected_amount: Decimal
    status: BillOccurrenceStatus = BillOccurrenceStatus.UPCOMING
    matched_transaction_id: UUID | None = None
    paid_at: datetime | None = None
    paid_amount: Decimal | None = None
    created_at: datetime = Field(default_factory=_utc_now)


class CandidateStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    IGNORED = "ignored"


class BillDetectionCandidate(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    merchant_name: str
    normalized_merchant: str
    detected_frequency: BillFrequency
    average_amount: Decimal
    last_amount: Decimal
    is_variable: bool = False
    confidence_score: Decimal  # e.g. 95.00
    evidence_transaction_ids: list[UUID] = Field(default_factory=list)
    status: CandidateStatus = CandidateStatus.PENDING_REVIEW
    suggested_category_id: UUID | None = None
    suggested_account_id: UUID | None = None
    next_estimated_date: date
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


# --- Budgets & Goals ---


class Budget(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    category_id: UUID
    period: str  # Format 'YYYY-MM'
    budgeted_amount: Decimal = Field(default=Decimal("0.0000"))
    enable_rollover: bool = False
    rollover_balance: Decimal = Field(default=Decimal("0.0000"))
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class CategoryBudgetProgress(BaseDomainModel):
    category_id: UUID
    category_name: str
    category_icon: str
    category_color: str
    budgeted: Decimal
    spent: Decimal
    remaining: Decimal
    pct_used: Decimal  # e.g. 71.0

    @property
    def spent_amount(self) -> Decimal:
        return self.spent

    @property
    def effective_budget(self) -> Decimal:
        return self.budgeted


class Goal(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    name: str
    target_amount: Decimal
    current_amount: Decimal = Field(default=Decimal("0.0000"))
    target_date: date | None = None
    icon: str = "target"
    color: str = "#059669"
    notes: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @property
    def pct_completed(self) -> int:
        if self.target_amount <= 0:
            return 100
        return min(100, int((self.current_amount / self.target_amount * 100).to_integral_value()))


# --- Users ---


class User(BaseDomainModel):
    """A login. All logins share the household's data, which belongs to the owner (ADR-019)."""

    id: UUID = Field(default_factory=uuid4)
    email: str
    hashed_password: str
    is_active: bool = True
    display_name: str = ""  # greeting and sidebar
    theme: str | None = None  # None = the household setting
    font_scale: int | None = None
    notification_prefs: dict[str, Any] = Field(default_factory=dict)  # empty = defaults
    invite_token_hash: str | None = None  # pending join / password link
    invite_expires_at: datetime | None = None


class PushSubscription(BaseDomainModel):
    """One device that accepts push notifications (ADR-021)."""

    id: UUID = Field(default_factory=uuid4)
    user_id: UUID  # the login that turned them on, not the household
    endpoint: str  # secret: anyone holding it can address this device
    p256dh: str
    auth: str


# --- Settings ---


class UserSettings(BaseDomainModel):
    """Display preferences (currency, number/date format, theme)."""

    user_id: UUID
    theme: str = "light"  # light | dark | system
    currency_code: str = "USD"
    currency_symbol: str = "$"
    locale: str = "en_US"
    font_scale: int = 100  # text size, % (Settings → Display)
    # Automatic bank sync; None = not set in the app, use the environment defaults.
    sync_enabled: bool | None = None
    sync_interval_hours: int | None = None
    sync_timezone: str | None = None
    sync_mode: str | None = None  # "interval" (None) or "cron" (0018)
    sync_cron: str | None = None  # five-field cron expression, used when sync_mode is "cron"
    # Optional AI categorization (docs/AI_PLAN.md); the key is stored encrypted.
    ai_enabled: bool = False
    ai_provider: str | None = None
    ai_base_url: str | None = None
    ai_model: str | None = None
    ai_api_key_encrypted: str | None = None
    ai_auto_apply_threshold: float | None = None  # None = never auto-apply
    ai_monthly_limit: int | None = None
    ai_run_after_sync: bool = True
    ai_options: dict[str, Any] = Field(default_factory=dict)  # services/ai/options.py
    # Dashboard cards per device (services/dashboard_layout.py); {} = defaults.
    dashboard_layout: dict[str, Any] = Field(default_factory=dict)


# --- AI categorization (docs/AI_PLAN.md) ---


class SuggestionStatus(StrEnum):
    PENDING = "pending"  # waiting for the user
    ACCEPTED = "accepted"  # user applied the suggested category
    CHANGED = "changed"  # user applied a different category
    REJECTED = "rejected"  # user skipped it
    UNKNOWN = "unknown"  # the AI wasn't sure; nothing to review
    AUTO_APPLIED = "auto_applied"  # applied automatically (phase C)


class AISuggestion(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    transaction_id: UUID
    category_id: UUID | None = None  # None = unknown
    confidence: float = 0.0
    reason: str | None = None
    rule_pattern: str | None = None
    status: SuggestionStatus = SuggestionStatus.PENDING
    provider: str
    model: str
    created_at: datetime = Field(default_factory=_utc_now)
    resolved_at: datetime | None = None


class AIRun(BaseDomainModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    started_at: datetime = Field(default_factory=_utc_now)
    trigger: str = "manual"  # manual | sync
    provider: str
    model: str
    items_sent: int = 0  # merchant groups sent (what counts against the monthly limit)
    transactions_covered: int = 0
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    status: str = "ok"  # ok | partial | error
    error: str | None = None


# --- Dashboard Summary ---


class DashboardSummary(BaseDomainModel):
    total_net_worth: Decimal
    monthly_net_worth_delta: Decimal  # net cash flow (income - expenses) this month
    monthly_net_worth_delta_pct: Decimal | None = None
    monthly_income: Decimal
    monthly_income_delta_pct: Decimal | None = None  # vs same span of last month
    monthly_expenses: Decimal
    monthly_expenses_delta_pct: Decimal | None = None  # vs same span of last month
    monthly_earned: Decimal = Field(default=Decimal("0.0000"))  # Income-group categories
    monthly_other_income: Decimal = Field(default=Decimal("0.0000"))  # uncategorized money in
    monthly_refunds: Decimal = Field(default=Decimal("0.0000"))  # netted against spending
    # The period compared with (same days last month, or the whole previous month)
    compare_income: Decimal = Field(default=Decimal("0.0000"))
    compare_expenses: Decimal = Field(default=Decimal("0.0000"))
    available_funds: Decimal
    budget_total_budgeted: Decimal
    budget_total_spent: Decimal
    budget_pct_used: Decimal
    upcoming_bills_count: int
    upcoming_bills_total: Decimal
    total_banking_balance: Decimal = Field(default=Decimal("0.0000"))
    total_investments: Decimal = Field(default=Decimal("0.0000"))
    total_liabilities: Decimal = Field(default=Decimal("0.0000"))

    @property
    def budget_remaining(self) -> Decimal:
        """Budgeted minus spent for the current month (never negative)."""
        return max(Decimal("0.00"), self.budget_total_budgeted - self.budget_total_spent)

    @property
    def monthly_surplus(self) -> Decimal:
        """Net monthly surplus (income minus expenses)."""
        return self.monthly_income - self.monthly_expenses


class MerchantIcon(BaseDomainModel):
    """A custom logo for a merchant; ``merchant`` is the normalized merchant name."""

    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    merchant: str
    content_type: str
    data: bytes = b""
    updated_at: datetime = Field(default_factory=_utc_now)
