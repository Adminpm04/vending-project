package com.vendingqr.kiosk.ui

import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.vendingqr.kiosk.Prefs
import com.vendingqr.kiosk.databinding.ActivitySettingsBinding
import com.vendingqr.kiosk.serial.VmcControllerService

/**
 * Разовая настройка киоска техником: machine_id, адрес сервера, токен контроллера
 * (выдаются в админке при добавлении точки), serial-порт VMC. Открывается долгим
 * нажатием на скрытую зону в углу экрана MainActivity.
 */
class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.inputMachineId.setText(Prefs.machineId(this))
        binding.inputServerUrl.setText(Prefs.serverUrl(this))
        binding.inputToken.setText(Prefs.token(this))
        binding.inputSerialPort.setText(Prefs.serialPort(this))
        binding.inputPortrait.isChecked = Prefs.isPortrait(this)
        binding.inputReverse.isChecked = Prefs.isReverse(this)

        binding.btnSave.setOnClickListener { save() }
    }

    private fun save() {
        val machineId = binding.inputMachineId.text.toString().trim()
        val serverUrl = binding.inputServerUrl.text.toString().trim()
        val token = binding.inputToken.text.toString().trim()
        val serialPort = binding.inputSerialPort.text.toString().trim()
        val portrait = binding.inputPortrait.isChecked
        val reverse = binding.inputReverse.isChecked

        if (machineId.isEmpty() || token.isEmpty()) {
            Toast.makeText(this, "Заполните ID автомата и токен", Toast.LENGTH_SHORT).show()
            return
        }

        Prefs.save(this, machineId, serverUrl, token, serialPort, portrait, reverse)

        // Перезапускаем сервис контроллера с новыми настройками.
        stopService(Intent(this, VmcControllerService::class.java))
        ContextCompat.startForegroundService(this, Intent(this, VmcControllerService::class.java))

        Toast.makeText(this, "Сохранено", Toast.LENGTH_SHORT).show()
        startActivity(Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP
        })
        finish()
    }
}
