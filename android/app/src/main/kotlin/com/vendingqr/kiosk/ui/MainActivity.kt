package com.vendingqr.kiosk.ui

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.pm.ActivityInfo
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.MotionEvent
import android.view.View
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.vendingqr.kiosk.Prefs
import com.vendingqr.kiosk.R
import com.vendingqr.kiosk.UpdateChecker
import com.vendingqr.kiosk.databinding.ActivityMainBinding
import com.vendingqr.kiosk.serial.VmcControllerService

/**
 * Единственный экран для покупателя: fullscreen WebView с kiosk.html с сервера.
 * RS232/WebSocket-логика работает отдельно в VmcControllerService — WebView
 * ничего не знает про serial-порт, вся связка идёт через backend по machine_id.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val retryHandler = Handler(Looper.getMainLooper())
    private var retryDelayMs = 2000L
    private var webViewReady = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // До setContentView(), чтобы не мелькнуть неверной ориентацией на старте.
        applyOrientation()
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        applyImmersiveMode()

        setupSettingsHotspot()
        applyConfigurationState()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        // MainActivity объявлена launchMode="singleTask": когда из SettingsActivity
        // возвращаются сюда после сохранения (CLEAR_TOP|SINGLE_TOP), система не
        // создаёт новый экземпляр и не вызывает onCreate() — переиспользует этот
        // же, вызывая только onNewIntent(). Без этого переопределения экран так и
        // оставался бы в состоянии «не настроено», в котором был при первом запуске,
        // даже если токен только что успешно сохранили. Заодно переприменяем
        // ориентацию — если её только что переключили в настройках, должно
        // сработать сразу, без перезапуска приложения.
        applyOrientation()
        applyConfigurationState()
    }

    /**
     * Планшет физически монтируется в автомат по-разному в зависимости от
     * корпуса — горизонтально или вертикально. Ориентация настраивается один
     * раз в SettingsActivity под конкретную точку (Prefs.isPortrait), а не
     * зашита статически в манифесте.
     */
    private fun applyOrientation() {
        requestedOrientation = if (Prefs.isPortrait(this))
            ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
        else
            ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE
    }

    private fun applyConfigurationState() {
        if (Prefs.isConfigured(this)) {
            startControllerService()
            // Раз за запуск приложения: сверить свою версию с сервером и, если
            // отличается, скачать новую и предложить установить — чтобы каждое
            // обновление не требовало вручную качать APK из браузера на месте.
            UpdateChecker.checkAndPromptOnce(this, Prefs.serverUrl(this))
            if (!webViewReady) {
                setupWebView()
                webViewReady = true
                // Сразу сменить текст с «не настроено» на «загрузка», пока WebView
                // тянет kiosk.html — иначе повисит устаревшая надпись до onPageFinished.
                showOverlay("Загрузка…", showSpinner = true)
            }
            loadKiosk()
        } else {
            // Статичное состояние ожидания настройки — без спиннера, иначе
            // выглядит как зависшая загрузка, хотя это штатное состояние.
            showOverlay(getString(R.string.err_not_configured), showSpinner = false)
        }
    }

    override fun onResume() {
        super.onResume()
        applyImmersiveMode()
        maybeStartLockTask()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) applyImmersiveMode()
    }

    private fun setupWebView() {
        binding.webView.apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.databaseEnabled = true
            settings.loadWithOverviewMode = true
            settings.useWideViewPort = true
            settings.setSupportZoom(false)
            settings.builtInZoomControls = false
            settings.cacheMode = android.webkit.WebSettings.LOAD_DEFAULT
            isLongClickable = false
            setOnLongClickListener { true } // без контекстного меню/выделения текста

            webViewClient = object : WebViewClient() {
                override fun onPageFinished(view: WebView, url: String) {
                    hideOverlay()
                    retryDelayMs = 2000L
                }

                override fun onReceivedError(
                    view: WebView,
                    request: WebResourceRequest,
                    error: WebResourceError,
                ) {
                    if (request.isForMainFrame) {
                        Log.w(TAG, "load error: ${error.description}")
                        showOverlay("Нет связи с сервером\nПробуем снова…", showSpinner = true)
                        scheduleRetry()
                    }
                }
            }
        }
    }

    private fun loadKiosk() {
        binding.webView.loadUrl(Prefs.kioskUrl(this))
    }

    /**
     * Свой long-press вместо View.setOnLongClickListener: у системного жеста
     * маленький допуск на дрожание пальца/курсора (touch slop ~8dp), из-за
     * чего он часто срывается при эмуляции касания мышью (BlueStacks и т.п.).
     * Здесь допуск сильно шире — держит жест, даже если курсор чуть сместился.
     */
    private fun setupSettingsHotspot() {
        val hotspotHandler = Handler(Looper.getMainLooper())
        var pressStartX = 0f
        var pressStartY = 0f
        val openSettings = Runnable {
            startActivity(Intent(this, SettingsActivity::class.java))
        }

        binding.settingsHotspot.setOnTouchListener { _, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    pressStartX = event.rawX
                    pressStartY = event.rawY
                    hotspotHandler.removeCallbacks(openSettings)
                    hotspotHandler.postDelayed(openSettings, LONG_PRESS_MS)
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = event.rawX - pressStartX
                    val dy = event.rawY - pressStartY
                    if (dx * dx + dy * dy > MOVE_TOLERANCE_PX * MOVE_TOLERANCE_PX) {
                        hotspotHandler.removeCallbacks(openSettings)
                    }
                    true
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    hotspotHandler.removeCallbacks(openSettings)
                    true
                }
                else -> false
            }
        }
    }

    private fun scheduleRetry() {
        retryHandler.removeCallbacksAndMessages(null)
        retryHandler.postDelayed({
            loadKiosk()
        }, retryDelayMs)
        retryDelayMs = (retryDelayMs * 2).coerceAtMost(30_000)
    }

    private fun showOverlay(text: String, showSpinner: Boolean) {
        binding.statusText.text = text
        binding.statusSpinner.visibility = if (showSpinner) View.VISIBLE else View.GONE
        binding.statusOverlay.visibility = View.VISIBLE
        // Показываем видимую метку поверх зоны долгого нажатия только пока нет
        // каталога товаров на экране (иначе значок мешал бы покупателю).
        binding.settingsHint.visibility = View.VISIBLE
    }

    private fun hideOverlay() {
        binding.statusOverlay.visibility = View.GONE
        binding.settingsHint.visibility = View.GONE
    }

    private fun startControllerService() {
        val intent = Intent(this, VmcControllerService::class.java)
        ContextCompat.startForegroundService(this, intent)
    }

    private fun applyImmersiveMode() {
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = (
            View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            )
    }

    /**
     * Screen pinning: работает без device-owner, но при первом запуске система
     * может показать системную подсказку. На управляемом устройстве (device owner)
     * этот же вызов проходит без диалогов вообще.
     */
    private fun maybeStartLockTask() {
        try {
            val am = getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
            val alreadyLocked = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                am.lockTaskModeState != ActivityManager.LOCK_TASK_MODE_NONE
            } else {
                @Suppress("DEPRECATION")
                am.isInLockTaskMode
            }
            if (!alreadyLocked) startLockTask()
        } catch (e: Exception) {
            Log.w(TAG, "screen pinning unavailable: ${e.message}")
        }
    }

    companion object {
        private const val TAG = "MainActivity"
        private const val LONG_PRESS_MS = 1200L
        private const val MOVE_TOLERANCE_PX = 80f
    }
}
