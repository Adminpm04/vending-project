package com.vendingqr.kiosk.serial

import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException

/**
 * Обёртка над нативным serial-мостом (see serial_port.c).
 * Порт /dev/ttyS1 на этом железе не требует root (crwxrwxrwx).
 */
class SerialPort(devicePath: String, baudRate: Int) {

    private val fd: java.io.FileDescriptor
    val inputStream: FileInputStream
    val outputStream: FileOutputStream

    init {
        val device = File(devicePath)
        if (!device.canRead() || !device.canWrite()) {
            try {
                // На случай нестандартной прошивки — пробуем chmod, но обычно не требуется.
                Runtime.getRuntime().exec(arrayOf("chmod", "666", devicePath)).waitFor()
            } catch (_: Exception) {
            }
        }
        fd = nativeOpen(devicePath, baudRate)
            ?: throw IOException("Не удалось открыть $devicePath (baud=$baudRate)")
        inputStream = FileInputStream(fd)
        outputStream = FileOutputStream(fd)
    }

    fun close() {
        try {
            inputStream.close()
        } catch (_: IOException) {
        }
        try {
            outputStream.close()
        } catch (_: IOException) {
        }
    }

    companion object {
        init {
            System.loadLibrary("vending_serial")
        }

        @JvmStatic
        private external fun nativeOpen(path: String, baudrate: Int): java.io.FileDescriptor?

        @JvmStatic
        external fun nativeClose(fd: Int)
    }
}
