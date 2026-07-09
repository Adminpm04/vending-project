"""Протокол XY-Vending "VMC - Upper computer" V3.0 (RS232, 57600 8N1).

VMC — хост: шлёт POLL каждые ~200 мс, мы (Upper computer) обязаны ответить
в течение 100 мс — либо ACK (нет команд), либо командой из очереди.

Формат пакета:
    STX(2) | Command(1) | Length(1) | PackNO+Text(n) | XOR(1)
    STX = 0xFA 0xFB;  Length = len(PackNO+Text);
    XOR — контрольная сумма от STX до Text включительно.
POLL и ACK — без PackNO (Length = 0).
"""
from __future__ import annotations

STX = bytes([0xFA, 0xFB])

# Команды
CMD_CHECK_SELECTION = 0x01   # мы → VMC: проверить исправность слота
CMD_SELECTION_STATE = 0x02   # VMC → мы: ответ на проверку слота
CMD_BUY = 0x03               # мы → VMC: выбрать слот на выдачу
CMD_DISPENSE_STATUS = 0x04   # VMC → мы: статус выдачи в реальном времени
CMD_SELECT_CANCEL = 0x05     # обе стороны: выбор/отмена выбора (0x0000 = отмена)
CMD_DRIVE_DIRECT = 0x06      # мы → VMC: выдача с явным управлением drop-sensor/elevator
CMD_SLOT_INFO = 0x11         # VMC → мы: цена/остаток/ёмкость/ID слота
CMD_SYNC = 0x31              # обе стороны: синхронизация при старте (обязательна)
CMD_POLL = 0x41              # VMC → мы: опрос
CMD_ACK = 0x42               # обе стороны: подтверждение
CMD_MACHINE_STATUS_REQ = 0x51  # мы → VMC: запросить статус машины
CMD_MACHINE_STATUS = 0x52      # VMC → мы: статус машины

POLL_PACKET = bytes([0xFA, 0xFB, 0x41, 0x00, 0x40])
ACK_PACKET = bytes([0xFA, 0xFB, 0x42, 0x00, 0x43])

# Статусы выдачи (Text[0] пакета 0x04) — полная таблица из VMC-Upper computer_V3.0.pdf,
# раздел 4.3.3. У этой прошивки VMC два разных кода успеха: 0x02 (обычная выдача)
# и 0x24 (после «положил в лоток микроволновки» — используется даже автоматами
# без микроволновки/лотка, судя по живому тесту на реальном железе). Раньше 0x24
# не был известен коду и ошибочно считался сбоем (найдено на живом тесте: слоты
# реально выдали товар, а мы доложили «ошибка»). Заодно добавлены остальные
# промежуточные статусы (двери/лоток микроволновки) как in_progress — по той же
# причине: неизвестный код иначе трактуется как терминальная ошибка и обрывает
# ожидание раньше времени, хотя выдача может ещё продолжаться.
DISPENSE_IN_PROGRESS = {0x01, 0x10, 0x11, 0x14, 0x16, 0x18, 0x19, 0x21, 0x22, 0x23, 0x26}
DISPENSE_SUCCESS = {0x02, 0x24}
DISPENSE_TERMINATED = 0xFF

DISPENSE_ERRORS = {
    0x03: "Selection jammed",
    0x04: "Motor doesn't stop normally",
    0x06: "Motor doesn't exist",
    0x07: "Elevator error",
    0x12: "Elevator ascending error",
    0x13: "Elevator descending error",
    0x15: "Microwave delivery door closing error",
    0x17: "Microwave inlet door opening error",
    0x20: "Microwave inlet door closing error",
    0x25: "Staypole return error",
    0x28: "Staypole push error",
    0x29: "Elevator entering microwave oven error",
    0x30: "Elevator exiting microwave oven error",
    0x31: "Pushrod pushing error in microwave oven",
    0x32: "Pushrod returning error in microwave oven",
    0xFF: "Purchase terminated",
}

