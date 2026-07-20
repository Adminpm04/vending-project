"""Диспетчер платёжного провайдера: JetQR (боевой) или ExpressPay/DCWallet.

Выбор — settings.PAYMENT_PROVIDER ("jetqr" | "expresspay"). Модуль даёт единый
интерфейс (create_invoice / check_invoice / cancel_invoice / qr_payload), а
внутри направляет в jetqr или expresspay. main.py работает только с этим слоем
и не знает, какой банк за ним.

Ключевая разница провайдеров:
  • JetQR — есть серверное создание инвойса; invoice_id генерит банк, и он же
    и содержимое QR, и ключ опроса статуса.
  • ExpressPay/DCWallet — создания инвойса нет: QR это просто URL, который мы
    рисуем сами, а ключ платежа = наш order_id (= id сессии). order_id кладётся
    в комментарий QR; по нему getpaystatus находит платёж (окно 1 минута с
    момента оплаты, поэтому опрашиваем непрерывно, пока висит экран).
"""
from __future__ import annotations

from config import settings
import jetqr
import expresspay


def _provider() -> str:
    return (settings.PAYMENT_PROVIDER or "jetqr").lower()


async def create_invoice(machine_id, slot_id, amount, *, session_id,
                         store_id=None, terminal_id=None) -> dict:
    if _provider() == "expresspay":
        # У ExpressPay нет серверного создания инвойса — просто фиксируем, что
        # ключ платежа = наш order_id (= id сессии). QR соберётся в qr_payload().
        return {
            "success": True,
            "invoice_id": str(session_id),
            "mis_payment_id": f"VND-{machine_id}-{slot_id}-{session_id}",
        }
    return await jetqr.create_invoice(
        machine_id, slot_id, amount, store_id=store_id, terminal_id=terminal_id)


async def check_invoice(invoice_id, amount=None) -> dict:
    if _provider() == "expresspay":
        # getpaystatus требует и order_id, и сумму (сумма входит в подпись).
        return await expresspay.get_pay_status(invoice_id, amount or 0)
    return await jetqr.check_invoice(invoice_id)


def qr_payload(invoice_id, amount=None) -> str:
    """Данные для отрисовки в QR. У JetQR это сам invoice_id, у ExpressPay —
    ссылка pay.expresspay.tj с суммой и order_id в комментарии."""
    if _provider() == "expresspay":
        return expresspay.build_qr_url(invoice_id, amount or 0)
    return invoice_id


async def cancel_invoice(invoice_id, transaction_id, payment_method=None) -> dict:
    if _provider() == "expresspay":
        # Способ возврата у DCWallet уточняется у банка — авто-возврата пока нет,
        # сессия остаётся refund_pending и оператор разбирается вручную.
        return {"success": False, "error": "expresspay refund not supported"}
    return await jetqr.cancel_invoice(invoice_id, transaction_id, payment_method)
