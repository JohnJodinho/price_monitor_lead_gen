from time import timezone
from datetime import datetime
import uuid
import enum
from typing import List
from decimal import Decimal
from sqlalchemy import (
    Integer,
    String,
    DateTime,
    ForeignKey,
    Numeric,
    Boolean,
    Text,
    UniqueConstraint,
    CheckConstraint,
    Uuid,
    func,
    Enum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


class AlertType(str, enum.Enum):
    THRESHOLD = "threshold"
    CHANGE = "change"


class CustomBase(Base):
    """Base class for all models"""

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Product(CustomBase):
    """A product being monitored across one or more URLs"""

    __tablename__ = "products"

    name: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(Text, unique=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    price_history: Mapped[List["PriceHistory"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    alerts: Mapped[List["PriceAlert"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class PriceHistory(CustomBase):
    """Time-series price data for each product"""

    __tablename__ = "price_history"

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    tier_used: Mapped[int] = mapped_column()

    product: Mapped["Product"] = relationship(back_populates="price_history")


class PriceAlert(CustomBase):
    """Fired when a price event is detected."""

    __tablename__ = "price_alerts"
    __table_args__ = (
        # Enforce data integrity at the database level so bad app logic
        # can't insert invalid hybrid rows.
        CheckConstraint(
            f"(alert_type = '{AlertType.THRESHOLD.name}' AND target_price IS NOT NULL AND previous_price IS NULL AND pct_change IS NULL) OR "
            f"(alert_type = '{AlertType.CHANGE.name}' AND previous_price IS NOT NULL AND pct_change IS NOT NULL AND target_price IS NULL)",
            name="chk_valid_alert_data",
        ),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    alert_type: Mapped[AlertType] = mapped_column(
        Enum(AlertType, native_enum=False, length=20)
    )
    price_at_alert: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    # "threshold" alerts
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    # "change" alerts
    previous_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    pct_change: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))

    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)

    product: Mapped["Product"] = relationship(back_populates="alerts")


class LeadTarget(CustomBase):
    """A seed URL to search for leads."""

    __tablename__ = "lead_targets"

    url: Mapped[str] = mapped_column(Text, unique=True)
    category: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    leads: Mapped[list["Lead"]] = relationship(
        back_populates="target", cascade="all, delete-orphan"
    )


class Lead(CustomBase):
    """An extracted business lead."""

    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("target_id", "email", name="uq_lead_target_email"),
    )

    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lead_targets.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str | None] = mapped_column(String(500))
    phone: Mapped[str | None] = mapped_column(String(100))
    company_name: Mapped[str | None] = mapped_column(String(500))
    contact_name: Mapped[str | None] = mapped_column(String(500))
    source_url: Mapped[str | None] = mapped_column(Text)

    target: Mapped["LeadTarget"] = relationship(back_populates="leads")


class RunJobType(str, enum.Enum):
    PRICE_MONITOR = "price_monitor"
    LEAD_GEN = "lead_gen"


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class ScrapeRun(CustomBase):
    """A log of a complete scraping job run (price monitor or lead generator)."""

    __tablename__ = "scrape_runs"

    job_type: Mapped[RunJobType] = mapped_column(
        Enum(RunJobType, native_enum=False, length=20), index=True
    )
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, native_enum=False, length=20)
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items_attempted: Mapped[int] = mapped_column(Integer, default=0)
    items_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text)
