"""PostgreSQL schema: customers, orders, cadence tracking and conversation state."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bruno.db import Base
from bruno.fsm import State
from bruno.timeutil import utcnow


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(120))
    phone_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    total_orders: Mapped[int] = mapped_column(Integer, default=0)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    # Tokenized card / Apple Pay reference from the payment processor. Never raw PANs.
    payment_token: Mapped[str | None] = mapped_column(String(255))
    card_label: Mapped[str | None] = mapped_column(String(40))  # e.g. "Visa •4242"

    orders: Mapped[list["Order"]] = relationship(back_populates="customer")
    cadence: Mapped["CadenceTracker | None"] = relationship(back_populates="customer")

    @property
    def first_name(self) -> str:
        return (self.name or "").split(" ")[0] or "there"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    items_json: Mapped[list] = mapped_column(JSON, default=list)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    order_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    channel: Mapped[str] = mapped_column(String(16))  # text | voice | pos
    status: Mapped[str] = mapped_column(String(16))  # completed | lapsed | active_chat
    pickup_time: Mapped[str | None] = mapped_column(String(5))
    pos_order_id: Mapped[str | None] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    payment_reference: Mapped[str | None] = mapped_column(String(128))
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"))

    customer: Mapped[Customer] = relationship(back_populates="orders")


class CadenceTracker(Base):
    __tablename__ = "cadence_tracker"

    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), primary_key=True)
    avg_days_between_orders: Mapped[float | None] = mapped_column(Float)
    last_order_date: Mapped[datetime | None] = mapped_column(DateTime)
    drift_threshold_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    usual_order_json: Mapped[list | None] = mapped_column(JSON)
    preferred_weekday: Mapped[int | None] = mapped_column(Integer)  # 0 = Monday
    typical_pickup_time: Mapped[str | None] = mapped_column(String(5))  # local "HH:MM"
    last_outreach_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    customer: Mapped[Customer] = relationship(back_populates="cadence")


class Conversation(Base):
    """Shared dialogue state. Text and voice channels read/write the same row."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    state: Mapped[str] = mapped_column(String(48), default=State.INITIAL_OUTREACH.value)
    cart_json: Mapped[list] = mapped_column(JSON, default=list)
    pickup_time: Mapped[str | None] = mapped_column(String(5))
    origin: Mapped[str] = mapped_column(String(16), default="outreach")  # outreach | inbound
    last_channel: Mapped[str] = mapped_column(String(16), default="text")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    customer: Mapped[Customer] = relationship()
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", order_by="Message.id"
    )

    @property
    def fsm_state(self) -> State:
        return State(self.state)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # customer | agent | system
    channel: Mapped[str] = mapped_column(String(16))  # text | voice
    content: Mapped[str] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
