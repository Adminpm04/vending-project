package com.vendingqr.kiosk

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat
import com.vendingqr.kiosk.serial.VmcControllerService
import com.vendingqr.kiosk.ui.MainActivity

/** Автозапуск при включении планшета — точка должна работать без вмешательства человека. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != "android.intent.action.QUICKBOOT_POWERON") {
            return
        }
        if (!Prefs.isConfigured(context)) return

        ContextCompat.startForegroundService(context, Intent(context, VmcControllerService::class.java))

        val launch = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        context.startActivity(launch)
    }
}
