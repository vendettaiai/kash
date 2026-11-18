"""Universal default rules and categories configuration for bootstrap and presets."""

from typing import Any

# Categories that mean "money moved between my own accounts" (never income/spending).
DEFAULT_TRANSFER_CATEGORY_NAMES = ("Transfers", "Credit Card Payment")

DEFAULT_CATEGORY_GROUPS: list[dict[str, Any]] = [
    {
        "name": "Essentials & Living",
        "sort_order": 1,
        "categories": [
            {"name": "Housing & Rent", "icon": "home", "color": "#0ea5e9", "sort_order": 1},
            {"name": "Groceries", "icon": "shopping-cart", "color": "#10b981", "sort_order": 2},
            {"name": "Utilities", "icon": "zap", "color": "#f59e0b", "sort_order": 3},
            {"name": "Transport", "icon": "car", "color": "#6366f1", "sort_order": 4},
            {"name": "Health", "icon": "heart", "color": "#ef4444", "sort_order": 5},
            {"name": "Insurance", "icon": "shield-check", "color": "#8b5cf6", "sort_order": 6},
        ],
    },
    {
        "name": "Discretionary & Lifestyle",
        "sort_order": 2,
        "categories": [
            {"name": "Dining & Takeout", "icon": "utensils", "color": "#f97316", "sort_order": 1},
            {"name": "Shopping", "icon": "shopping-bag", "color": "#ec4899", "sort_order": 2},
            {"name": "Subscriptions", "icon": "credit-card", "color": "#06b6d4", "sort_order": 3},
            {"name": "Leisure", "icon": "film", "color": "#a855f7", "sort_order": 4},
            {"name": "Personal Care", "icon": "smile", "color": "#14b8a6", "sort_order": 5},
            {"name": "Travel & Vacation", "icon": "plane", "color": "#3b82f6", "sort_order": 6},
        ],
    },
    {
        "name": "Income",
        "sort_order": 3,
        "categories": [
            {"name": "Salary & Income", "icon": "dollar-sign", "color": "#059669", "sort_order": 1},
            {
                "name": "Freelance & Consulting",
                "icon": "briefcase",
                "color": "#10b981",
                "sort_order": 2,
            },
            {
                "name": "Investments & Dividends",
                "icon": "trending-up",
                "color": "#3b82f6",
                "sort_order": 3,
            },
            {"name": "Other Income", "icon": "gift", "color": "#84cc16", "sort_order": 4},
        ],
    },
    {
        "name": "Transfers & Financial",
        "sort_order": 4,
        "categories": [
            {
                "name": "Transfers",
                "icon": "arrow-left-right",
                "color": "#64748b",
                "sort_order": 1,
                "is_transfer": True,
            },
            {
                "name": "Credit Card Payment",
                "icon": "credit-card",
                "color": "#475569",
                "sort_order": 2,
                "is_transfer": True,
            },
            {"name": "Taxes & Fees", "icon": "receipt", "color": "#94a3b8", "sort_order": 3},
            {
                # Money sent to other people (Zelle, Cash App...). Real spending,
                # unlike Transfers. Money received this way stays uncategorized
                # ("other income") so you can file it as a payback yourself.
                "name": "Person to Person",
                "icon": "smartphone",
                "color": "#0891b2",
                "sort_order": 4,
            },
        ],
    },
]


