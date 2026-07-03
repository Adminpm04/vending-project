package com.vendingqr.kiosk.net

import android.os.Handler
import android.os.Looper
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * WebSocket-канал контроллера точки к центральному серверу (/ws/machine/{id}).
 * Отдельный от того, что использует kiosk.html внутри WebView (/ws/kiosk/{id}) —
 * сервер связывает их через machine_id, как описано в архитектуре проекта.
 * Реконнект с экспоненциальным бэкоффом, как в controller.py::ws_loop.
 */
class ControllerWebSocket(
    private val serverUrl: String,
    private val machineId: String,
    private val token: String,
    private val onMessage: (JSONObject) -> Unit,
    private val onStateChange: (Boolean) -> Unit,
) {
    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    @Volatile private var ws: WebSocket? = null
    @Volatile private var shouldRun = false
    private var backoffMs = 1000L
    private val handler = Handler(Looper.getMainLooper())

    fun connect() {
        shouldRun = true
        doConnect()
    }

    fun disconnect() {
        shouldRun = false
        handler.removeCallbacksAndMessages(null)
        ws?.close(1000, "app stopping")
        ws = null
    }

    fun send(json: JSONObject) {
        val socket = ws
        if (socket == null) {
            Log.w(TAG, "send() while disconnected: $json")
            return
        }
        socket.send(json.toString())
    }

    private fun doConnect() {
        if (!shouldRun) return
        val wsUrl = buildWsUrl()
        val request = Request.Builder().url(wsUrl).build()
        ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "connected: $wsUrl")
                backoffMs = 1000
                onStateChange(true)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val obj = JSONObject(text)
                    if (obj.optString("type") == "ping") {
                        webSocket.send(JSONObject().put("type", "pong").toString())
                        return
                    }
                    onMessage(obj)
                } catch (e: Exception) {
                    Log.w(TAG, "bad message ignored: $text")
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                onStateChange(false)
                scheduleReconnect()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "ws failure: ${t.message}")
                onStateChange(false)
                scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        if (!shouldRun) return
        handler.postDelayed({ doConnect() }, backoffMs)
        backoffMs = (backoffMs * 2).coerceAtMost(30_000)
    }

    private fun buildWsUrl(): String {
        val base = serverUrl.trimEnd('/')
        val wsBase = when {
            base.startsWith("https://") -> "wss://" + base.removePrefix("https://")
            base.startsWith("http://") -> "ws://" + base.removePrefix("http://")
            else -> "ws://$base"
        }
        return "$wsBase/ws/machine/$machineId?token=$token"
    }

    companion object {
        private const val TAG = "ControllerWS"
    }
}
