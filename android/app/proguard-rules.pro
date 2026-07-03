# WebView JS interface methods must survive obfuscation
-keepclassmembers class com.vendingqr.kiosk.ui.MainActivity$WebAppInterface {
    public *;
}
-keep class com.vendingqr.kiosk.serial.SerialPort { *; }
-dontwarn okhttp3.**
-dontwarn okio.**
