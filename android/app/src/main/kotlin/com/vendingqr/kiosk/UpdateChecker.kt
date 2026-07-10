package com.vendingqr.kiosk

import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.Toast
import androidx.core.content.FileProvider
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Проверка обновлений киоска: сверяет sha256 своего установленного APK с тем,
 * что сейчас лежит на сервере (GET /api/apk-version). Если отличается —
 * качает новый файл и открывает системный экран установки. Полностью без
 * подтверждения Android не даёт поставить приложение без прав device owner —
 * но один тап «Установить» на месте куда быстрее, чем качать вручную через
 * браузер и разбираться, какой файл в Загрузках свежий.
 */
object UpdateChecker {
    private const val TAG = "UpdateChecker"
    private val executor = Executors.newSingleThreadExecutor()
    private val mainHandler = Handler(Looper.getMainLooper())
    private val client = OkHttpClient.Builder().build()
    private val checking = AtomicBoolean(false)

    fun checkAndPromptOnce(context: Context, serverUrl: String) {
        if (!checking.compareAndSet(false, true)) return  // уже проверяем — не дублируем
        val appContext = context.applicationContext
        executor.execute {
            try {
                runCheck(appContext, serverUrl)
            } catch (e: Exception) {
                Log.w(TAG, "update check failed: ${e.message}")
                toast(appContext, "Проверка обновлений: ${e.message}")
            } finally {
                checking.set(false)
            }
        }
    }

    // На кассовом планшете нет доступа к логам (adb) — без тоста любой сбой
    // (сеть недоступна, sourceDir не читается, установщик не открылся) был бы
    // полностью незаметен и выглядел бы как «автообновление не работает»,
    // хотя на самом деле просто молча падало на каком-то шаге.
    private fun toast(context: Context, text: String) {
        mainHandler.post { Toast.makeText(context, text, Toast.LENGTH_LONG).show() }
    }

    private fun runCheck(context: Context, serverUrl: String) {
        val base = serverUrl.trimEnd('/')
        val remoteHash = fetchRemoteHash(base)
        if (remoteHash == null) {
            toast(context, "Проверка обновлений: сервер недоступен")
            return
        }
        val localHash = ownApkHash(context)
        if (localHash == null) {
            toast(context, "Проверка обновлений: не удалось прочитать свою версию")
            return
        }
        if (remoteHash.equals(localHash, ignoreCase = true)) {
            Log.i(TAG, "kiosk app is up to date")
            return
        }
        Log.i(TAG, "new version on server ($remoteHash != $localHash) — downloading")
        toast(context, "Найдено обновление, скачиваю…")
        val file = downloadApk(context, base)
        if (file == null) {
            toast(context, "Не удалось скачать обновление")
            return
        }
        mainHandler.post { promptInstall(context, file) }
    }

    private fun fetchRemoteHash(base: String): String? {
        val req = Request.Builder().url("$base/api/apk-version").build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) return null
            val body = resp.body?.string() ?: return null
            val hash = JSONObject(body).optString("sha256", "")
            return hash.ifBlank { null }
        }
    }

    private fun ownApkHash(context: Context): String? {
        val sourceDir = context.applicationInfo?.sourceDir ?: return null
        return try {
            sha256(File(sourceDir))
        } catch (e: Exception) {
            Log.w(TAG, "failed to hash own APK: ${e.message}")
            null
        }
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buf = ByteArray(65536)
            var n: Int
            while (input.read(buf).also { n = it } > 0) digest.update(buf, 0, n)
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun downloadApk(context: Context, base: String): File? {
        val req = Request.Builder().url("$base/apk").build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) {
                Log.w(TAG, "apk download failed: HTTP ${resp.code}")
                return null
            }
            val body = resp.body ?: return null
            val dir = File(context.cacheDir, "updates").apply { mkdirs() }
            val file = File(dir, "vending-kiosk.apk")
            file.outputStream().use { out -> body.byteStream().copyTo(out) }
            return file
        }
    }

    private fun promptInstall(context: Context, apkFile: File) {
        try {
            val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", apkFile)
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
        } catch (e: Exception) {
            Log.w(TAG, "failed to launch installer: ${e.message}")
            Toast.makeText(context, "Не удалось открыть установщик: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }
}
