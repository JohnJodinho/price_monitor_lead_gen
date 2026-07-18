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
from sqlalchemy.dialects.postgresql import JSONB

from db import Base


class AlertType(str, enum.Enum):
    THRESHOLD = "threshold"
    CHANGE = "change"


class ClientVertical(str, enum.Enum):
    ECOMMERCE = "ecommerce"
    REAL_ESTATE = "real_estate"


class OutreachStatus(str, enum.Enum):
    NOT_CONTACTED = "not_contacted"
    CONTACTED = "contacted"
    REPLIED = "replied"
    CLOSED = "closed"
    DEAD = "dead"


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

    sku: Mapped[str | None] = mapped_column(
        String(200), index=True
    )  # Grabs the same item across retailers
    name: Mapped[str] = mapped_column(String(500))
    category: Mapped[str | None] = mapped_column(
        String(200), index=True, default="uncategorized"
    )
    url: Mapped[str] = mapped_column(Text, unique=True)
    retailer: Mapped[str] = mapped_column(
        String(100), index=True
    )  # e.g. "bestbuy" or "amazon" or "walmart"
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    price_history: Mapped[List["PriceHistory"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    alerts: Mapped[List["PriceAlert"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), index=True
    )


class PriceHistory(CustomBase):
    """Time-series price data for each product"""

    __tablename__ = "price_history"

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    price: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )  # Use null because some retailers might not have the product in stock
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    merchant: Mapped[str | None] = mapped_column(
        String(255)
    )  # "Sold by Amazon" vs a 3rd-party seller — context for Buy-Box-hijack price crashes

    tier_used: Mapped[int] = mapped_column()
    meta_data: Mapped[dict] = mapped_column(
        JSONB, default=dict
    )  # shipping_cost, coupon_active, condition, etc. — no new column per edge case
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
        UniqueConstraint("target_id", "source_url", name="uq_lead_target_url"),
    )

    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lead_targets.id", ondelete="CASCADE"), index=True
    )
    company_name: Mapped[str | None] = mapped_column(String(500))
    source_url: Mapped[str | None] = mapped_column(Text)

    contacts: Mapped[dict] = mapped_column(JSONB, default=dict)
    socials: Mapped[dict] = mapped_column(JSONB, default=dict)

    related_alert_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_alerts.id", ondelete="SET NULL")
    )
    pitch_summary: Mapped[str | None] = mapped_column(
        Text
    )  # "12 SKUs, 8% above market on high-volume items"
    outreach_status: Mapped[OutreachStatus] = mapped_column(
        Enum(OutreachStatus, native_enum=False, length=20),
        default=OutreachStatus.NOT_CONTACTED,
    )  # "not_contacted"|"contacted"|"replied"|"closed"|"dead"
    target: Mapped["LeadTarget"] = relationship(back_populates="leads")


class RunJobType(str, enum.Enum):
    PRICE_MONITOR = "price_monitor"
    LEAD_GEN = "lead_gen"
    REAL_ESTATE_MONITOR = "real_estate_monitor"


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
    meta_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    platform: Mapped[str | None] = mapped_column(
        String(200)
    )  # "amazon" | "airbnb" | "vrbo" | ...


class Property(CustomBase):
    """A single platform's listing for a rental property being monitored."""

    __tablename__ = "properties"

    name: Mapped[str] = mapped_column(String(500))
    property_key: Mapped[str | None] = mapped_column(
        String(200), index=True
    )  # groups Airbnb/Vrbo/Booking listings of the same physical unit
    platform: Mapped[str] = mapped_column(
        String(50), index=True
    )  # "airbnb" | "vrbo" | "booking"
    url: Mapped[str] = mapped_column(Text, unique=True)
    market: Mapped[str] = mapped_column(String(200))  # "NYC/NJ Metro", "Miami"
    bedrooms: Mapped[int | None] = mapped_column(Integer)
    host_name: Mapped[str | None] = mapped_column(String(300))
    cleaning_fee: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    review_count: Mapped[int | None] = mapped_column(Integer)
    avg_rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    consecutive_404s: Mapped[int] = mapped_column(Integer, default=0)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), index=True
    )

    rate_history: Mapped[List["RateHistory"]] = relationship(
        back_populates="property", cascade="all, delete-orphan"
    )


class RateHistory(CustomBase):
    """Nightly rate snapshot. stay_date is the night being priced;
    created_at (inherited) is when price is captured - both matter,
    since dynamic pricing shifts as the stay date approaches."""

    __tablename__ = "rate_history"

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="CASCADE"), index=True
    )
    stay_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    nightly_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )  # NULL = booked/blocked, never 0.00
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    meta_data: Mapped[dict] = mapped_column(
        JSONB, default=dict
    )  # minimum_stay, discount_applied, special_event_pricing, etc.

    property: Mapped["Property"] = relationship(back_populates="rate_history")


class Client(CustomBase):
    __tablename__ = "clients"

    company_name: Mapped[str] = mapped_column(String(500))
    contact_email: Mapped[str | None] = mapped_column(String(300))
    vertical: Mapped[ClientVertical] = mapped_column(
        Enum(ClientVertical, native_enum=False, length=20), index=True
    )  # "ecommerce" | "real_estate"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    def __repr__(self):
        return f"<Client(company_name={self.company_name}, contact_email={self.contact_email}, vertical={self.vertical})>"
