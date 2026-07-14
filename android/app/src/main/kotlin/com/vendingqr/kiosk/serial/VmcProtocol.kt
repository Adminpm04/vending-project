package com.vendingqr.kiosk.serial

/**
 * Протокол XY-Vending "VMC - Upper computer" V3.0 (RS232, 57600 8N1).
 * 1:1 порт backend/vmc_protocol.py — держать в синхроне при изменении протокола.
 *
 * VMC — хост: шлёт POLL каждые ~200 мс, мы (Upper computer) обязаны ответить
 * в течение 100 мс — либо ACK (нет команд), либо командой из очереди.
 *
 * Формат пакета: STX(2) | Command(1) | Length(1) | PackNO+Text(n) | XOR(1)
 * STX = 0xFA 0xFB; Length = len(PackNO+Text); XOR — от STX до Text включительно.
 * POLL и ACK — без PackNO (Length = 0).
 */
object VmcProtocol {

    val STX = byteArrayOf(0xFA.toByte(), 0xFB.toByte())

    const val CMD_CHECK_SELECTION = 0x01
    const val CMD_SELECTION_STATE = 0x02
    const val CMD_BUY = 0x03
    const val CMD_DISPENSE_STATUS = 0x04
    const val CMD_SELECT_CANCEL = 0x05
    const val CMD_DRIVE_DIRECT = 0x06
    const val CMD_SLOT_INFO = 0x11
    const val CMD_SYNC = 0x31
    const val CMD_POLL = 0x41
    const val CMD_ACK = 0x42
    const val CMD_MACHINE_STATUS_REQ = 0x51
    const val CMD_MACHINE_STATUS = 0x52
    const val CMD_ELEVATOR_STATUS_REQ = 0x53
    const val CMD_ELEVATOR_STATUS = 0x54

    val POLL_PACKET = byteArrayOf(0xFA.toByte(), 0xFB.toByte(), 0x41, 0x00, 0x40)
    val ACK_PACKET = byteArrayOf(0xFA.toByte(), 0xFB.toByte(), 0x42, 0x00, 0x43)

    // Полная таблица из VMC-Upper computer_V3.0.pdf, разд. 4.3.3 — см. пояснение
    // в backend/vmc_protocol.py у тех же констант (1:1 порт).
    private val DISPENSE_IN_PROGRESS = setOf(0x01, 0x10, 0x11, 0x14, 0x16, 0x18, 0x19, 0x21, 0x22, 0x23, 0x26)
    private val DISPENSE_SUCCESS = setOf(0x02, 0x24)

    private val DISPENSE_ERRORS = mapOf(
        0x03 to "Selection jammed",
        0x04 to "Motor doesn't stop normally",
        0x06 to "Motor doesn't exist",
        0x07 to "Elevator error",
        0x12 to "Elevator ascending error",
        0x13 to "Elevator descending error",
        0x15 to "Microwave delivery door closing error",
        0x17 to "Microwave inlet door opening error",
        0x20 to "Microwave inlet door closing error",
        0x25 to "Staypole return error",
        0x28 to "Staypole push error",
        0x29 to "Elevator entering microwave oven error",
        0x30 to "Elevator exiting microwave oven error",
        0x31 to "Pushrod pushing error in microwave oven",
        0x32 to "Pushrod returning error in microwave oven",
        0xFF to "Purchase terminated",
    )

    private const val SELECTION_OK = 0x01
    private val SELECTION_STATES = mapOf(
        0x01 to "Normal",
        0x02 to "Out of stock",
        0x03 to "Selection doesn't exist",
        0x04 to "Selection pause",
        0x05 to "Product inside elevator",
        0x06 to "Delivery door unlocked",
        0x07 to "Elevator error",
        0x08 to "Elevator self-checking faulty",
        0x16 to "Staypole return error",
        0x17 to "Main motor fault",
        0x18 to "Translation motor fault",
        0x19 to "Staypole push error",
    )

    // Статусы лифта/дверцы выдачи (Text[0] пакета 0x54) — общие для всей
    // машины, не завязаны на конкретный слот. Диагностика застрявшего товара.
    private val ELEVATOR_STATES = mapOf(
        0x00 to "Normal",
        0x01 to "Product stuck in elevator",
        0x02 to "Delivery door not closed",
        0x03 to "Elevator error",
        0x04 to "Elevator self-checking error",
    )

    fun xorChecksum(data: ByteArray, offset: Int = 0, length: Int = data.size): Int {
        var x = 0
        for (i in offset until offset + length) {
            x = x xor (data[i].toInt() and 0xFF)
        }
        return x and 0xFF
    }

    private fun buildPacket(command: Int, packNo: Int?, text: ByteArray = ByteArray(0)): ByteArray {
        val body = if (packNo == null) ByteArray(0) else byteArrayOf(packNo.toByte()) + text
        val head = STX + byteArrayOf(command.toByte(), body.size.toByte()) + body
        return head + byteArrayOf(xorChecksum(head).toByte())
    }

    /** 0x03 — выдать товар из слота (selection number, 2 байта big-endian). */
    fun buildBuy(packNo: Int, slotId: Int): ByteArray {
        val text = byteArrayOf((slotId shr 8).toByte(), (slotId and 0xFF).toByte())
        return buildPacket(CMD_BUY, packNo, text)
    }