UNIVERSAL_DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "name": "Transport",
        "priority": 10,
        "conditions_op": "or",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": (
                    r"PILOT|FLYING / J|PETRO|TA #|TA / #|LOVE'S|SHEETZ|SHELL / OIL|BP#|"
                    r"AG / TRAVEL PLAZA|NOURIA|RUTTERS|SUPERCHARGE|E-Z\*PASS|UBER / \*TRIP|"
                    r"LYFT|CAR WASH|PARKING|SOUTHWES|SWA\*|Harrisburg Middletown"
                ),
            }
        ],
        "actions": [
            {"op": "set_category_named", "value": "Transport"},
        ],
    },
    {
        "name": "Transfers",
        "priority": 10,
        "conditions_op": "or",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": (
                    r"Transfer to \*|Transfer from /|Transfer TO / :|Transfer FROM:|Withdrawal,|"
                    r"BARCLAYCARD|AMEX / EPAYMENT|DISCOVER DC / PYMNTS|SYF PAYMNT|"
                    r"Stewart Oshane Visa Direct|PAYPAL / INSTANT TRANSFER"
                ),
            }
        ],
        "actions": [
            {"op": "set_category_named", "value": "Transfers"},
        ],
    },
    {
        "name": "Person to Person",
        "priority": 10,
        "conditions_op": "and",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": r"Zelle|ZELLE|CASH / APP|CASH APP|XOOM|VENMO|Venmo",
            },
            {"field": "amount", "op": "lt", "value": "0"},
        ],
        "actions": [
            {"op": "set_category_named", "value": "Person to Person"},
        ],
    },
    {
        "name": "Shopping",
        "priority": 10,
        "conditions_op": "or",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": (
                    r"AMAZON|WAL-MART|WALMART|TARGET|DOLLAR / TREE|BJS / WHOLESALE|BURLINGTON|"
                    r"MARSHALLS|ROSS / STORES|PRIMARK|HOME DEPOT|WALGREENS|AFFIRM|"
                    r"ZIP\* / GOOGLE PAY|BOSS / MONEY|LIME \(JAMAICA\)|MAILPAC|"
                    r"Accrue / for Snipes|SoNo Collection"
                ),
            }
        ],
        "actions": [
            {"op": "set_category_named", "value": "Shopping"},
        ],
    },
    {
        "name": "Subscriptions",
        "priority": 15,
        "conditions_op": "or",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": (
                    r"YouTubePremium|YOUTUBEPREMIUM|SPOTIFY|Google / One|GOOGLE.*ONE|Workspace|"
                    r"WORKSPACE|GOOGLE.*CLOUD|SVCSFAHMULATRUC|Twitch|TWITCH|Trucker Path|"
                    r"TRUCKER PATH|CHATGPT|OPENAI|STARLINK|FRONTIER|TESLA.*SUBSCRIPTIO|"
                    r"SIMPLEFIN|DRUNKENSLUG|DKWEBHOSTIN|AMZNFreeTime"
                ),
            }
        ],
        "actions": [
            {"op": "set_category_named", "value": "Subscriptions"},
        ],
    },
    {
        "name": "Groceries",
        "priority": 15,
        "conditions_op": "or",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": r"PRICERITE|FOOD / BAZAAR|GALA / FOODS|C TOWN|SHOPRITE",
            },
            {
                "field": "description",
                "op": "contains",
                "value": "Point Of Sale Withdrawal / BJS.COM EXP 800-257-2582 MAUS",
            },
        ],
        "actions": [
            {"op": "set_category_named", "value": "Groceries"},
        ],
    },
    {
        "name": "Personal Care",
        "priority": 20,
        "conditions_op": "or",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": r"WIG N / COLOR|HAIR / PLUS|SHUBH / BEAUTY|SUPER BUBBLES",
            }
        ],
        "actions": [
            {"op": "set_category_named", "value": "Personal Care"},
        ],
    },
    {
        "name": "Taxes & Fees",
        "priority": 20,
        "conditions_op": "or",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": r"NSF FEE|International Fee|DISTRICT COURT|PASSPORT|ATI\*",
            }
        ],
        "actions": [
            {"op": "set_category_named", "value": "Taxes & Fees"},
        ],
    },
    {
        "name": "Salary & Income",
        "priority": 20,
        "conditions_op": "or",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": r"SALARY|PAYROLL|DIRECT DEPOSIT",
            }
        ],
        "actions": [
            {"op": "set_category_named", "value": "Salary & Income"},
        ],
    },
    {
        "name": "Health",
        "priority": 20,
        "conditions_op": "or",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": (
                    r"NATIONAL / INCOME LIFE|NATIONAL INCOME LIFE|"
                    r"EDGE / FITNESS|THE EDGE FITNESS"
                ),
            }
        ],
        "actions": [
            {"op": "set_category_named", "value": "Health"},
        ],
    },
    {
        "name": "Leisure",
        "priority": 20,
        "conditions_op": "or",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": r"Merge Cooking|Fishdom|MONOPOLY GO|MonopolyGo|STEAM GAMES|GWL / POCONOS",
            },
            {
                "field": "description",
                "op": "contains",
                "value": "Point Of Sale Withdrawal GIRL / SCOUTS OF THE USA 800-811-9342 NYUS",
            },
        ],
        "actions": [
            {"op": "set_category_named", "value": "Leisure"},
        ],
    },
    {
        "name": "Fast Food & Dining",
        "priority": 20,
        "conditions_op": "or",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": (
                    r"MCDONALD|TACO BELL|HARDEE|CHICK-FIL-A|CHIPOTLE|WENDY|"
                    r"BURGER KING|SUBWAY|STARBUCKS|DUNKIN|CHEESECAKE"
                ),
            }
        ],
        "actions": [
            {"op": "set_category_named", "value": "Dining & Takeout"},
        ],
    },
    {
        "name": "Investments & Brokerage",
        "priority": 5,
        "conditions_op": "or",
        "conditions": [
            {
                "field": "description",
                "op": "regex",
                "value": (
                    r"PURCHASE INTO CORE ACCOUNT|MONEY MARKET|SPAXX|"
                    r"FIDELITY|SCHWAB|VANGUARD|ROBINHOOD"
                ),
            },
        ],
        "actions": [
            {"op": "set_category_named", "value": "Investments & Dividends"},
        ],
    },
]

DEFAULT_MATCHING_RULES: list[dict[str, Any]] = [
    {
        "name": "Transfer Exact Match",
        "rule_type": "transfer",
        "priority": 10,
        "is_active": True,
        "action_mode": "links",
        "config": {
            "different_accounts": True,
            "amount_match_mode": "exact",
            "days_early": 2,
            "days_late": 2,
            "unique_candidate": True,
        },
    },
    {
        "name": "Recurring Payment Match",
        "rule_type": "recurring",
        "priority": 10,
        "is_active": True,
        "action_mode": "links",
        # Links bank payments to bill due dates (services/bills/service.py).
        # Without days_early/days_late the window depends on the bill's frequency.
        "config": {
            "same_account": True,
            "amount_match_mode": "within_percent",
            "amount_tolerance_percent": 25,
        },
    },
]
