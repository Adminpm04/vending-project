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
        self.synced = False

    def _next_pack_no(self) -> int:
        no = self._pack_no
        self._pack_no = self._pack_no % 255 + 1
        return no

    def queue_dispense(self, slot_id: int):
        self._outbox.append(vmc.build_buy(self._next_pack_no(), slot_id))

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

    try:
        status = await asyncio.wait_for(link.dispense_events.get(), timeout=45)
    except asyncio.TimeoutError:
        status = {"kind": "error", "code": None, "message": "VMC response timeout"}

    result = {
        "type": "dispense_result",
        "session_id": session_id,
        "success": status["kind"] == "success",
        "code": status.get("code"),
        "message": status.get("message"),
    }
    log.info(f"Dispense result: {result}")
    await ws.send(json.dumps(result))


async def main():
    loop = asyncio.get_event_loop()
    link = VMCLink(SERIAL_PORT, SERIAL_BAUD, loop)
    link.queue_sync()  # протокол требует 0x31 при старте Upper computer
    loop.run_in_executor(None, link.run)
    await ws_loop(link)


if __name__ == "__main__":
    asyncio.run(main())
