package com.vendingqr.kiosk

import android.content.Context
import android.content.SharedPreferences

/** Настройки киоска: заполняются один раз в SettingsActivity при установке на точку. */
object Prefs {
    private const val FILE = "vending_kiosk_prefs"
    private const val KEY_MACHINE_ID = "machine_id"
    private const val KEY_SERVER_URL = "server_url"
    private const val KEY_TOKEN = "machine_token"
    private const val KEY_SERIAL_PORT = "serial_port"

    private const val DEFAULT_SERVER_URL = "http://10.251.4.253:8000"
    private const val DEFAULT_SERIAL_PORT = "/dev/ttyS1"
    private const val DEFAULT_BAUD = 57600

    private fun prefs(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun isConfigured(ctx: Context): Boolean =
        machineId(ctx).isNotBlank() && token(ctx).isNotBlank()

    fun machineId(ctx: Context): String = prefs(ctx).getString(KEY_MACHINE_ID, "") ?: ""
    fun serverUrl(ctx: Context): String = prefs(ctx).getString(KEY_SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
    fun token(ctx: Context): String = prefs(ctx).getString(KEY_TOKEN, "") ?: ""
    fun serialPort(ctx: Context): String = prefs(ctx).getString(KEY_SERIAL_PORT, DEFAULT_SERIAL_PORT) ?: DEFAULT_SERIAL_PORT
    fun serialBaud(ctx: Context): Int = DEFAULT_BAUD

    fun save(ctx: Context, machineId: String, serverUrl: String, token: String, serialPort: String) {
        prefs(ctx).edit()
            .putString(KEY_MACHINE_ID, machineId.trim())
            .putString(KEY_SERVER_URL, serverUrl.trim().ifBlank { DEFAULT_SERVER_URL })
            .putString(KEY_TOKEN, token.trim())
            .putString(KEY_SERIAL_PORT, serialPort.trim().ifBlank { DEFAULT_SERIAL_PORT })
            .apply()
    }

    fun kioskUrl(ctx: Context): String = "${serverUrl(ctx).trimEnd('/')}/kiosk/${machineId(ctx)}"
}
