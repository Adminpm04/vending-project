from __future__ import annotations

import logging
logging.basicConfig(level=logging.INFO)

from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime
import asyncio
import json
import os
import re
import secrets
import uuid

from database import (
    get_db, init_db, SessionLocal,
    VendingMachine, ProductSlot, VendingSession, Blacklist, SessionStatus,
    AdminUser, AdminSession, AdminRole, Product,
)
from jetqr import create_invoice, check_invoice, cancel_invoice
from config import settings
import auth as authlib

app = FastAPI(title="Vending QR Payment System")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# machine_id -> WebSocket контроллера точки (RS232-агент)
machine_clients: dict[str, WebSocket] = {}
# machine_id -> WebSocket'ы kiosk-интерфейсов (экран может переподключаться)
kiosk_clients: dict[str, list[WebSocket]] = {}
# session_id -> Future с результатом выдачи от контроллера
_dispense_waiters: dict[int, asyncio.Future] = {}
# request_id -> Future с результатом проверки слота датчиком (до оплаты)
_slot_check_waiters: dict[str, asyncio.Future] = {}
SLOT_CHECK_TIMEOUT = 4  # секунд — не держим клиента перед QR дольше этого


async def _db(fn):
    """Синхронный SQLAlchemy-вызов вне event loop (thread pool)."""
    return await asyncio.get_event_loop().run_in_executor(None, fn)


# ─── STARTUP ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    await _db(_bootstrap_admin)
    asyncio.create_task(_ws_heartbeat())


def _bootstrap_admin():
    """Один раз при пустой таблице пользователей — создать стартовый admin-аккаунт.
    Дальше пользователей заводит сам admin через панель, эта функция больше
    не понадобится (данные из настроек используются только для первого запуска)."""
    db = SessionLocal()
    try:
        if db.query(AdminUser).count() > 0:
            return
        db.add(AdminUser(
            username=settings.ADMIN_BOOTSTRAP_USERNAME,
            password_hash=authlib.hash_password(settings.ADMIN_BOOTSTRAP_PASSWORD),
            role=AdminRole.admin,
        ))
        db.commit()
        logging.warning(
            f"Создан стартовый admin-аккаунт '{settings.ADMIN_BOOTSTRAP_USERNAME}' — "
            f"смените пароль через панель после первого входа."
        )
    finally:
        db.close()


async def _ws_heartbeat():
    """Ping каждые 25 с — держит WebSocket живым через NAT/прокси."""
    while True:
        await asyncio.sleep(25)
        for mid, ws in list(machine_clients.items()):
            try:
                await ws.send_text('{"type":"ping"}')
            except Exception:
                machine_clients.pop(mid, None)
        for mid, lst in list(kiosk_clients.items()):
            for ws in list(lst):
                try:
                    await ws.send_text('{"type":"ping"}')
                except Exception:
                    # kiosk_ws мог удалить ws параллельно — remove() тогда бросил бы
                    # ValueError и убил бы весь heartbeat-цикл.
                    if ws in lst:
                        lst.remove(ws)


# ─── KIOSK API (планшет: каталог, покупка) ───────────────────────────────────

@app.get("/api/kiosk/{machine_id}/products")
async def kiosk_products(machine_id: str, db: Session = Depends(get_db)):
    machine = db.query(VendingMachine).filter(
        VendingMachine.machine_id == machine_id,
        VendingMachine.is_active == True,
    ).first()
    if not machine:
        raise HTTPException(404, "machine not found")
    slots = db.query(ProductSlot).filter(
        ProductSlot.machine_id == machine_id,
        ProductSlot.is_active == True,
    ).order_by(ProductSlot.slot_id).all()
    return {
        "machine_id": machine_id,
        "name": machine.name,
        "location": machine.location,
        "online": machine_id in machine_clients,
        "products": [
            {
                "slot_id": s.slot_id,
                "name": s.product_name,
                "price": s.price,
                "stock": s.stock_qty,
                "image_url": s.image_url,
                "available": s.stock_qty > 0,
            }
            for s in slots
        ],
    }


async def check_slot_stock(machine_id: str, slot_id: int) -> dict:
    """Спросить контроллер о реальном состоянии слота (датчик VMC, команда 0x01),
    прежде чем показать клиенту QR — ловит случай, когда остаток в базе устарел
    (забыли обновить в админке), а физически товар уже кончился.
    Fail-open: если контроллер не ответил вовремя (в т.ч. старая версия APK без
    поддержки этой проверки) — не блокируем покупку, полагаемся на stock_qty,
    как раньше."""
    ws = machine_clients.get(machine_id)
    if ws is None:
        return {"checked": False}

    request_id = str(uuid.uuid4())
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _slot_check_waiters[request_id] = future
    try:
        await ws.send_text(json.dumps({
            "type": "check_slot",
            "request_id": request_id,
            "slot_id": slot_id,
        }))
        result = await asyncio.wait_for(future, timeout=SLOT_CHECK_TIMEOUT)
    except Exception:
        return {"checked": False}
    finally:
        _slot_check_waiters.pop(request_id, None)

    if not result.get("checked", True):
        return {"checked": False}
    return {"checked": True, "ok": bool(result.get("ok")), "message": result.get("message")}


