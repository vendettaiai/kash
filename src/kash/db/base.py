"""SQLAlchemy declarative base and common model definitions."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Numeric, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# Cross-dialect JSON type supporting PostgreSQL JSONB and SQLite JSON
JsonType = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy declarative models."""

    type_annotation_map = {
        uuid.UUID: Uuid(as_uuid=True),
        Decimal: Numeric(18, 4),
        datetime: DateTime(timezone=True),
        dict[str, Any]: JsonType,
        list[Any]: JsonType,
    }


class TimestampMixin:
    """Mixin for created_at and updated_at audit columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
