"""Database Data Provider orchestrating SQL repositories."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kash.db.repositories.account_repo import SqlAccountRepository
from kash.db.repositories.ai_repo import SqlAIRepository
from kash.db.repositories.bank_connection_repo import SqlBankConnectionRepository
from kash.db.repositories.bill_repo import SqlBillRepository
from kash.db.repositories.budget_repo import SqlBudgetRepository
from kash.db.repositories.category_repo import SqlCategoryRepository
from kash.db.repositories.dashboard_repo import SqlDashboardRepository
from kash.db.repositories.goal_repo import SqlGoalRepository
from kash.db.repositories.merchant_icon_repo import SqlMerchantIconRepository
from kash.db.repositories.notification_repo import SqlNotificationRepository
from kash.db.repositories.rule_repo import SqlRuleRepository
from kash.db.repositories.settings_repo import SqlSettingsRepository
from kash.db.repositories.transaction_repo import SqlTransactionRepository
from kash.db.repositories.user_repo import SqlUserRepository
from kash.db.session import get_session_factory
from kash.domain.protocols import (
    AccountRepository,
    AIRepository,
    BankConnectionRepository,
    BillRepository,
    BudgetRepository,
    CategoryRepository,
    DashboardRepository,
    DataProvider,
    GoalRepository,
    MerchantIconRepository,
    NotificationRepository,
    RuleRepository,
    SettingsRepository,
    TransactionRepository,
    UserRepository,
)


class DatabaseDataProvider(DataProvider):
    """Production SQL Data Provider backed by PostgreSQL / SQLite async sessions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None):
        self._session_factory = session_factory or get_session_factory()
        self._accounts = SqlAccountRepository(self._session_factory)
        self._transactions = SqlTransactionRepository(self._session_factory)
        self._categories = SqlCategoryRepository(self._session_factory)
        self._rules = SqlRuleRepository(self._session_factory)
        self._bills = SqlBillRepository(self._session_factory)
        self._budgets = SqlBudgetRepository(self._session_factory)
        self._goals = SqlGoalRepository(self._session_factory)
        self._dashboard = SqlDashboardRepository(self._session_factory)
        self._bank_connections = SqlBankConnectionRepository(self._session_factory)
        self._settings = SqlSettingsRepository(self._session_factory)
        self._users = SqlUserRepository(self._session_factory)
        self._ai = SqlAIRepository(self._session_factory)
        self._merchant_icons = SqlMerchantIconRepository(self._session_factory)
        self._notifications = SqlNotificationRepository(self._session_factory)

    @property
    def accounts(self) -> AccountRepository:
        return self._accounts

    @property
    def transactions(self) -> TransactionRepository:
        return self._transactions

    @property
    def categories(self) -> CategoryRepository:
        return self._categories

    @property
    def rules(self) -> RuleRepository:
        return self._rules

    @property
    def bills(self) -> BillRepository:
        return self._bills

    @property
    def budgets(self) -> BudgetRepository:
        return self._budgets

    @property
    def goals(self) -> GoalRepository:
        return self._goals

    @property
    def dashboard(self) -> DashboardRepository:
        return self._dashboard

    @property
    def bank_connections(self) -> BankConnectionRepository:
        return self._bank_connections

    @property
    def settings(self) -> SettingsRepository:
        return self._settings

    @property
    def users(self) -> UserRepository:
        return self._users

    @property
    def ai(self) -> AIRepository:
        return self._ai

    @property
    def merchant_icons(self) -> MerchantIconRepository:
        return self._merchant_icons

    @property
    def notifications(self) -> NotificationRepository:
        return self._notifications