# Статусы проверки слота (Text[0] пакета 0x02)
SELECTION_OK = 0x01
SELECTION_STATES = {
    0x01: "Normal",
    0x02: "Out of stock",
    0x03: "Selection doesn't exist",
    0x04: "Selection pause",
    0x05: "Product inside elevator",
    0x06: "Delivery door unlocked",
    0x07: "Elevator error",
    0x08: "Elevator self-checking faulty",
    0x16: "Staypole return error",
    0x17: "Main motor fault",
    0x18: "Translation motor fault",
    0x19: "Staypole push error",
}


def xor_checksum(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


def build_packet(command: int, pack_no: int | None = None, text: bytes = b"") -> bytes:
    """Собрать пакет. Для POLL/ACK pack_no=None (Length=0, без PackNO)."""
    body = b"" if pack_no is None else bytes([pack_no]) + text
    head = STX + bytes([command, len(body)]) + body
    return head + bytes([xor_checksum(head)])


def build_buy(pack_no: int, slot_id: int) -> bytes:
    """0x03 — выдать товар из слота (selection number, 2 байта big-endian)."""
    return build_packet(CMD_BUY, pack_no, slot_id.to_bytes(2, "big"))


def build_check_selection(pack_no: int, slot_id: int) -> bytes:
    """0x01 — проверить исправность слота перед покупкой."""
    return build_packet(CMD_CHECK_SELECTION, pack_no, slot_id.to_bytes(2, "big"))


def build_drive_direct(pack_no: int, slot_id: int,
                       drop_sensor: bool = True, elevator: bool = False) -> bytes:
    """0x06 — выдача с явным включением drop-sensor / лифта."""
    text = bytes([1 if drop_sensor else 0, 1 if elevator else 0]) + slot_id.to_bytes(2, "big")
    return build_packet(CMD_DRIVE_DIRECT, pack_no, text)


def build_sync(pack_no: int) -> bytes:
    """0x31 — синхронизация. Обязательна при старте Upper computer;
    также отправляется в ответ на 0x31 от VMC."""
    return build_packet(CMD_SYNC, pack_no)


class PacketParser:
    """Инкрементальный разбор байтового потока от VMC.

    feed() принимает очередной кусок данных, возвращает список полных
    валидных пакетов вида (command, pack_no|None, text).
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, int | None, bytes]]:
        self._buf.extend(data)
        packets = []
        while True:
            start = self._buf.find(STX)
            if start < 0:
                # мусор без STX — оставляем последний байт (вдруг это 0xFA)
                del self._buf[:-1]
                break
            if start > 0:
                del self._buf[:start]
            if len(self._buf) < 5:
                break  # ждём минимум STX+cmd+len+xor
            length = self._buf[3]
            total = 4 + length + 1
            if len(self._buf) < total:
                break
            frame = bytes(self._buf[:total])
            del self._buf[:total]
            if xor_checksum(frame[:-1]) != frame[-1]:
                continue  # битый пакет — пропускаем, VMC перешлёт
            command = frame[2]
            if length == 0:
                packets.append((command, None, b""))
            else:
                packets.append((command, frame[4], frame[5:-1]))
        return packets


def parse_dispense_status(text: bytes) -> dict:
    """Разобрать Text пакета 0x04: Status(1) + selection(2) [+ microwave(1)]."""
    status = text[0]
    slot = int.from_bytes(text[1:3], "big") if len(text) >= 3 else None
    if status in DISPENSE_SUCCESS:
        kind = "success"
    elif status in DISPENSE_IN_PROGRESS:
        kind = "in_progress"
    else:
        kind = "error"
    return {
        "kind": kind,
        "code": status,
        "slot": slot,
        "message": DISPENSE_ERRORS.get(status, f"status 0x{status:02X}"),
    }


def parse_selection_state(text: bytes) -> dict:
    """Разобрать Text пакета 0x02: State(1) + selection(2)."""
    state = text[0]
    slot = int.from_bytes(text[1:3], "big") if len(text) >= 3 else None
    return {
        "ok": state == SELECTION_OK,
        "code": state,
        "slot": slot,
        "message": SELECTION_STATES.get(state, f"state 0x{state:02X}"),
    }
