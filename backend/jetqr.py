from __future__ import annotations

import httpx
import uuid
import asyncio
import logging
from config import settings

logger = logging.getLogger(__name__)

# Persistent client with connection pooling — reuses TCP connections
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=6,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
    return _client


def _headers() -> dict:
    return {
        "X-Api-Key": settings.JETQR_API_KEY,
        "Content-Type": "application/json",
    }


async def create_invoice(machine_id: str, slot_id: int, amount: float,
                         store_id: str | None = None,
                         terminal_id: str | None = None) -> dict:
    """Создать инвойс на конкретный товар. Сумма динамическая (цена товара).

    mis_payment_id содержит machine_id и slot_id — по нему можно восстановить
    привязку транзакции к автомату даже без нашей БД.
    """
    mis_payment_id = f"VND-{machine_id}-{slot_id}-{uuid.uuid4().hex[:8].upper()}"
    payload = {
        "merchant_id": settings.JETQR_MERCHANT_ID,
        "store_id": store_id or settings.JETQR_STORE_ID,
        "terminal_id": terminal_id or settings.JETQR_TERMINAL_ID,
        "mis_terminal_id": settings.JETQR_MIS_TERMINAL_ID,
        "mis_payment_id": mis_payment_id,
        "mis_amount": amount,
    }

    for attempt in range(3):
        try:
            response = await get_client().post(
                f"{settings.JETQR_BASE_URL}/api/v1/merchant/invoice",
                headers=_headers(),
                json=payload,
            )
            data = response.json()
            if data.get("type") == "SUCCESS":
                return {
                    "success": True,
                    "invoice_id": data["invoice_id"],
                    "mis_payment_id": mis_payment_id,
                }
            logger.warning(f"create_invoice attempt {attempt+1} failed: {data}")
        except Exception as e:
            logger.warning(f"create_invoice attempt {attempt+1} error: {e}")
        if attempt < 2:
            await asyncio.sleep(1)

    return {"success": False, "error": "max retries exceeded"}


async def check_invoice(invoice_id: str) -> dict:
    for attempt in range(2):
        try:
            response = await get_client().get(
                f"{settings.JETQR_BASE_URL}/api/v1/merchant/invoice",
                headers={"X-Api-Key": settings.JETQR_API_KEY},
                params={"invoiceId": invoice_id},
            )
            data = response.json()
        except Exception as e:
            logger.warning(f"check_invoice attempt {attempt+1} error: {e}")
            return {"paid": False, "pending": True}

        code = data.get("code")
        if code == 200:
            return {
                "paid": True,
                "phone": data.get("phone_number"),
                "amount": data.get("amount_arrived"),
                "bank": data.get("bank_name"),
                "transaction_id": data.get("transaction_id"),
            }
        elif code == 202:
            return {"paid": False, "pending": True}
        elif response.status_code >= 500 and attempt == 0:
            await asyncio.sleep(0.5)
            continue
        else:
            return {"paid": False, "pending": False, "error": True}
    return {"paid": False, "pending": False, "error": True}


async def cancel_invoice(invoice_id: str, transaction_id: str,
                         payment_method: str | None = None) -> dict:
    """Полная отмена оплаченного инвойса (возврат денег клиенту).

    Вызывается при сбое выдачи товара. cancellation_type=0 — полный возврат.
    Точный merchant-facing endpoint уточняется у JetQR/Aliftech —
    путь настраивается через JETQR_CANCEL_PATH.
    """
    payload = {
        "invoice_id": invoice_id,
        "transaction_id": transaction_id,
        "cancellation_type": 0,
    }
    if payment_method:
        payload["payment_method"] = payment_method

    for attempt in range(3):
        try:
            response = await get_client().post(
                f"{settings.JETQR_BASE_URL}{settings.JETQR_CANCEL_PATH}",
                headers=_headers(),
                json=payload,
            )
            data = response.json()
            if data.get("type") == "SUCCESS" or data.get("code") == 200:
                return {"success": True, "data": data}
            logger.warning(f"cancel_invoice attempt {attempt+1} failed: {data}")
        except Exception as e:
            logger.warning(f"cancel_invoice attempt {attempt+1} error: {e}")
        if attempt < 2:
            await asyncio.sleep(2)

    # Возврат не прошёл — сессия остаётся в refund_pending, оператор
    # разбирается вручную через админку.
    return {"success": False, "error": "max retries exceeded"}
