"""Оплата через ExpressPay / DCWallet (Dushanbe City) — альтернатива JetQR.

Две половины (по докам банка):
  1. QR на оплату — просто ссылка pay.expresspay.tj/?a=&s=&c=&f1=&f2=&f3=,
     которую рисуем в QR на своей стороне (запрос к банку не нужен).
  2. Статус оплаты — GET getpaystatus на api1.dc.tj/v3/vending с md5-подписью.

Наш order_id (= id сессии) кладём в комментарий (c) при генерации QR, и по
нему же спрашиваем статус — так платёж привязывается к конкретной покупке.
ВАЖНО: точный способ привязки order_id к платежу подтверждается у банка; тут
он положен в c. Endpoint статуса на момент интеграции отдавал 404 (нужен наш
IP в белом списке банка / подтверждение адреса) — сам URL берётся из config,
чтобы поправить без пересборки.
"""
from __future__ import annotations

import hashlib
import logging
import datetime
import urllib.parse

import httpx
from config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=8,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
    return _client


def _fmt_amount(amount: float) -> str:
    """Сумма: целую показываем без дробей (s=1), иначе как есть."""
    return str(int(amount)) if float(amount) == int(amount) else str(amount)


def build_qr_url(order_id: int | str, amount: float, comment: str | None = None) -> str:
    """Ссылка ExpressPay/DCWallet для QR. При скане клиента перебрасывает на
    страницу оплаты или открывает DCWallet. order_id кладём в c (комментарий) —
    по нему getpaystatus потом находит платёж."""
    c = comment if comment is not None else str(order_id)
    params = {
        "a": settings.EXPRESSPAY_CARD,     # pan — карта получателя
        "s": _fmt_amount(amount),          # сумма
        "c": c,                            # комментарий = наш order_id
        "f1": settings.EXPRESSPAY_ARTICLE, # артикул услуги (даёт Душанбе Сити)
        "f2": "",                          # не используется, но обязателен
        "f3": "",                          # не используется, но обязателен
    }
    return settings.EXPRESSPAY_QR_BASE.rstrip("/") + "/?" + urllib.parse.urlencode(params)


def _sign(inputdate: str, pan: str, order_id: str) -> str:
    """sign = md5(inputdate.pan.secret.order_id) — из докладной банка."""
    raw = f"{inputdate}{pan}{settings.EXPRESSPAY_SECRET}{order_id}"
    return hashlib.md5(raw.encode()).hexdigest()


async def get_pay_status(order_id: int | str, amount: float) -> dict:
    """getpaystatus. Возвращает форму, совместимую с poll_payment:
       {"paid": bool, "pending": bool, "phone": .., "amount": .., "bank": ..}.

    Ответ банка: code 200 (нашёл, есть status "Успешно оплачен"),
    102 (платежа пока нет), 101 (неверная подпись).
    Запрос активен только 1 минуту с момента оплаты — важно опрашивать
    непрерывно, пока висит экран оплаты."""
    pan = settings.EXPRESSPAY_CARD
    order_id = str(order_id)
    inputdate = datetime.datetime.now().strftime("%y%m%d%H%M%S")
    params = {
        "pan": pan,
        "order_id": order_id,
        "summa": _fmt_amount(amount),
        "inputdate": inputdate,
        "sign": _sign(inputdate, pan, order_id),
    }
    try:
        r = await get_client().get(settings.EXPRESSPAY_STATUS_URL, params=params)
        data = r.json()
    except Exception as e:
        logger.warning(f"expresspay get_pay_status error: {e}")
        return {"paid": False, "pending": True}

    code = str(data.get("code"))
    status = str(data.get("status") or "")
    if code == "200" and "оплач" in status.lower():
        return {
            "paid": True,
            "phone": data.get("phone"),
            "amount": data.get("summa"),
            "bank": "DCWallet",
            "transaction_id": None,   # в ответе getpaystatus нет id транзакции
            "raw": data,
        }
    if code == "102":
        return {"paid": False, "pending": True}     # платежа пока нет
    if code == "101":
        logger.error(f"expresspay: неверная подпись (101) order_id={order_id}")
        return {"paid": False, "pending": False, "error": True}
    # неизвестный ответ — считаем, что ещё ждём (не роняем сессию)
    logger.warning(f"expresspay: неожиданный ответ {data}")
    return {"paid": False, "pending": True}
