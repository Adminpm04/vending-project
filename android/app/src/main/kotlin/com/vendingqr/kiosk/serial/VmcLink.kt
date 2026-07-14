package com.vendingqr.kiosk.serial

import android.util.Log
import java.io.IOException
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean

/**
 * RS232-связь с VMC: POLL/ACK-цикл, отправка команд, приём статусов.
 * 1:1 порт backend/controller.py::VMCLink — держать в синхроне при изменении протокола.
 *
 * VMC опрашивает нас каждые ~200 мс; ответ должен уйти в течение 100 мс, поэтому
 * чтение порта идёт в отдельном потоке с блокирующим read() (VTIME=1 в native-коде).
 */
class VmcLink(private val devicePath: String, private val baudRate: Int) {

    private var serialPort: SerialPort? = null
    private val parser = VmcProtocol.PacketParser()
    private var packNo = 1
    private val outbox = ArrayDeque<ByteArray>()
    private var pendingAck: ByteArray? = null
    private var retries = 0
    private val running = AtomicBoolean(false)
    private var readerThread: Thread? = null

    /** Финальные (не in-progress) статусы выдачи — читаются VmcController'ом. */
    val dispenseEvents = LinkedBlockingQueue<VmcProtocol.DispenseStatus>()

    /** Ответы на проверку слота (0x02) — читаются VmcController'ом. */
    val selectionEvents = LinkedBlockingQueue<VmcProtocol.SelectionState>()

    @Volatile
    var synced = false
        private set

    @Volatile
    var lastActivityMs = 0L
        private set

    private fun nextPackNo(): Int {
        val no = packNo
        packNo = packNo % 255 + 1
        return no
    }

    @Synchronized
    fun queueDispense(slotId: Int) {
        outbox.addLast(VmcProtocol.buildDriveDirect(nextPackNo(), slotId, dropSensor = true, elevator = false))
    }

    @Synchronized
    fun queueCheckSelection(slotId: Int) {
        outbox.addLast(VmcProtocol.buildCheckSelection(nextPackNo(), slotId))
    }

    @Synchronized
    private fun queueSync() {
        outbox.addLast(VmcProtocol.buildSync(nextPackNo()))
    }

    @Throws(IOException::class)
    fun start() {
        if (running.getAndSet(true)) return
        val port = SerialPort(devicePath, baudRate)
        serialPort = port
        queueSync() // протокол требует 0x31 при старте Upper computer
        readerThread = Thread(::runLoop, "vmc-serial-reader").apply {
            isDaemon = true
            start()
        }
    }

    fun stop() {
        running.set(false)
        readerThread?.interrupt()
        serialPort?.close()
        serialPort = null
    }

    val isRunning: Boolean get() = running.get()

    private fun runLoop() {
        val port = serialPort ?: return
        val buf = ByteArray(64)
        Log.i(TAG, "RS232 reader started on $devicePath @ $baudRate")
        while (running.get()) {
            try {
                val n = port.inputStream.read(buf)
                if (n <= 0) continue // таймаут чтения (VTIME) — нормальная ситуация
                lastActivityMs = System.currentTimeMillis()
                val packets = parser.feed(buf.copyOf(n))
                for (p in packets) handle(port, p)
            } catch (e: IOException) {
                if (running.get()) {
                    Log.e(TAG, "serial read error: ${e.message}")
                    Thread.sleep(500)
                }
            } catch (_: InterruptedException) {
                break
            }
        }
        Log.i(TAG, "RS232 reader stopped")
    }

    private fun writeSafe(port: SerialPort, data: ByteArray) {
        try {
            port.outputStream.write(data)
        } catch (e: IOException) {
            Log.e(TAG, "serial write error: ${e.message}")
        }
    }

    @Synchronized
    private fun handle(port: SerialPort, packet: VmcProtocol.Packet) {
        if (packet.command == VmcProtocol.CMD_POLL) {
            val ack = pendingAck
            when {
                ack != null && retries < 5 -> {
                    writeSafe(port, ack)
                    retries++
                }
                ack != null -> {
                    Log.e(TAG, "command not ACKed after 5 retries — dropping")
                    pendingAck = null
                    retries = 0
                    writeSafe(port, VmcProtocol.ACK_PACKET)
                }
                outbox.isNotEmpty() -> {
                    val next = outbox.removeFirst()
                    writeSafe(port, next)
                    pendingAck = next
                    retries = 0
                }
                else -> writeSafe(port, VmcProtocol.ACK_PACKET)
            }
            return
        }

        if (packet.command == VmcProtocol.CMD_ACK) {
            pendingAck = null
            retries = 0
            return
        }

        // Любой информационный пакет от VMC подтверждаем ACK.
        writeSafe(port, VmcProtocol.ACK_PACKET)

        when (packet.command) {
            VmcProtocol.CMD_SYNC -> {
                queueSync()
                synced = true
                Log.i(TAG, "VMC sync")
            }
            VmcProtocol.CMD_DISPENSE_STATUS -> {
                val status = VmcProtocol.parseDispenseStatus(packet.text)
                Log.i(TAG, "Dispense status: $status")
                if (status.kind != VmcProtocol.DispenseStatus.Kind.IN_PROGRESS) {
                    dispenseEvents.offer(status)
                }
            }
            VmcProtocol.CMD_SELECTION_STATE -> {
                val state = VmcProtocol.parseSelectionState(packet.text)
                Log.i(TAG, "Selection state: $state")
                selectionEvents.offer(state)
            }
            VmcProtocol.CMD_SLOT_INFO -> {
                // Цены/остатки ведёт сервер — информация VMC не используется.
            }
            else -> Log.d(TAG, "VMC cmd 0x%02X text=%s".format(packet.command, packet.text.joinToString("") { "%02X".format(it.toInt() and 0xFF) }))
        }
    }

    companion object {
        private const val TAG = "VmcLink"
    }
}
