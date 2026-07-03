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
        if (msg.optString("type") == "dispense") {
            val sessionId = msg.optInt("session_id")
            val slotId = msg.optInt("slot_id")
            executor.submit { handleDispense(sessionId, slotId) }
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

        val status = try {
            link.dispenseEvents.poll(45, TimeUnit.SECONDS)
        } catch (e: InterruptedException) {
            null
        }

        if (status != null) {
            sendResult(sessionId, status.kind == VmcProtocol.DispenseStatus.Kind.SUCCESS, status.code, status.message)
        } else {
            sendResult(sessionId, success = false, code = null, message = "VMC response timeout")
        }
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
