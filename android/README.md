# Vending QR — Android-киоск

Нативное Android-приложение для планшета на автомате: полноэкранный WebView с
готовым `kiosk.html` с сервера + независимый foreground-сервис, который держит
RS232-связь с VMC (`/dev/ttyS1`, 57600 8N1) и WebSocket-канал с backend для
приёма команд выдачи. Архитектура зеркалит `backend/vmc_protocol.py` и
`controller/controller.py` — держать в синхроне при изменении протокола.

## Компоненты

| Файл | Роль |
|---|---|
| `serial/SerialPort.kt` + `cpp/serial_port.c` | JNI-обёртка над POSIX termios — открывает `/dev/ttyS1` без root (права `crwxrwxrwx` подтверждены на реальном планшете) |
| `serial/VmcProtocol.kt` | 1:1 порт `backend/vmc_protocol.py`: сборка/разбор пакетов, XOR, коды статусов |
| `serial/VmcLink.kt` | POLL/ACK-цикл в отдельном потоке — ответ VMC в пределах 100 мс |
| `serial/VmcController.kt` + `net/ControllerWebSocket.kt` | 1:1 порт `controller/controller.py`: приём `dispense` от сервера → команда `0x06` → результат обратно |
| `serial/VmcControllerService.kt` | Foreground-сервис — держит контроллер живым независимо от экрана |
| `ui/MainActivity.kt` | Полноэкранный WebView, грузит `{server}/kiosk/{machine_id}`, screen pinning, автоперезагрузка при обрыве связи |
| `ui/SettingsActivity.kt` | Разовая настройка точки (открывается долгим тапом в левом верхнем углу экрана) |
| `BootReceiver.kt` | Автозапуск при включении планшета |

**Важно:** WebView (покупательский интерфейс) и `VmcControllerService` (RS232)
— два независимых канала к серверу. Они не общаются друг с другом напрямую;
backend связывает их по `machine_id`, как описано в `PROJECT_CONTEXT.md`.

## Первая настройка точки

1. В админке (`/admin` → «Точки» → «Добавить точку») создать точку — получить `secret_token`
2. На планшете открыть приложение, долгий тап в левом верхнем углу экрана
3. Заполнить: ID автомата, адрес сервера, токен, serial-порт (по умолчанию `/dev/ttyS1`)
4. «Сохранить» — сервис контроллера перезапустится с новыми настройками

## Сборка

Локально нужны Android SDK 34 + NDK 26.1.10909125 + CMake 3.22.1 (Android Studio
поставит сама). В этом окружении разработки SDK/NDK не установлены (места и
политики недостаточно) — сборка идёт в GitHub Actions
(`.github/workflows/android-build.yml`), собирает debug APK при каждом пуше
в `android/**`, результат — артефакт `vending-kiosk-debug-apk`.

Локально (если открываете в Android Studio — она сама сгенерирует wrapper и
всё соберёт). Из командной строки без Android Studio: `cd android && gradle
wrapper --gradle-version 8.9 && ./gradlew :app:assembleDebug` (wrapper-jar
намеренно не закоммичен в репозиторий — CI ставит Gradle сам).

## Установка на планшет

```bash
adb install -r app-debug.apk
```

Приложение зарегистрировано и как `HOME`/`LAUNCHER` — при желании можно
назначить его лаунчером по умолчанию (`Настройки → Приложения по умолчанию`),
тогда после включения планшет сразу откроется в киоске.
