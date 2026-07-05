from sqlalchemy import (
    create_engine, Column, String, Float, DateTime, Enum, Boolean, Integer,
    ForeignKey, UniqueConstraint,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import enum
from config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class AdminRole(str, enum.Enum):
    admin = "admin"        # полный доступ: точки, слоты, сессии, чёрный список, пользователи
    operator = "operator"  # оперативная работа: точки, слоты, сессии, возвраты — без настроек/пользователей
    viewer = "viewer"      # только просмотр, без единого мутирующего действия


class SessionStatus(str, enum.Enum):
    pending = "pending"                # инвойс создан, ждём оплату
    paid = "paid"                      # оплата подтверждена JetQR
    dispensing = "dispensing"          # команда выдачи отправлена контроллеру
    dispensed = "dispensed"            # VMC подтвердил успешную выдачу (0x02)
    refund_pending = "refund_pending"  # выдача не удалась, инициируем возврат
    refunded = "refunded"              # возврат подтверждён
    expired = "expired"                # оплата не пришла за отведённое время
    failed = "failed"                  # невосстановимая ошибка (см. error поле)


class VendingMachine(Base):
    __tablename__ = "vending_machines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    machine_id = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=True)
    location = Column(String(200), nullable=True)
    secret_token = Column(String(100), nullable=False)
    is_active = Column(Boolean, default=True)
    # Отдельные реквизиты JetQR на точку (разделение статистики/денег).
    # Если NULL — используются значения из settings.
    jetqr_store_id = Column(String(50), nullable=True)
    jetqr_terminal_id = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    slots = relationship("ProductSlot", back_populates="machine")


class ProductSlot(Base):
    __tablename__ = "product_slots"
    __table_args__ = (UniqueConstraint("machine_id", "slot_id", name="uq_machine_slot"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    machine_id = Column(String(50), ForeignKey("vending_machines.machine_id"), nullable=False, index=True)
    slot_id = Column(Integer, nullable=False)          # номер слота на VMC (selection number)
    product_name = Column(String(100), nullable=False)
    price = Column(Float, nullable=False)
    stock_qty = Column(Integer, default=0)
    capacity = Column(Integer, default=10)
    image_url = Column(String(300), nullable=True)
    is_active = Column(Boolean, default=True)

    machine = relationship("VendingMachine", back_populates="slots")


class VendingSession(Base):
    __tablename__ = "vending_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    machine_id = Column(String(50), nullable=False, index=True)
    slot_id = Column(Integer, nullable=False)
    product_name = Column(String(100), nullable=True)
    amount = Column(Float, nullable=False)
    status = Column(Enum(SessionStatus), default=SessionStatus.pending, index=True)
    invoice_id = Column(String(100), nullable=True, index=True)
    mis_payment_id = Column(String(100), nullable=True)
    transaction_id = Column(String(100), nullable=True)   # из JetQR после оплаты, нужен для возврата
    phone_number = Column(String(20), nullable=True)
    bank_name = Column(String(100), nullable=True)
    error = Column(String(200), nullable=True)            # код/описание ошибки VMC при сбое выдачи
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    paid_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)


class Blacklist(Base):
    __tablename__ = "blacklist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String(20), unique=True, nullable=False, index=True)
    reason = Column(String(200), nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(200), nullable=False)
    role = Column(Enum(AdminRole), default=AdminRole.operator, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("admin_users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