@app.post("/api/kiosk/{machine_id}/buy")
async def kiosk_buy(machine_id: str, data: dict, db: Session = Depends(get_db)):
    """Клиент выбрал товар: создаём сессию + инвойс, возвращаем QR."""
    try:
        slot_id = int(data["slot_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "slot_id required")

    machine = db.query(VendingMachine).filter(
        VendingMachine.machine_id == machine_id,
        VendingMachine.is_active == True,
    ).first()
    if not machine:
        raise HTTPException(404, "machine not found")
    if machine_id not in machine_clients:
        raise HTTPException(503, "machine offline")

    slot = db.query(ProductSlot).filter(
        ProductSlot.machine_id == machine_id,
        ProductSlot.slot_id == slot_id,
        ProductSlot.is_active == True,
    ).first()
    if not slot:
        raise HTTPException(404, "slot not found")
    if slot.stock_qty <= 0:
        raise HTTPException(409, "out of stock")

    check = await check_slot_stock(machine_id, slot_id)
    if check.get("checked") and not check.get("ok"):
        slot.stock_qty = 0
        db.commit()
        raise HTTPException(409, "out of stock")

    session = VendingSession(
        machine_id=machine_id,
        slot_id=slot_id,
        product_name=slot.product_name,
        amount=slot.price,
        status=SessionStatus.pending,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    result = await create_invoice(
        machine_id, slot_id, slot.price,
        store_id=machine.jetqr_store_id,
        terminal_id=machine.jetqr_terminal_id,
    )
    if not result["success"]:
        session.status = SessionStatus.failed
        session.error = "invoice creation failed"
        session.closed_at = datetime.utcnow()
        db.commit()
        raise HTTPException(502, "payment system unavailable")

    session.invoice_id = result["invoice_id"]
    session.mis_payment_id = result["mis_payment_id"]
    db.commit()

    asyncio.create_task(poll_payment(session.id, result["invoice_id"]))

    return {
        "session_id": session.id,
        "invoice_id": result["invoice_id"],
        "amount": slot.price,
        "product": slot.product_name,
        "qr_url": f"/api/qr/{result['invoice_id']}",
        # Киоск отсчитывает ровно столько же, сколько сервер поллит оплату —
        # иначе экран сбросится раньше, а поздняя оплата выдаст товар в пустоту.
        "payment_timeout": settings.PAYMENT_POLL_TIMEOUT,
    }


@app.get("/api/kiosk/session/{session_id}")
async def kiosk_session_status(session_id: int, db: Session = Depends(get_db)):
    s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "session not found")
    return {"session_id": s.id, "status": s.status, "error": s.error}


@app.post("/api/kiosk/session/{session_id}/cancel")
async def kiosk_session_cancel(session_id: int, db: Session = Depends(get_db)):
    """Клиент нажал «Отмена» на экране оплаты. Пока сессия pending — закрываем её,
    чтобы сервер перестал принимать оплату по этому QR (иначе поллинг ждёт ещё
    до 5 минут и поздняя оплата выдала бы товар при сброшенном экране).
    Если оплата уже прошла (не pending) — отменять поздно, флоу продолжается."""
    s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "session not found")
    if s.status != SessionStatus.pending:
        return {"cancelled": False, "status": s.status}
    s.status = SessionStatus.expired
    s.error = "cancelled by customer"
    s.closed_at = datetime.utcnow()
    db.commit()
    return {"cancelled": True, "status": s.status}


# ─── PAYMENT FLOW ────────────────────────────────────────────────────────────

async def poll_payment(session_id: int, invoice_id: str):
    """Поллинг JetQR раз в 2 с до оплаты (или таймаут). Затем — выдача."""
    max_attempts = int(settings.PAYMENT_POLL_TIMEOUT / settings.PAYMENT_POLL_INTERVAL)

    for _ in range(max_attempts):
        await asyncio.sleep(settings.PAYMENT_POLL_INTERVAL)

        def _still_pending():
            db = SessionLocal()
            try:
                s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
                return s is not None and s.status == SessionStatus.pending
            finally:
                db.close()
        if not await _db(_still_pending):
            return  # сессию отменили/закрыли — прекращаем поллинг

        result = await check_invoice(invoice_id)

        if result.get("paid"):
            phone = result.get("phone")

            def _check_blacklist():
                db = SessionLocal()
                try:
                    return phone and db.query(Blacklist).filter(
                        Blacklist.phone_number == phone).first() is not None
                finally:
                    db.close()
            blacklisted = await _db(_check_blacklist)

            def _mark_paid():
                db = SessionLocal()
                try:
                    s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
                    if s:
                        s.status = SessionStatus.paid
                        s.paid_at = datetime.utcnow()
                        s.phone_number = phone
                        s.bank_name = result.get("bank")
                        s.transaction_id = result.get("transaction_id")
                        db.commit()
                        return s.machine_id, s.slot_id
                    return None, None
                finally:
                    db.close()
            machine_id, slot_id = await _db(_mark_paid)

            if blacklisted:
                logging.warning(f"Blacklisted phone {phone} paid session {session_id} — refunding")
                await _start_refund(session_id, "blacklisted client")
                return

            if machine_id:
                await notify_kiosk(machine_id, {"type": "paid", "session_id": session_id})
                await dispense(session_id, machine_id, slot_id)
            return

        if result.get("error"):
            break

    # Таймаут/ошибка — закрываем неоплаченную сессию
    def _expire():
        db = SessionLocal()
        try:
            s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
            if s and s.status == SessionStatus.pending:
                s.status = SessionStatus.expired
                s.closed_at = datetime.utcnow()
                db.commit()
                return s.machine_id
        finally:
            db.close()
    machine_id = await _db(_expire)
    if machine_id:
        await notify_kiosk(machine_id, {"type": "payment_timeout", "session_id": session_id})


# ─── DISPENSE FLOW ───────────────────────────────────────────────────────────

async def dispense(session_id: int, machine_id: str, slot_id: int, refund_on_fail: bool = True) -> dict:
    """Отправляет команду выдачи контроллеру, ждёт результат VMC.
    Возвращает {"ok": bool, "message": str}. При refund_on_fail=False (ручная
    выдача оператором) деньги при сбое не возвращаются — только помечаем ошибку."""
    async def _fail(reason: str) -> dict:
        if refund_on_fail:
            await _start_refund(session_id, reason)
        else:
            await _db(lambda: _mark_session_failed(session_id, reason))
        return {"ok": False, "message": reason}

    ws = machine_clients.get(machine_id)
    if ws is None:
        logging.error(f"Machine {machine_id} went offline before dispensing session {session_id}")
        return await _fail("machine offline")

    def _mark_dispensing():
        db = SessionLocal()
        try:
            s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
            if s:
                s.status = SessionStatus.dispensing
                db.commit()
        finally:
            db.close()
    await _db(_mark_dispensing)

    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _dispense_waiters[session_id] = future
    try:
        await ws.send_text(json.dumps({
            "type": "dispense",
            "session_id": session_id,
            "slot_id": slot_id,
        }))
        result = await asyncio.wait_for(future, timeout=settings.DISPENSE_TIMEOUT)
    except asyncio.TimeoutError:
        logging.error(f"Dispense timeout session {session_id} machine {machine_id}")
        return await _fail("dispense timeout")
    except Exception as e:
        logging.error(f"Dispense send failed session {session_id}: {e}")
        return await _fail("controller connection lost")
    finally:
        _dispense_waiters.pop(session_id, None)

    if result.get("success"):
        def _mark_dispensed():
            db = SessionLocal()
            try:
                s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
                if s:
                    s.status = SessionStatus.dispensed
                    s.closed_at = datetime.utcnow()
                    slot = db.query(ProductSlot).filter(
                        ProductSlot.machine_id == s.machine_id,
                        ProductSlot.slot_id == s.slot_id,
                    ).first()
                    if slot and slot.stock_qty > 0:
                        slot.stock_qty -= 1
                    db.commit()
            finally:
                db.close()
        await _db(_mark_dispensed)
        await notify_kiosk(machine_id, {"type": "dispensed", "session_id": session_id})
        logging.info(f"Session {session_id} dispensed OK")
        return {"ok": True, "message": "dispensed"}
    else:
        error = result.get("message", "dispense error")
        logging.warning(f"Session {session_id} dispense failed: {error}")
        return await _fail(error)


def _mark_session_failed(session_id: int, reason: str):
    db = SessionLocal()
    try:
        s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
        if s:
            s.status = SessionStatus.failed
            s.error = reason[:200]
            s.closed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


async def _start_refund(session_id: int, reason: str):
    """Сбой выдачи после оплаты → возврат денег через JetQR."""
    def _load():
        db = SessionLocal()
        try:
            s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
            if s:
                s.status = SessionStatus.refund_pending
                s.error = reason[:200]
                db.commit()
                return s.machine_id, s.invoice_id, s.transaction_id
            return None, None, None
        finally:
            db.close()
    machine_id, invoice_id, transaction_id = await _db(_load)
    if not invoice_id:
        return

    await notify_kiosk(machine_id, {
        "type": "refund_started", "session_id": session_id, "reason": reason,
    })

    result = await cancel_invoice(invoice_id, transaction_id or "")

    def _finalize():
        db = SessionLocal()
        try:
            s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
            if s:
                if result["success"]:
                    s.status = SessionStatus.refunded
                    s.closed_at = datetime.utcnow()
                # refund не прошёл — остаётся refund_pending, виден в админке
                db.commit()
        finally:
            db.close()
    await _db(_finalize)

    await notify_kiosk(machine_id, {
        "type": "refunded" if result["success"] else "refund_failed",
        "session_id": session_id,
    })


# ─── WEBSOCKET: контроллер точки (RS232-агент) ───────────────────────────────

@app.websocket("/ws/machine/{machine_id}")
async def machine_ws(websocket: WebSocket, machine_id: str):
    token = websocket.headers.get("x-machine-token") or websocket.query_params.get("token")

    def _auth():
        db = SessionLocal()
        try:
            m = db.query(VendingMachine).filter(
                VendingMachine.machine_id == machine_id,
                VendingMachine.is_active == True,
            ).first()
            if m is None:
                return False
            # Токен набирается руками на экранной клавиатуре: автозамена дефиса
            # на другое тире (—/–) или случайная смена регистра — обычное дело
            # и не должны считаться неверным токеном. Сверяем только буквы/цифры,
            # без разделителя и регистра. compare_digest на голых ASCII-строках —
            # никогда не бросает TypeError, в отличие от сырых строк с юникодом.
            expected = _normalize_token(m.secret_token)
            got = _normalize_token(token or "")
            return secrets.compare_digest(expected.encode(), got.encode())
        finally:
            db.close()

    if not await _db(_auth):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    machine_clients[machine_id] = websocket
    logging.info(f"Machine {machine_id} connected")
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "dispense_result":
                waiter = _dispense_waiters.get(msg.get("session_id"))
                if waiter and not waiter.done():
                    waiter.set_result(msg)
            elif msg.get("type") == "slot_state":
                waiter = _slot_check_waiters.get(msg.get("request_id"))
                if waiter and not waiter.done():
                    waiter.set_result(msg)
            # type=="pong"/прочее — игнорируем
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        if machine_clients.get(machine_id) is websocket:
            machine_clients.pop(machine_id, None)
        logging.info(f"Machine {machine_id} disconnected")


# ─── WEBSOCKET: kiosk-экран ──────────────────────────────────────────────────

@app.websocket("/ws/kiosk/{machine_id}")
async def kiosk_ws(websocket: WebSocket, machine_id: str):
    await websocket.accept()
    kiosk_clients.setdefault(machine_id, []).append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        lst = kiosk_clients.get(machine_id, [])
        if websocket in lst:
            lst.remove(websocket)


async def notify_kiosk(machine_id: str, data: dict):
    for ws in list(kiosk_clients.get(machine_id, [])):
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            pass


# ─── AUTH ────────────────────────────────────────────────────────────────────

def get_current_user(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> AdminUser:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "not authenticated")
    session = db.query(AdminSession).filter(AdminSession.token == token).first()
    if not session or session.expires_at < datetime.utcnow():
        raise HTTPException(401, "session expired")
    user = db.query(AdminUser).filter(AdminUser.id == session.user_id, AdminUser.is_active == True).first()
    if not user:
        raise HTTPException(401, "user not found or disabled")
    return user


def require_role(*roles: AdminRole):
    """Доступ только для перечисленных ролей. viewer — везде read-only,
    operator — оперативная работа, но не пользователи/настройки, admin — всё."""
    def dep(user: AdminUser = Depends(get_current_user)) -> AdminUser:
        if user.role not in roles:
            raise HTTPException(403, "insufficient role")
        return user
    return dep


require_admin = require_role(AdminRole.admin)
require_operator = require_role(AdminRole.admin, AdminRole.operator)
require_viewer = require_role(AdminRole.admin, AdminRole.operator, AdminRole.viewer)


@app.post("/api/admin/login")
async def login(data: dict, db: Session = Depends(get_db)):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    user = db.query(AdminUser).filter(AdminUser.username == username, AdminUser.is_active == True).first()
    if not user or not authlib.verify_password(password, user.password_hash):
        raise HTTPException(401, "invalid username or password")
    # Попутно чистим протухшие сессии, чтобы таблица не росла бесконечно.
    db.query(AdminSession).filter(AdminSession.expires_at < datetime.utcnow()).delete()
    token = authlib.new_session_token()
    db.add(AdminSession(token=token, user_id=user.id, expires_at=authlib.session_expiry()))
    db.commit()
    return {"token": token, "username": user.username, "role": user.role}


@app.post("/api/admin/logout", dependencies=[Depends(get_current_user)])
async def logout(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    token = authorization.removeprefix("Bearer ").strip()
    db.query(AdminSession).filter(AdminSession.token == token).delete()
    db.commit()
    return {"success": True}


@app.get("/api/admin/me")
async def me(user: AdminUser = Depends(get_current_user)):
    return {"username": user.username, "role": user.role}


# ─── USER MANAGEMENT (только admin) ──────────────────────────────────────────

@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
async def list_users(db: Session = Depends(get_db)):
    users = db.query(AdminUser).order_by(AdminUser.created_at).all()
    return [
        {"id": u.id, "username": u.username, "role": u.role, "is_active": u.is_active,
         "created_at": u.created_at.isoformat()}
        for u in users
    ]


@app.post("/api/admin/users", dependencies=[Depends(require_admin)])
async def create_user(data: dict, db: Session = Depends(get_db)):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role", AdminRole.operator)
    if not username or len(password) < 6:
        raise HTTPException(400, "username required, password must be at least 6 characters")
    if role not in (AdminRole.admin, AdminRole.operator, AdminRole.viewer):
        raise HTTPException(400, "invalid role")
    if db.query(AdminUser).filter(AdminUser.username == username).first():
        raise HTTPException(409, "username already exists")
    user = AdminUser(username=username, password_hash=authlib.hash_password(password), role=role)
    db.add(user)
    db.commit()
    return {"id": user.id, "username": user.username, "role": user.role}


@app.delete("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
async def delete_user(user_id: int, me: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    if user_id == me.id:
        raise HTTPException(400, "cannot delete your own account")
    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "user not found")
    remaining_admins = db.query(AdminUser).filter(
        AdminUser.role == AdminRole.admin, AdminUser.id != user_id).count()
    if user.role == AdminRole.admin and remaining_admins == 0:
        raise HTTPException(400, "cannot delete the last remaining admin")
    db.query(AdminSession).filter(AdminSession.user_id == user_id).delete()
    db.delete(user)
    db.commit()
    return {"success": True}


# ─── ADMIN API ───────────────────────────────────────────────────────────────

# Без 0/O/1/I/L (визуально путаются) и без гласных (случайно не складывается в
# слово) — код набирается руками на экранной клавиатуре планшета на точке.
_TOKEN_ALPHABET = "23456789BCDFGHJKMNPQRSTVWXYZ"
_TOKEN_LEN = 10  # ~48 бит энтропии — с запасом для одного WS-логина за попытку


def _generate_machine_token() -> str:
    code = "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LEN))
    return code[:5] + "-" + code[5:]  # XXXXX-XXXXX — легче читать и набирать


def _normalize_token(s: str) -> str:
    """Для сверки токена при подключении контроллера: дефис — просто
    визуальный разделитель, не часть секрета, а регистр клавиатура может
    менять сама. Оставляем только буквы/цифры и приводим к верхнему регистру."""
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


@app.post("/api/admin/machines", dependencies=[Depends(require_operator)])
async def add_machine(data: dict, db: Session = Depends(get_db)):
    machine_id = (data.get("machine_id") or "").strip()
    # machine_id попадает в URL киоска и путь WebSocket — только безопасные символы.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", machine_id):
        raise HTTPException(400, "machine_id: 1-50 chars, only letters, digits, '-' and '_'")
    if db.query(VendingMachine).filter(VendingMachine.machine_id == machine_id).first():
        raise HTTPException(409, "machine_id already exists")
    token = _generate_machine_token()
    m = VendingMachine(
        machine_id=machine_id,
        name=data.get("name"),
        location=data.get("location"),
        secret_token=token,
        jetqr_store_id=data.get("jetqr_store_id"),
        jetqr_terminal_id=data.get("jetqr_terminal_id"),
    )
    db.add(m)
    # Стандартный ассортимент сети — большинство точек продают один и тот же
    # набор товаров, поэтому сразу заполняем слоты по умолчанию (с ценой
    # default_price). Точечные исключения правятся потом руками на точке.
    defaults = db.query(Product).filter(
        Product.in_default_assortment == True).order_by(Product.id).all()
    for i, p in enumerate(defaults, start=1):
        db.add(ProductSlot(
            machine_id=machine_id, slot_id=i, product_id=p.id,
            product_name=p.name, image_url=p.image_url,
            price=p.default_price or 0, stock_qty=0, capacity=10,
        ))
    db.commit()
    # Токен показывается один раз — прошивается в контроллер точки
    return {"machine_id": m.machine_id, "secret_token": token}


@app.post("/api/admin/machines/{machine_id}/regenerate-token", dependencies=[Depends(require_operator)])
async def regenerate_machine_token(machine_id: str, db: Session = Depends(get_db)):
    """Перевыпуск токена — например, чтобы заменить старый длинный токен точки
    на новый короткий формат, или если токен мог утечь. Старое соединение
    контроллера (если было установлено) не разрывается немедленно, но при
    следующем переподключении по старому токену получит 401 — на планшете
    нужно сразу вписать новый через Настройки."""
    m = db.query(VendingMachine).filter(VendingMachine.machine_id == machine_id).first()
    if not m:
        raise HTTPException(404, "machine not found")
    m.secret_token = _generate_machine_token()
    db.commit()
    return {"machine_id": m.machine_id, "secret_token": m.secret_token}


@app.get("/api/admin/machines", dependencies=[Depends(require_viewer)])
async def list_machines(db: Session = Depends(get_db)):
    machines = db.query(VendingMachine).all()
    return [
        {
            "machine_id": m.machine_id,
            "name": m.name,
            "location": m.location,
            "is_active": m.is_active,
            "online": m.machine_id in machine_clients,
            "lat": m.lat,
            "lng": m.lng,
        }
        for m in machines
    ]


@app.post("/api/admin/machines/{machine_id}/location", dependencies=[Depends(require_operator)])
async def set_machine_location(machine_id: str, data: dict, db: Session = Depends(get_db)):
    """Задать координаты точки на карте (клик по карте в админке)."""
    m = db.query(VendingMachine).filter(VendingMachine.machine_id == machine_id).first()
    if not m:
        raise HTTPException(404, "machine not found")
    try:
        m.lat = float(data["lat"])
        m.lng = float(data["lng"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "lat and lng required")
    db.commit()
    return {"success": True}


@app.post("/api/admin/machines/{machine_id}", dependencies=[Depends(require_operator)])
async def update_machine(machine_id: str, data: dict, db: Session = Depends(get_db)):
    """Переименовать точку / поменять локацию (machine_id и токен так не трогаются —
    machine_id завязан на URL киоска и путь WebSocket, токен меняется отдельно
    через regenerate-token)."""
    m = db.query(VendingMachine).filter(VendingMachine.machine_id == machine_id).first()
    if not m:
        raise HTTPException(404, "machine not found")
    if "name" in data:
        m.name = (data["name"] or "").strip() or None
    if "location" in data:
        m.location = (data["location"] or "").strip() or None
    db.commit()
    return {"success": True}


@app.post("/api/admin/machines/{machine_id}/slots", dependencies=[Depends(require_operator)])
async def upsert_slot(machine_id: str, data: dict, db: Session = Depends(get_db)):
    try:
        slot_id = int(data["slot_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "slot_id required")
    # Если передан product_id — берём имя и фото из каталога (денормализуем на слот),
    # а цену/остаток задаём per-point из data (цена по умолчанию — из товара).
    product = None
    if data.get("product_id"):
        try:
            product = db.query(Product).filter(Product.id == int(data["product_id"])).first()
        except (TypeError, ValueError):
            raise HTTPException(400, "invalid product_id")

    slot = db.query(ProductSlot).filter(
        ProductSlot.machine_id == machine_id,
        ProductSlot.slot_id == slot_id,
    ).first()
    if slot:
        if product is not None:
            slot.product_id = product.id
            slot.product_name = product.name
            slot.image_url = product.image_url
        for field in ("product_name", "price", "stock_qty", "capacity", "image_url", "is_active"):
            if field in data:
                setattr(slot, field, data[field])
    else:
        name = product.name if product else (data.get("product_name") or "").strip()
        if not name:
            raise HTTPException(400, "product_id or product_name required for a new slot")
        try:
            slot = ProductSlot(
                machine_id=machine_id,
                slot_id=slot_id,
                product_id=product.id if product else None,
                product_name=name,
                image_url=product.image_url if product else data.get("image_url"),
                price=float(data["price"]) if data.get("price") not in (None, "") else float((product.default_price if product else 0) or 0),
                stock_qty=int(data.get("stock_qty", 0)),
                capacity=int(data.get("capacity", 10)),
            )
        except (TypeError, ValueError):
            raise HTTPException(400, "invalid numeric value in price/stock_qty/capacity")
        db.add(slot)
    db.commit()
    return {"success": True}


@app.delete("/api/admin/machines/{machine_id}/slots/{slot_id}", dependencies=[Depends(require_operator)])
async def delete_slot(machine_id: str, slot_id: int, db: Session = Depends(get_db)):
    slot = db.query(ProductSlot).filter(
        ProductSlot.machine_id == machine_id, ProductSlot.slot_id == slot_id).first()
    if not slot:
        raise HTTPException(404, "slot not found")
    db.delete(slot)
    db.commit()
    return {"success": True}


@app.get("/api/admin/machines/{machine_id}/slots", dependencies=[Depends(require_viewer)])
async def list_slots(machine_id: str, db: Session = Depends(get_db)):
    slots = db.query(ProductSlot).filter(
        ProductSlot.machine_id == machine_id).order_by(ProductSlot.slot_id).all()
    return [
        {
            "slot_id": s.slot_id,
            "product_id": s.product_id,
            "product_name": s.product_name,
            "image_url": s.image_url,
            "price": s.price,
            "stock_qty": s.stock_qty,
            "capacity": s.capacity,
            "is_active": s.is_active,
        }
        for s in slots
    ]


# ─── PRODUCTS (каталог товаров) ──────────────────────────────────────────────

@app.post("/api/admin/upload", dependencies=[Depends(require_operator)])
async def upload_image(data: dict):
    """Загрузка фото товара: JSON {filename, data_base64} → файл на диск → URL.
    base64-в-JSON вместо multipart, чтобы не тащить python-multipart зависимостью."""
    import base64
    raw = data.get("data_base64") or ""
    if "," in raw and raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1]  # срезаем префикс data:image/...;base64,
    try:
        blob = base64.b64decode(raw, validate=True)
    except Exception:
        raise HTTPException(400, "invalid base64 data")
    if not blob or len(blob) > settings.UPLOAD_MAX_BYTES:
        raise HTTPException(400, f"empty or too large (max {settings.UPLOAD_MAX_BYTES} bytes)")

    # Определяем расширение по сигнатуре файла — не доверяем присланному имени.
    ext = None
    if blob[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif blob[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif blob[:6] in (b"GIF87a", b"GIF89a"):
        ext = "gif"
    elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        ext = "webp"
    if ext is None:
        raise HTTPException(400, "unsupported image type (use JPG/PNG/GIF/WEBP)")

    name = f"{secrets.token_hex(16)}.{ext}"
    with open(os.path.join(settings.UPLOAD_DIR, name), "wb") as f:
        f.write(blob)
    return {"url": f"/uploads/{name}"}


@app.get("/api/admin/products", dependencies=[Depends(require_viewer)])
async def list_products(db: Session = Depends(get_db)):
    from sqlalchemy import func
    products = db.query(Product).order_by(Product.name).all()
    # сколько точек продаёт каждый товар
    counts = dict(
        db.query(ProductSlot.product_id, func.count(func.distinct(ProductSlot.machine_id)))
        .filter(ProductSlot.product_id.isnot(None))
        .group_by(ProductSlot.product_id).all()
    )
    return [
        {
            "id": p.id, "name": p.name, "category": p.category,
            "image_url": p.image_url, "default_price": p.default_price,
            "in_default_assortment": p.in_default_assortment,
            "points_count": int(counts.get(p.id, 0)),
        }
        for p in products
    ]


@app.post("/api/admin/products", dependencies=[Depends(require_operator)])
async def create_product(data: dict, db: Session = Depends(get_db)):
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    try:
        default_price = float(data["default_price"]) if data.get("default_price") not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "invalid default_price")
    p = Product(
        name=name,
        category=(data.get("category") or "").strip() or None,
        image_url=data.get("image_url"),
        default_price=default_price,
        in_default_assortment=bool(data.get("in_default_assortment", True)),
    )
    db.add(p)
    db.commit()
    return {"id": p.id, "name": p.name}


@app.post("/api/admin/products/{product_id}", dependencies=[Depends(require_operator)])
async def update_product(product_id: int, data: dict, db: Session = Depends(get_db)):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(404, "product not found")
    if "name" in data:
        p.name = (data["name"] or "").strip() or p.name
    if "category" in data:
        p.category = (data["category"] or "").strip() or None
    if "image_url" in data:
        p.image_url = data["image_url"]
    if "in_default_assortment" in data:
        p.in_default_assortment = bool(data["in_default_assortment"])
    if "default_price" in data:
        try:
            p.default_price = float(data["default_price"]) if data["default_price"] not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(400, "invalid default_price")
    # Изменение имени/фото товара распространяем на все привязанные слоты
    # (kiosk читает имя/фото со слота) — цену/остаток не трогаем.
    if "name" in data or "image_url" in data:
        for slot in db.query(ProductSlot).filter(ProductSlot.product_id == product_id).all():
            slot.product_name = p.name
            slot.image_url = p.image_url
    db.commit()
    return {"success": True}


@app.delete("/api/admin/products/{product_id}", dependencies=[Depends(require_operator)])
async def delete_product(product_id: int, db: Session = Depends(get_db)):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(404, "product not found")
    # Слоты не удаляем — только отвязываем (товар пропадёт из каталога, но слот
    # с текущими именем/ценой продолжит работать).
    for slot in db.query(ProductSlot).filter(ProductSlot.product_id == product_id).all():
        slot.product_id = None
    db.delete(p)
    db.commit()
    return {"success": True}


@app.get("/api/admin/products/{product_id}/placements", dependencies=[Depends(require_viewer)])
async def product_placements(product_id: int, db: Session = Depends(get_db)):
    """Где продаётся товар и по какой цене — для редактирования цены per-point."""
    slots = db.query(ProductSlot).filter(ProductSlot.product_id == product_id).all()
    machines = {m.machine_id: m for m in db.query(VendingMachine).all()}
    return [
        {
            "machine_id": s.machine_id,
            "machine_name": (machines.get(s.machine_id).name if machines.get(s.machine_id) else s.machine_id),
            "slot_id": s.slot_id,
            "price": s.price,
            "stock_qty": s.stock_qty,
        }
        for s in slots
    ]


@app.get("/api/admin/sessions", dependencies=[Depends(require_viewer)])
async def list_sessions(machine_id: str | None = None, db: Session = Depends(get_db)):
    q = db.query(VendingSession)
    if machine_id:
        q = q.filter(VendingSession.machine_id == machine_id)
    sessions = q.order_by(VendingSession.created_at.desc()).limit(200).all()
    return [
        {
            "id": s.id,
            "machine_id": s.machine_id,
            "slot_id": s.slot_id,
            "product_name": s.product_name,
            "amount": s.amount,
            "status": s.status,
            "phone_number": s.phone_number,
            "error": s.error,
            "created_at": s.created_at.isoformat(),
            "paid_at": s.paid_at.isoformat() if s.paid_at else None,
        }
        for s in sessions
    ]


@app.get("/api/admin/stats", dependencies=[Depends(require_viewer)])
async def stats(db: Session = Depends(get_db)):
    from sqlalchemy import func
    from datetime import timedelta
    today = datetime.utcnow().date()
    rows = db.query(
        VendingSession.machine_id,
        func.count().label("sales"),
        func.coalesce(func.sum(VendingSession.amount), 0).label("revenue"),
    ).filter(
        VendingSession.status == SessionStatus.dispensed,
        func.date(VendingSession.created_at) == today,
    ).group_by(VendingSession.machine_id).all()

    refunds_today = db.query(func.count()).filter(
        VendingSession.status.in_([SessionStatus.refunded, SessionStatus.refund_pending]),
        func.date(VendingSession.created_at) == today,
    ).scalar()

    # За 14 дней — отдельным сгруппированным запросом по всей таблице (не по
    # /api/admin/sessions, у которого лимит 200 строк: с ростом продаж график
    # начинал занижать старые дни, т.к. они просто не попадали в выборку).
    since = today - timedelta(days=13)
    daily_rows = db.query(
        func.date(VendingSession.created_at).label("day"),
        func.coalesce(func.sum(VendingSession.amount), 0).label("revenue"),
    ).filter(
        VendingSession.status == SessionStatus.dispensed,
        func.date(VendingSession.created_at) >= since,
    ).group_by(func.date(VendingSession.created_at)).all()
    daily_map = {
        (r.day if isinstance(r.day, str) else r.day.isoformat()): float(r.revenue)
        for r in daily_rows
    }
    daily_revenue = [
        {"date": (since + timedelta(days=i)).isoformat(),
         "revenue": daily_map.get((since + timedelta(days=i)).isoformat(), 0.0)}
        for i in range(14)
    ]

    return {
        "date": today.isoformat(),
        "machines_online": len(machine_clients),
        "refunds_today": refunds_today,
        "by_machine": [
            {"machine_id": r.machine_id, "sales": r.sales, "revenue": float(r.revenue)}
            for r in rows
        ],
        "total_revenue": float(sum(r.revenue for r in rows)),
        "daily_revenue": daily_revenue,
    }


@app.post("/api/admin/sessions/{session_id}/retry-refund", dependencies=[Depends(require_operator)])
async def retry_refund(session_id: int, db: Session = Depends(get_db)):
    """Повторить возврат для зависшей refund_pending сессии."""
    s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
    if not s or s.status != SessionStatus.refund_pending:
        raise HTTPException(404, "no refund-pending session")
    asyncio.create_task(_start_refund(session_id, s.error or "manual retry"))
    return {"success": True}


@app.post("/api/admin/machines/{machine_id}/force-dispense")
async def force_dispense(machine_id: str, data: dict, user: AdminUser = Depends(require_operator)):
    """Ручная выдача оператором: клиент оплатил, но товар не выпал — оператор
    открывает автомат в панели и выдаёт нужный слот удалённо. Без оплаты и без
    возврата при сбое (деньги уже получены), реальный результат возвращается."""
    if data.get("slot_id") in (None, ""):
        raise HTTPException(400, "slot_id required")
    slot_id = int(data["slot_id"])
    ws = machine_clients.get(machine_id)
    if ws is None:
        raise HTTPException(503, "machine offline")

    def _create():
        db = SessionLocal()
        try:
            slot = db.query(ProductSlot).filter(
                ProductSlot.machine_id == machine_id, ProductSlot.slot_id == slot_id).first()
            s = VendingSession(
                machine_id=machine_id,
                slot_id=slot_id,
                product_name=f"Ручная выдача · {user.username}" + (f" · {slot.product_name}" if slot else ""),
                amount=0,
                status=SessionStatus.paid,
            )
            db.add(s)
            db.commit()
            db.refresh(s)
            return s.id
        finally:
            db.close()
    session_id = await _db(_create)
    result = await dispense(session_id, machine_id, slot_id, refund_on_fail=False)
    return {"success": result["ok"], "message": result["message"], "session_id": session_id}


@app.get("/api/admin/blacklist", dependencies=[Depends(require_admin)])
async def list_blacklist(db: Session = Depends(get_db)):
    rows = db.query(Blacklist).order_by(Blacklist.added_at.desc()).all()
    return [
        {"id": b.id, "phone_number": b.phone_number, "reason": b.reason,
         "added_at": b.added_at.isoformat()}
        for b in rows
    ]


@app.post("/api/admin/blacklist", dependencies=[Depends(require_admin)])
async def add_blacklist(data: dict, db: Session = Depends(get_db)):
    phone = (data.get("phone_number") or "").strip()
    if not phone:
        raise HTTPException(400, "phone_number required")
    if db.query(Blacklist).filter(Blacklist.phone_number == phone).first():
        raise HTTPException(409, "phone already blacklisted")
    db.add(Blacklist(phone_number=phone, reason=data.get("reason")))
    db.commit()
    return {"success": True}


@app.delete("/api/admin/blacklist/{entry_id}", dependencies=[Depends(require_admin)])
async def remove_blacklist(entry_id: int, db: Session = Depends(get_db)):
    b = db.query(Blacklist).filter(Blacklist.id == entry_id).first()
    if not b:
        raise HTTPException(404, "entry not found")
    db.delete(b)
    db.commit()
    return {"success": True}


# ─── QR GENERATOR ────────────────────────────────────────────────────────────

@app.get("/api/qr/{invoice_id}")
async def generate_qr(invoice_id: str):
    import qrcode, io
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(invoice_id)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


# ─── FRONTEND ────────────────────────────────────────────────────────────────

os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")

frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    @app.get("/apk")
    async def download_apk():
        """Короткая ссылка для установки на планшет прямо из браузера точки —
        без QR (планшет закреплён в автомате, камерой не отсканировать)."""
        path = os.path.join(frontend_dir, "vending-kiosk.apk")
        if not os.path.exists(path):
            raise HTTPException(404, "APK not uploaded yet")
        return FileResponse(
            path,
            media_type="application/vnd.android.package-archive",
            filename="vending-kiosk.apk",
        )

    @app.get("/api/apk-version")
    async def apk_version():
        """Хэш текущего APK на сервере — приложение сверяет со своим на старте
        и само предлагает обновиться, если файл сменился. Хэш вместо номера
        версии — не нужно отдельно помнить бампнуть versionCode при каждой
        заливке новой сборки, простая замена файла уже меняет ответ."""
        path = os.path.join(frontend_dir, "vending-kiosk.apk")
        if not os.path.exists(path):
            raise HTTPException(404, "APK not uploaded yet")
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return {"sha256": h.hexdigest()}

    @app.get("/kiosk/{machine_id}", response_class=HTMLResponse)
    async def kiosk_page(machine_id: str):
        return FileResponse(os.path.join(frontend_dir, "kiosk.html"))

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page():
        return FileResponse(os.path.join(frontend_dir, "admin.html"))
