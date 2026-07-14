package com.vendingqr.kiosk.serial

import android.util.Log
import com.vendingqr.kiosk.net.ControllerWebSocket
import org.json.JSONObject
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Связывает RS232-мост (VmcLink) с сервером (ControllerWebSocket).
 * 1:1 порт backend/controller.py — приём "dispense" от сервера → команда VMC →
 * результат обратно серверу. Держать в синхроне при изменении протокола сервера.
 */
class VmcController(
    serverUrl: String,
    private val machineId: String,
    token: String,
    serialPortPath: String,
    serialBaud: Int,
) {
    private val link = VmcLink(serialPortPath, serialBaud)
    private val executor: ExecutorService = Executors.newCachedThreadPool()
    private val ws = ControllerWebSocket(
        serverUrl = serverUrl,
        machineId = machineId,
        token = token,
        onMessage = ::handleServerMessage,
        onStateChange = { connected ->
            Log.i(TAG, "server connection: $connected")
            serverConnected = connected
        },
    )

    @Volatile var serverConnected = false
        private set

    @Volatile var lastError: String? = null
        private set

    val serialConnected: Boolean get() = link.isRunning

    fun start() {
        try {
            link.start()
            lastError = null
        } catch (e: Throwable) {
            // Throwable, не Exception: если нативная .so не грузится под текущую
            // архитектуру (эмулятор x86, битый .so), JVM бросает UnsatisfiedLinkError —
            // это Error, а не Exception, обычный catch(Exception) его бы не поймал
            // и приложение упало бы вместо аккуратного "нет связи с VMC".
            lastError = "Serial: ${e.message}"
            Log.e(TAG, "failed to open serial port", e)
        }
        ws.connect()
    }

    fun stop() {
        ws.disconnect()
        link.stop()
        executor.shutdownNow()
    }

    private fun handleServerMessage(msg: JSONObject) {
        when (msg.optString("type")) {
            "dispense" -> {
                val sessionId = msg.optInt("session_id")
                val slotId = msg.optInt("slot_id")
                executor.submit { handleDispense(sessionId, slotId) }
            }
            "check_slot" -> {
                val requestId = msg.optString("request_id")
                val slotId = msg.optInt("slot_id")
                executor.submit { handleCheckSlot(requestId, slotId) }
            }
            "check_elevator" -> {
                val requestId = msg.optString("request_id")
                executor.submit { handleCheckElevator(requestId) }
            }
        }
    }

    private fun handleDispense(sessionId: Int, slotId: Int) {
        Log.i(TAG, "dispense request: session=$sessionId slot=$slotId")

        // Очищаем старые события, чтобы не поймать статус прошлой выдачи.
        link.dispenseEvents.clear()

        if (!link.isRunning) {
            sendResult(sessionId, success = false, code = null, message = "Serial port not open")
            return
        }

        link.queueDispense(slotId)

        // Статус (0x04) несёт свой selection number — если предыдущий запрос
        // задержался (напр. механизм долго возился) и его финальный статус
        // пришёл только сейчас, после того как мы уже отправили команду для
        // ДРУГОГО слота, poll() без проверки принял бы чужой ответ за наш.
        // Отбрасываем несовпадающие по слоту события и ждём дальше — до
        // общего дедлайна, а не по одному poll() с полным таймаутом.
        val deadline = System.currentTimeMillis() + 45_000
        var status: VmcProtocol.DispenseStatus? = null
        while (System.currentTimeMillis() < deadline) {
            val remaining = deadline - System.currentTimeMillis()
            val ev = try {
                link.dispenseEvents.poll(remaining, TimeUnit.MILLISECONDS)
            } catch (e: InterruptedException) {
                null
            } ?: break
            if (ev.slot != null && ev.slot != slotId) {
                Log.w(TAG, "dispense status for slot ${ev.slot}, expected $slotId (session=$sessionId) — stale, ignoring")
                continue
            }
            status = ev
            break
        }

        if (status != null) {
            sendResult(sessionId, status.kind == VmcProtocol.DispenseStatus.Kind.SUCCESS, status.code, status.message)
        } else {
            sendResult(sessionId, success = false, code = null, message = "VMC response timeout")
        }
    }

    private fun handleCheckSlot(requestId: String, slotId: Int) {
        Log.i(TAG, "check-slot request: request_id=$requestId slot=$slotId")

        link.selectionEvents.clear()

        if (!link.isRunning) {
            sendSlotState(requestId, checked = false, ok = true, message = "Serial port not open")
            return
        }

        link.queueCheckSelection(slotId)

        val deadline = System.currentTimeMillis() + 3_000
        var state: VmcProtocol.SelectionState? = null
        while (System.currentTimeMillis() < deadline) {
            val remaining = deadline - System.currentTimeMillis()
            val ev = try {
                link.selectionEvents.poll(remaining, TimeUnit.MILLISECONDS)
            } catch (e: InterruptedException) {
                null
            } ?: break
            if (ev.slot != null && ev.slot != slotId) {
                Log.w(TAG, "selection state for slot ${ev.slot}, expected $slotId (request=$requestId) — stale, ignoring")
                continue
            }
            state = ev
            break
        }

        if (state != null) {
            sendSlotState(requestId, checked = true, ok = state.ok, message = state.message)
        } else {
            sendSlotState(requestId, checked = false, ok = true, message = "VMC response timeout")
        }
    }

    private fun handleCheckElevator(requestId: String) {
        Log.i(TAG, "check-elevator request: request_id=$requestId")

        link.elevatorEvents.clear()

        if (!link.isRunning) {
            sendElevatorState(requestId, checked = false, ok = true, message = "Serial port not open")
            return
        }

        link.queueElevatorStatus()

        val status = try {
            link.elevatorEvents.poll(3, TimeUnit.SECONDS)
        } catch (e: InterruptedException) {
            null
        }

        if (status != null) {
            sendElevatorState(requestId, checked = true, ok = status.ok, message = status.message)
        } else {
            sendElevatorState(requestId, checked = false, ok = true, message = "VMC response timeout")
        }
    }

    private fun sendElevatorState(requestId: String, checked: Boolean, ok: Boolean, message: String) {
        val result = JSONObject().apply {
            put("type", "elevator_state")
            put("request_id", requestId)
            put("checked", checked)
            put("ok", ok)
            put("message", message)
        }
        Log.i(TAG, "elevator state: $result")
        ws.send(result)
    }

    private fun sendSlotState(requestId: String, checked: Boolean, ok: Boolean, message: String) {
        val result = JSONObject().apply {
            put("type", "slot_state")
            put("request_id", requestId)
            put("checked", checked)
            put("ok", ok)
            put("message", message)
        }
        Log.i(TAG, "slot state: $result")
        ws.send(result)
    }

    private fun sendResult(sessionId: Int, success: Boolean, code: Int?, message: String) {
        val result = JSONObject().apply {
            put("type", "dispense_result")
            put("session_id", sessionId)
            put("success", success)
            put("code", code ?: JSONObject.NULL)
            put("message", message)
        }
        Log.i(TAG, "dispense result: $result")
        ws.send(result)
    }

    companion object {
        private const val TAG = "VmcController"
    }
}