    /** 0x01 — проверить исправность слота перед покупкой. */
    fun buildCheckSelection(packNo: Int, slotId: Int): ByteArray {
        val text = byteArrayOf((slotId shr 8).toByte(), (slotId and 0xFF).toByte())
        return buildPacket(CMD_CHECK_SELECTION, packNo, text)
    }

    /** 0x05 с selection=0x0000 — «отменить выбор» (по умолчанию так трактуется
     * VMC, когда эту команду шлёт upper computer). Сброс зависшего внутреннего
     * состояния выбора после серии сбоев подряд. */
    fun buildCancelSelection(packNo: Int): ByteArray = buildPacket(CMD_SELECT_CANCEL, packNo, byteArrayOf(0, 0))

    /**
     * 0x06 — выдача с явным включением drop-sensor / лифта. Это команда, которую
     * реально использует проверенное заводское приложение на этом железе
     * (drop_sensor=1, elevator=0) — надёжнее общего 0x03.
     */
    fun buildDriveDirect(packNo: Int, slotId: Int, dropSensor: Boolean = true, elevator: Boolean = false): ByteArray {
        val text = byteArrayOf(
            if (dropSensor) 1 else 0,
            if (elevator) 1 else 0,
            (slotId shr 8).toByte(),
            (slotId and 0xFF).toByte(),
        )
        return buildPacket(CMD_DRIVE_DIRECT, packNo, text)
    }

    /** 0x31 — синхронизация. Обязательна при старте Upper computer. */
    fun buildSync(packNo: Int): ByteArray = buildPacket(CMD_SYNC, packNo)

    /** 0x53 — запросить статус лифта/дверцы выдачи (общий, не по слоту). */
    fun buildElevatorStatusReq(packNo: Int): ByteArray = buildPacket(CMD_ELEVATOR_STATUS_REQ, packNo)

    data class ElevatorStatus(val ok: Boolean, val code: Int, val message: String)

    fun parseElevatorStatus(text: ByteArray): ElevatorStatus {
        val code = text[0].toInt() and 0xFF
        return ElevatorStatus(code == 0x00, code, ELEVATOR_STATES[code] ?: "status 0x%02X".format(code))
    }

    data class DispenseStatus(val kind: Kind, val code: Int, val slot: Int?, val message: String) {
        enum class Kind { IN_PROGRESS, SUCCESS, ERROR }
    }

    fun parseDispenseStatus(text: ByteArray): DispenseStatus {
        val status = text[0].toInt() and 0xFF
        val slot = if (text.size >= 3) {
            ((text[1].toInt() and 0xFF) shl 8) or (text[2].toInt() and 0xFF)
        } else null
        val kind = when {
            DISPENSE_SUCCESS.contains(status) -> DispenseStatus.Kind.SUCCESS
            DISPENSE_IN_PROGRESS.contains(status) -> DispenseStatus.Kind.IN_PROGRESS
            else -> DispenseStatus.Kind.ERROR
        }
        val message = DISPENSE_ERRORS[status] ?: "status 0x%02X".format(status)
        return DispenseStatus(kind, status, slot, message)
    }

    data class SelectionState(val ok: Boolean, val code: Int, val slot: Int?, val message: String)

    fun parseSelectionState(text: ByteArray): SelectionState {
        val state = text[0].toInt() and 0xFF
        val slot = if (text.size >= 3) {
            ((text[1].toInt() and 0xFF) shl 8) or (text[2].toInt() and 0xFF)
        } else null
        val message = SELECTION_STATES[state] ?: "state 0x%02X".format(state)
        return SelectionState(state == SELECTION_OK, state, slot, message)
    }

    data class Packet(val command: Int, val packNo: Int?, val text: ByteArray)

    /**
     * Инкрементальный разбор байтового потока от VMC.
     * feed() принимает очередной кусок данных, возвращает список полных валидных пакетов.
     */
    class PacketParser {
        private val buf = ArrayDeque<Byte>()

        fun feed(data: ByteArray): List<Packet> {
            buf.addAll(data.toList())
            val packets = mutableListOf<Packet>()

            while (true) {
                val startIdx = findStx()
                if (startIdx < 0) {
                    // мусор без STX — оставляем последний байт (вдруг это 0xFA)
                    while (buf.size > 1) buf.removeFirst()
                    break
                }
                repeat(startIdx) { buf.removeFirst() }
                if (buf.size < 5) break // ждём минимум STX+cmd+len+xor

                val frame = buf.toList()
                val length = frame[3].toInt() and 0xFF
                val total = 4 + length + 1
                if (frame.size < total) break

                val frameBytes = frame.subList(0, total).toByteArray()
                repeat(total) { buf.removeFirst() }

                if (xorChecksum(frameBytes, 0, frameBytes.size - 1) != (frameBytes.last().toInt() and 0xFF)) {
                    continue // битый пакет — пропускаем, VMC перешлёт
                }
                val command = frameBytes[2].toInt() and 0xFF
                if (length == 0) {
                    packets.add(Packet(command, null, ByteArray(0)))
                } else {
                    val packNo = frameBytes[4].toInt() and 0xFF
                    val text = frameBytes.copyOfRange(5, frameBytes.size - 1)
                    packets.add(Packet(command, packNo, text))
                }
            }
            return packets
        }

        private fun findStx(): Int {
            val list = buf.toList()
            for (i in 0..list.size - 2) {
                if (list[i] == STX[0] && list[i + 1] == STX[1]) return i
            }
            return -1
        }
    }
}
