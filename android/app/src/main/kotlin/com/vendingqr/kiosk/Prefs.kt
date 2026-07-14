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
    private const val KEY_PORTRAIT = "portrait"
    private const val KEY_REVERSE = "reverse"
    private const val KEY_LOCK_TASK = "lock_task"

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
    // Планшет физически монтируется в автомат по-разному в зависимости от
    // модели корпуса — на одних точках горизонтально, на других вертикально.
    // По умолчанию true (портретная) — так смонтировано большинство точек;
    // альбомную включают вручную в настройках там, где нужно.
    fun isPortrait(ctx: Context): Boolean = prefs(ctx).getBoolean(KEY_PORTRAIT, true)
    // Планшет иногда вставлен в корпус развёрнутым на 180° относительно своего
    // «естественного» верха — тогда обычная портретная/альбомная ориентация
    // покажет содержимое вверх ногами для того, кто стоит перед автоматом.
    fun isReverse(ctx: Context): Boolean = prefs(ctx).getBoolean(KEY_REVERSE, false)
    // Блокировка экрана (screen pinning) — не даёт покупателю свернуть киоск
    // и выйти в систему. По умолчанию ВЫКЛЮЧЕНА — сейчас идёт активное
    // тестирование, блокировка мешает выходить при зависании через RustDesk.
    // Перед реальным запуском точки в бой — включить здесь по умолчанию true
    // (или отметить галочку в настройках на конкретном планшете).
    fun isLockTaskEnabled(ctx: Context): Boolean = prefs(ctx).getBoolean(KEY_LOCK_TASK, false)

    fun save(ctx: Context, machineId: String, serverUrl: String, token: String, serialPort: String, portrait: Boolean, reverse: Boolean, lockTask: Boolean) {
        prefs(ctx).edit()
            .putString(KEY_MACHINE_ID, machineId.trim())
            .putString(KEY_SERVER_URL, serverUrl.trim().ifBlank { DEFAULT_SERVER_URL })
            .putString(KEY_TOKEN, token.trim())
            .putString(KEY_SERIAL_PORT, serialPort.trim().ifBlank { DEFAULT_SERIAL_PORT })
            .putBoolean(KEY_PORTRAIT, portrait)
            .putBoolean(KEY_REVERSE, reverse)
            .putBoolean(KEY_LOCK_TASK, lockTask)
            .apply()
    }

    fun kioskUrl(ctx: Context): String = "${serverUrl(ctx).trimEnd('/')}/kiosk/${machineId(ctx)}"
}
