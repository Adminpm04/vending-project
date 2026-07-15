"""Контроллер точки ("Upper computer") — работает на планшете/Pi рядом с VMC.

Две задачи:
 1. RS232-цикл с VMC: отвечать на POLL в пределах 100 мс, слать команды выдачи.
 2. WebSocket к центральному серверу: получать "dispense", отправлять результат.

Запуск:  python3 controller.py
Настройки берутся из .env рядом с файлом (см. .env.example).

Требования: pyserial, websockets  (pip install pyserial websockets)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import serial  # pyserial
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import vmc_protocol as vmc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("controller")


def _load_env():
    path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

SERVER_URL = os.environ.get("SERVER_URL", "ws://localhost:8000")
MACHINE_ID = os.environ.get("MACHINE_ID", "VND-001")
MACHINE_TOKEN = os.environ.get("MACHINE_TOKEN", "")
SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyS1")
SERIAL_BAUD = int(os.environ.get("SERIAL_BAUD", "57600"))


class VMCLink:
    """RS232-связь с VMC: POLL/ACK-цикл, отправка команд, приём статусов.

    VMC опрашивает нас каждые ~200 мс; ответ должен уйти в течение 100 мс,
    поэтому чтение порта — в отдельном потоке, ответ на POLL — немедленный.
    """

    def __init__(self, port: str, baud: int, loop: asyncio.AbstractEventLoop):
        self._serial = serial.Serial(port, baud, bytesize=8, parity="N",
                                     stopbits=1, timeout=0.05)
        self._loop = loop
        self._parser = vmc.PacketParser()
        self._pack_no = 1
        self._outbox: list[bytes] = []       # команды, ждущие ближайшего POLL
        self._pending_ack: bytes | None = None
        self._retries = 0
        self.dispense_events: asyncio.Queue = asyncio.Queue()
        self.selection_events: asyncio.Queue = asyncio.Queue()
        self.elevator_events: asyncio.Queue = asyncio.Queue()
        self.menu_events: asyncio.Queue = asyncio.Queue()
        self.synced = False

    def _next_pack_no(self) -> int:
        no = self._pack_no
        self._pack_no = self._pack_no % 255 + 1
        return no

    def queue_dispense(self, slot_id: int):
        # Команда 0x06 (drive direct) с drop_sensor=1, elevator=0 — именно так
        # выдаёт товар рабочее заводское приложение dc.com.vending на этом железе
        # (проверено разбором его APK). Надёжнее, чем 0x03: явно включает датчик падения.
        self._outbox.append(
            vmc.build_drive_direct(self._next_pack_no(), slot_id,
                                   drop_sensor=True, elevator=False))

    def queue_check_selection(self, slot_id: int):
        # Команда 0x01 — спросить у VMC настоящий статус слота (датчик), не
        # трогая выдачу. Используется перед показом QR, чтобы не продавать
        # то, чего физически уже нет, даже если остаток в базе не обновили.
        self._outbox.append(vmc.build_check_selection(self._next_pack_no(), slot_id))

    def queue_elevator_status(self):
        # Команда 0x53 — статус лифта/дверцы выдачи, общий для всей машины
        # (не по слоту). Диагностика застрявшего товара без физического
        # вскрытия автомата.
        self._outbox.append(vmc.build_elevator_status_req(self._next_pack_no()))

    def queue_cancel_selection(self):
        # Команда 0x05 с selection=0x0000 — «отменить выбор». Сброс зависшего
        # внутреннего состояния VMC после серии сбоев подряд.
        self._outbox.append(vmc.build_cancel_selection(self._next_pack_no()))

    def queue_query_selection_number(self):
        # Меню-команда 0x70 (тип 0x41) — какие номера слотов реально знает VMC.
        self._outbox.append(vmc.build_query_selection_number(self._next_pack_no()))

    def queue_sync(self):
        self._outbox.append(vmc.build_sync(self._next_pack_no()))

    def run(self):
        """Блокирующий цикл чтения порта — запускать в отдельном потоке."""
        log.info(f"RS232 open {self._serial.port} @ {self._serial.baudrate}")
        while True:
            data = self._serial.read(64)
            if not data:
                continue
            for command, pack_no, text in self._parser.feed(data):
                self._handle(command, pack_no, text)

    def _handle(self, command: int, pack_no: int | None, text: bytes):
        if command == vmc.CMD_POLL:
            # На POLL отвечаем немедленно: команда из очереди или ACK
            if self._pending_ack is not None:
                # предыдущая команда не подтверждена — повторяем (до 5 раз)
                if self._retries < 5:
                    self._serial.write(self._pending_ack)
                    self._retries += 1
                else:
                    log.error("Command not ACKed after 5 retries — dropping")
                    self._pending_ack = None
                    self._retries = 0
                    self._serial.write(vmc.ACK_PACKET)
            elif self._outbox:
                packet = self._outbox.pop(0)
                self._serial.write(packet)
                self._pending_ack = packet
                self._retries = 0
            else:
                self._serial.write(vmc.ACK_PACKET)
            return

        if command == vmc.CMD_ACK:
            self._pending_ack = None
            self._retries = 0
            return

        # Любой информационный пакет от VMC подтверждаем ACK
        self._serial.write(vmc.ACK_PACKET)

        if command == vmc.CMD_SYNC:
            # VMC запросил синхронизацию — отвечаем тем же (требование протокола)
            self.queue_sync()
            self.synced = True
            log.info("VMC sync")
        elif command == vmc.CMD_DISPENSE_STATUS:
            status = vmc.parse_dispense_status(text)
            log.info(f"Dispense status: {status}")
            if status["kind"] != "in_progress":
                asyncio.run_coroutine_threadsafe(
                    self.dispense_events.put(status), self._loop)
        elif command == vmc.CMD_SELECTION_STATE:
            state = vmc.parse_selection_state(text)
            log.info(f"Selection state: {state}")
            asyncio.run_coroutine_threadsafe(
                self.selection_events.put(state), self._loop)
        elif command == vmc.CMD_ELEVATOR_STATUS:
            status = vmc.parse_elevator_status(text)
            log.info(f"Elevator status: {status}")
            asyncio.run_coroutine_threadsafe(
                self.elevator_events.put(status), self._loop)
        elif command == vmc.CMD_MENU_RESP:
            resp = vmc.parse_menu_response(text)
            log.info(f"Menu response: {resp}")
            asyncio.run_coroutine_threadsafe(
                self.menu_events.put(resp), self._loop)
        elif command == vmc.CMD_SLOT_INFO:
            pass  # цены/остатки ведёт сервер — информация VMC не используется
        else:
            log.debug(f"VMC cmd 0x{command:02X} text={text.hex()}")


async def ws_loop(link: VMCLink):
    """Держит WebSocket к серверу, реконнект с бэкоффом."""
    url = f"{SERVER_URL}/ws/machine/{MACHINE_ID}?token={MACHINE_TOKEN}"
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                log.info(f"Connected to server as {MACHINE_ID}")
                backoff = 1
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if msg.get("type") == "ping":
                        await ws.send('{"type":"pong"}')
                    elif msg.get("type") == "dispense":
                        asyncio.create_task(
                            handle_dispense(link, ws, msg["session_id"], msg["slot_id"]))
                    elif msg.get("type") == "check_slot":
                        asyncio.create_task(
                            handle_check_slot(link, ws, msg["request_id"], msg["slot_id"]))
                    elif msg.get("type") == "check_elevator":
                        asyncio.create_task(
                            handle_check_elevator(link, ws, msg["request_id"]))
                    elif msg.get("type") == "cancel_selection":
                        asyncio.create_task(
                            handle_cancel_selection(link, ws, msg["request_id"]))
                    elif msg.get("type") == "query_selection_number":
                        asyncio.create_task(
                            handle_query_selection_number(link, ws, msg["request_id"]))
        except Exception as e:
            log.warning(f"WS disconnected: {e}; retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def handle_dispense(link: VMCLink, ws, session_id: int, slot_id: int):
    """Команда выдачи от сервера → RS232 → результат обратно серверу."""
    log.info(f"Dispense request: session={session_id} slot={slot_id}")

    # Очищаем старые события, чтобы не поймать статус прошлой выдачи
    while not link.dispense_events.empty():
        link.dispense_events.get_nowait()

    link.queue_dispense(slot_id)

    # Статус (0x04) несёт свой selection number — если предыдущий запрос
    # задержался и его финальный статус пришёл только сейчас, после того как
    # мы уже отправили команду для ДРУГОГО слота, наивный get() принял бы
    # чужой ответ за наш. Отбрасываем несовпадающие по слоту события и ждём
    # дальше — до общего дедлайна, а не одним ожиданием с полным таймаутом.
    # slot=0 — отдельный случай, не "чужой ответ": для трёхзначных селекций
    # (ряд+позиция, напр. 101) эта прошивка VMC в финальном статусе выдачи
    # всегда возвращает 0 вместо реального номера (обнаружено на живом
    # тесте — 0x02/0x24 с slot=0 отбрасывались как stale, и настоящая
    # успешная выдача репортилась как timeout).
    deadline = asyncio.get_event_loop().time() + 45
    status = None
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            status = {"kind": "error", "code": None, "message": "VMC response timeout"}
            break
        try:
            ev = await asyncio.wait_for(link.dispense_events.get(), timeout=remaining)
        except asyncio.TimeoutError:
            status = {"kind": "error", "code": None, "message": "VMC response timeout"}
            break
        if ev.get("slot") is not None and ev["slot"] != 0 and ev["slot"] != slot_id:
            log.warning(f"Dispense status for slot {ev['slot']}, expected {slot_id} (session={session_id}) — stale, ignoring")
            continue
        status = ev
        break

    result = {
        "type": "dispense_result",
        "session_id": session_id,
        "success": status["kind"] == "success",
        "code": status.get("code"),
        "message": status.get("message"),
    }
    log.info(f"Dispense result: {result}")
    await ws.send(json.dumps(result))


async def handle_check_slot(link: VMCLink, ws, request_id: str, slot_id: int):
    """Запрос сервера "есть ли реально товар в слоте?" → RS232 → ответ обратно."""
    log.info(f"Check-slot request: request_id={request_id} slot={slot_id}")

    while not link.selection_events.empty():
        link.selection_events.get_nowait()

    link.queue_check_selection(slot_id)

    deadline = asyncio.get_event_loop().time() + 3
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            result = {
                "type": "slot_state", "request_id": request_id,
                "checked": False, "ok": True, "message": "VMC response timeout",
            }
            break
        try:
            state = await asyncio.wait_for(link.selection_events.get(), timeout=remaining)
        except asyncio.TimeoutError:
            result = {
                "type": "slot_state", "request_id": request_id,
                "checked": False, "ok": True, "message": "VMC response timeout",
            }
            break
        if state.get("slot") is not None and state["slot"] != slot_id:
            log.warning(f"Selection state for slot {state['slot']}, expected {slot_id} (request={request_id}) — stale, ignoring")
            continue
        result = {
            "type": "slot_state", "request_id": request_id,
            "checked": True, "ok": state["ok"], "message": state.get("message"),
        }
        break
    log.info(f"Slot state: {result}")
    await ws.send(json.dumps(result))


async def handle_check_elevator(link: VMCLink, ws, request_id: str):
    """Запрос сервера "статус лифта/дверцы" → RS232 → ответ обратно."""
    log.info(f"Check-elevator request: request_id={request_id}")

    while not link.elevator_events.empty():
        link.elevator_events.get_nowait()

    link.queue_elevator_status()

    try:
        status = await asyncio.wait_for(link.elevator_events.get(), timeout=3)
        result = {
            "type": "elevator_state", "request_id": request_id,
            "checked": True, "ok": status["ok"], "message": status.get("message"),
        }
    except asyncio.TimeoutError:
        result = {
            "type": "elevator_state", "request_id": request_id,
            "checked": False, "ok": True, "message": "VMC response timeout",
        }
    log.info(f"Elevator state: {result}")
    await ws.send(json.dumps(result))


async def handle_cancel_selection(link: VMCLink, ws, request_id: str):
    """Запрос сервера "отменить выбор" (0x05) — без содержательного ответа
    от VMC, только подтверждение отправки."""
    log.info(f"Cancel-selection request: request_id={request_id}")
    if not link._serial.is_open:
        result = {"type": "cancel_selection_result", "request_id": request_id,
                  "sent": False, "message": "Serial port not open"}
    else:
        link.queue_cancel_selection()
        result = {"type": "cancel_selection_result", "request_id": request_id,
                  "sent": True, "message": "Command queued"}
    log.info(f"Cancel selection: {result}")
    await ws.send(json.dumps(result))


async def handle_query_selection_number(link: VMCLink, ws, request_id: str):
    """Меню-запрос 0x70/0x41 — какие номера слотов реально знает VMC."""
    log.info(f"Query-selection-number request: request_id={request_id}")

    while not link.menu_events.empty():
        link.menu_events.get_nowait()

    link.queue_query_selection_number()

    try:
        resp = await asyncio.wait_for(link.menu_events.get(), timeout=3)
        result = {
            "type": "menu_response", "request_id": request_id, "checked": True,
            "command_type": resp.get("command_type"), "operation_type": resp.get("operation_type"),
            "raw_hex": resp.get("raw_hex"),
        }
    except asyncio.TimeoutError:
        result = {"type": "menu_response", "request_id": request_id,
                  "checked": False, "message": "VMC response timeout"}
    log.info(f"Menu response: {result}")
    await ws.send(json.dumps(result))


async def main():
    loop = asyncio.get_event_loop()
    link = VMCLink(SERIAL_PORT, SERIAL_BAUD, loop)
    link.queue_sync()  # протокол требует 0x31 при старте Upper computer
    loop.run_in_executor(None, link.run)
    await ws_loop(link)


if __name__ == "__main__":
    asyncio.run(main())
