/*
 * JNI-мост к RS232-порту VMC через стандартные POSIX termios-вызовы.
 * Порт /dev/ttyS1 на этом железе открыт всем приложениям (crwxrwxrwx),
 * поэтому root не требуется — достаточно open()/tcsetattr().
 *
 * Реализует тот же паттерн, что используют промышленные android-serialport
 * библиотеки (та же схема работает в заводском приложении на этом автомате).
 */
#include <termios.h>
#include <fcntl.h>
#include <errno.h>
#include <string.h>
#include <unistd.h>
#include <jni.h>
#include <android/log.h>

#define LOG_TAG "VendingSerial"
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)

static speed_t baudToSpeed(jint baudrate) {
    switch (baudrate) {
        case 1200: return B1200;
        case 2400: return B2400;
        case 4800: return B4800;
        case 9600: return B9600;
        case 19200: return B19200;
        case 38400: return B38400;
        case 57600: return B57600;
        case 115200: return B115200;
        default: return (speed_t) -1;
    }
}

JNIEXPORT jobject JNICALL
Java_com_vendingqr_kiosk_serial_SerialPort_nativeOpen(JNIEnv *env, jclass clazz,
                                                        jstring path, jint baudrate) {
    const char *pathChars = (*env)->GetStringUTFChars(env, path, NULL);
    int fd = open(pathChars, O_RDWR | O_NOCTTY | O_NDELAY);
    (*env)->ReleaseStringUTFChars(env, path, pathChars);

    if (fd < 0) {
        LOGE("open() failed: %s", strerror(errno));
        return NULL;
    }

    // Убираем O_NDELAY после открытия, чтобы read() блокировался с VTIME (см. ниже).
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags & ~O_NDELAY);

    speed_t speed = baudToSpeed(baudrate);
    if (speed == (speed_t) -1) {
        LOGE("unsupported baudrate: %d", baudrate);
        close(fd);
        return NULL;
    }

    struct termios cfg;
    if (tcgetattr(fd, &cfg) != 0) {
        LOGE("tcgetattr failed: %s", strerror(errno));
        close(fd);
        return NULL;
    }

    cfmakeraw(&cfg);
    cfsetispeed(&cfg, speed);
    cfsetospeed(&cfg, speed);

    // Протокол VMC: 8 data bits, no parity, 1 stop bit (8N1).
    cfg.c_cflag |= (CLOCAL | CREAD);
    cfg.c_cflag &= ~PARENB;
    cfg.c_cflag &= ~CSTOPB;
    cfg.c_cflag &= ~CSIZE;
    cfg.c_cflag |= CS8;
    cfg.c_cflag &= ~CRTSCTS;   // без аппаратного управления потоком

    // read() возвращается как только есть хоть 1 байт, либо через 100мс простоя —
    // важно для быстрого ответа на POLL от VMC (окно 100мс на ответ).
    cfg.c_cc[VMIN] = 0;
    cfg.c_cc[VTIME] = 1;

    if (tcsetattr(fd, TCSANOW, &cfg) != 0) {
        LOGE("tcsetattr failed: %s", strerror(errno));
        close(fd);
        return NULL;
    }
    tcflush(fd, TCIOFLUSH);

    jclass fdClass = (*env)->FindClass(env, "java/io/FileDescriptor");
    jmethodID fdInit = (*env)->GetMethodID(env, fdClass, "<init>", "()V");
    jfieldID descField = (*env)->GetFieldID(env, fdClass, "descriptor", "I");

    jobject fileDescriptor = (*env)->NewObject(env, fdClass, fdInit);
    (*env)->SetIntField(env, fileDescriptor, descField, fd);

    LOGI("serial port opened fd=%d baud=%d", fd, baudrate);
    return fileDescriptor;
}

JNIEXPORT void JNICALL
Java_com_vendingqr_kiosk_serial_SerialPort_nativeClose(JNIEnv *env, jclass clazz, jint fd) {
    if (fd >= 0) {
        close(fd);
        LOGI("serial port closed fd=%d", fd);
    }
}
