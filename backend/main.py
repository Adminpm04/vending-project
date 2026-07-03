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
import secrets

from database import (
    get_db, init_db, SessionLocal,
    VendingMachine, ProductSlot, VendingSession, Blacklist, SessionStatus,
)
from jetqr import create_invoice, check_invoice, cancel_invoice
from config import settings

app = FastAPI(title="Vending QR Payment System")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# machine_id -> WebSocket контроллера точки (RS232-агент)
machine_clients: dict[str, WebSocket] = {}
# machine_id -> WebSocket'ы kiosk-интерфейсов (экран может переподключаться)
kiosk_clients: dict[str, list[WebSocket]] = {}
# session_id -> Future с результатом выдачи от контроллера
_dispense_waiters: dict[int, asyncio.Future] = {}


async def _db(fn):
    """Синхронный SQLAlchemy-вызов вне event loop (thread pool)."""
    return await asyncio.get_event_loop().run_in_executor(None, fn)


# ─── STARTUP ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(_ws_heartbeat())


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


@app.post("/api/kiosk/{machine_id}/buy")
async def kiosk_buy(machine_id: str, data: dict, db: Session = Depends(get_db)):
    """Клиент выбрал товар: создаём сессию + инвойс, возвращаем QR."""
    slot_id = int(data["slot_id"])

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
    }


@app.get("/api/kiosk/session/{session_id}")
async def kiosk_session_status(session_id: int, db: Session = Depends(get_db)):
    s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "session not found")
    return {"session_id": s.id, "status": s.status, "error": s.error}


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

async def dispense(session_id: int, machine_id: str, slot_id: int):
    """Оплата подтверждена → шлём команду контроллеру, ждём результат VMC."""
    ws = machine_clients.get(machine_id)
    if ws is None:
        logging.error(f"Machine {machine_id} went offline before dispensing session {session_id}")
        await _start_refund(session_id, "machine offline")
        return

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
        await _start_refund(session_id, "dispense timeout")
        return
    except Exception as e:
        logging.error(f"Dispense send failed session {session_id}: {e}")
        await _start_refund(session_id, "controller connection lost")
        return
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
    else:
        error = result.get("message", "dispense error")
        logging.warning(f"Session {session_id} dispense failed: {error}")
        await _start_refund(session_id, error)


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
            return m is not None and secrets.compare_digest(m.secret_token, token or "")
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


# ─── ADMIN API ───────────────────────────────────────────────────────────────

def require_admin(x_admin_token: str = Header(default="")):
    if not secrets.compare_digest(x_admin_token, settings.ADMIN_TOKEN):
        raise HTTPException(401, "invalid admin token")


@app.post("/api/admin/machines", dependencies=[Depends(require_admin)])
async def add_machine(data: dict, db: Session = Depends(get_db)):
    token = secrets.token_urlsafe(32)
    m = VendingMachine(
        machine_id=data["machine_id"],
        name=data.get("name"),
        location=data.get("location"),
        secret_token=token,
        jetqr_store_id=data.get("jetqr_store_id"),
        jetqr_terminal_id=data.get("jetqr_terminal_id"),
    )
    db.add(m)
    db.commit()
    # Токен показывается один раз — прошивается в контроллер точки
    return {"machine_id": m.machine_id, "secret_token": token}


@app.get("/api/admin/machines", dependencies=[Depends(require_admin)])
async def list_machines(db: Session = Depends(get_db)):
    machines = db.query(VendingMachine).all()
    return [
        {
            "machine_id": m.machine_id,
            "name": m.name,
            "location": m.location,
            "is_active": m.is_active,
            "online": m.machine_id in machine_clients,
        }
        for m in machines
    ]


@app.post("/api/admin/machines/{machine_id}/slots", dependencies=[Depends(require_admin)])
async def upsert_slot(machine_id: str, data: dict, db: Session = Depends(get_db)):
    slot = db.query(ProductSlot).filter(
        ProductSlot.machine_id == machine_id,
        ProductSlot.slot_id == int(data["slot_id"]),
    ).first()
    if slot:
        for field in ("product_name", "price", "stock_qty", "capacity", "image_url", "is_active"):
            if field in data:
                setattr(slot, field, data[field])
    else:
        slot = ProductSlot(
            machine_id=machine_id,
            slot_id=int(data["slot_id"]),
            product_name=data["product_name"],
            price=float(data["price"]),
            stock_qty=int(data.get("stock_qty", 0)),
            capacity=int(data.get("capacity", 10)),
            image_url=data.get("image_url"),
        )
        db.add(slot)
    db.commit()
    return {"success": True}


@app.get("/api/admin/machines/{machine_id}/slots", dependencies=[Depends(require_admin)])
async def list_slots(machine_id: str, db: Session = Depends(get_db)):
    slots = db.query(ProductSlot).filter(
        ProductSlot.machine_id == machine_id).order_by(ProductSlot.slot_id).all()
    return [
        {
            "slot_id": s.slot_id,
            "product_name": s.product_name,
            "price": s.price,
            "stock_qty": s.stock_qty,
            "capacity": s.capacity,
            "is_active": s.is_active,
        }
        for s in slots
    ]


@app.get("/api/admin/sessions", dependencies=[Depends(require_admin)])
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


@app.get("/api/admin/stats", dependencies=[Depends(require_admin)])
async def stats(db: Session = Depends(get_db)):
    from sqlalchemy import func
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

    return {
        "date": today.isoformat(),
        "machines_online": len(machine_clients),
        "refunds_today": refunds_today,
        "by_machine": [
            {"machine_id": r.machine_id, "sales": r.sales, "revenue": float(r.revenue)}
            for r in rows
        ],
        "total_revenue": float(sum(r.revenue for r in rows)),
    }


@app.post("/api/admin/sessions/{session_id}/retry-refund", dependencies=[Depends(require_admin)])
async def retry_refund(session_id: int, db: Session = Depends(get_db)):
    """Повторить возврат для зависшей refund_pending сессии."""
    s = db.query(VendingSession).filter(VendingSession.id == session_id).first()
    if not s or s.status != SessionStatus.refund_pending:
        raise HTTPException(404, "no refund-pending session")
    asyncio.create_task(_start_refund(session_id, s.error or "manual retry"))
    return {"success": True}


@app.post("/api/admin/machines/{machine_id}/force-dispense", dependencies=[Depends(require_admin)])
async def force_dispense(machine_id: str, data: dict):
    """Ручная выдача оператором (форс-мажор). Без оплаты, только логируется."""
    ws = machine_clients.get(machine_id)
    if ws is None:
        raise HTTPException(503, "machine offline")

    def _create():
        db = SessionLocal()
        try:
            s = VendingSession(
                machine_id=machine_id,
                slot_id=int(data["slot_id"]),
                product_name="FORCE-DISPENSE",
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
    await dispense(session_id, machine_id, int(data["slot_id"]))
    return {"success": True, "session_id": session_id}


@app.post("/api/admin/blacklist", dependencies=[Depends(require_admin)])
async def add_blacklist(data: dict, db: Session = Depends(get_db)):
    db.add(Blacklist(phone_number=data["phone_number"], reason=data.get("reason")))
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

    @app.get("/kiosk/{machine_id}", response_class=HTMLResponse)
    async def kiosk_page(machine_id: str):
        return FileResponse(os.path.join(frontend_dir, "kiosk.html"))

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page():
        return FileResponse(os.path.join(frontend_dir, "admin.html"))
