"""Single-owner identity for this self-hosted instance.

Kash currently runs as a single-user app. Every record is owned by this fixed
user id until authentication and multi-user support land (see PROJECT_PLAN
Phase 8). The value must never change: existing databases reference it.
"""

from uuid import UUID

OWNER_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
