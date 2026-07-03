package com.vendingqr.kiosk.serial

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.vendingqr.kiosk.Prefs
import com.vendingqr.kiosk.R
import com.vendingqr.kiosk.ui.MainActivity

/**
 * Foreground-сервис контроллера точки. Живёт независимо от WebView/Activity —
 * держит RS232-связь с VMC и WebSocket с сервером всё время, пока планшет включён.
 */
class VmcControllerService : Service() {

    private var controller: VmcController? = null

    override fun onCreate() {
        super.onCreate()
        startForeground(NOTIFICATION_ID, buildNotification())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (controller == null && Prefs.isConfigured(this)) {
            controller = VmcController(
                serverUrl = Prefs.serverUrl(this),
                machineId = Prefs.machineId(this),
                token = Prefs.token(this),
                serialPortPath = Prefs.serialPort(this),
                serialBaud = Prefs.serialBaud(this),
            ).also { it.start() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        controller?.stop()
        controller = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, "Vending QR — контроллер точки", NotificationManager.IMPORTANCE_MIN,
            )
            manager.createNotificationChannel(channel)
        }
        val openApp = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Vending QR")
            .setContentText("Контроллер автомата активен")
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentIntent(openApp)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()
    }

    companion object {
        private const val CHANNEL_ID = "vmc_controller"
        private const val NOTIFICATION_ID = 1
    }
}
