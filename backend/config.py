from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql://vending:vending123@localhost/vending_db"

    # JetQR API
    JETQR_BASE_URL: str = "https://dev-jetqr.aliftech.net/test"
    JETQR_API_KEY: str = ""
    JETQR_MERCHANT_ID: str = ""
    JETQR_STORE_ID: str = ""
    JETQR_TERMINAL_ID: str = ""
    JETQR_MIS_TERMINAL_ID: str = "MIS-VND-001"

    # Merchant-facing cancel/refund endpoint — точный путь уточняется у JetQR/Aliftech,
    # поэтому вынесен в настройку, а не захардкожен.
    JETQR_CANCEL_PATH: str = "/api/v1/merchant/invoice/cancel"

    # Платёжный провайдер: "jetqr" | "expresspay". Пока не переключаем боевой
    # флоу — ExpressPay включается сменой этой настройки, когда банк откроет
    # endpoint статуса (getpaystatus сейчас отдаёт 404, нужен наш IP в белом
    # списке банка).
    PAYMENT_PROVIDER: str = "jetqr"

    # ExpressPay / DCWallet (Dushanbe City)
    # База QR: pay.dc.tj (подтверждена поставщиком 2026-07; ранее была pay.expresspay.tj).
    EXPRESSPAY_QR_BASE: str = "https://pay.dc.tj/"
    # Endpoint статуса оплаты — точный путь ещё уточняется у поставщика
    # (текущий /v3/vending отдаёт 404 «Not Found»). Вынесен в настройку.
    EXPRESSPAY_STATUS_URL: str = "https://api1.dc.tj/v3/vending"
    # Глобальный pan-fallback. Реальный pan берётся с автомата (VendingMachine.expresspay_pan),
    # т.к. у каждой точки свой QR/pan; это значение используется, только если у точки не задан свой.
    EXPRESSPAY_CARD: str = "5058270380027408"   # pan — карта получателя (из QR-наклейки)
    EXPRESSPAY_ARTICLE: str = "133"             # f1 — артикул услуги для вендинга (указан провайдером: F1=133)
    EXPRESSPAY_SECRET: str = "ddbbcff0-9db0-4897-9d38-b80babe32306"  # секрет для md5-подписи

    # Payment
    PAYMENT_POLL_INTERVAL: float = 2.0    # сек; чаще нельзя — JetQR блокирует терминал
    PAYMENT_POLL_TIMEOUT: int = 300       # сек; сколько ждём оплату после показа QR
    DISPENSE_TIMEOUT: int = 60            # сек; сколько ждём результат выдачи от контроллера

    # Номер поддержки — показывается клиенту на кассе, если оплата прошла, а
    # товар не выпал. Возврат не автоматический: оператор решает по звонку
    # (выдать вручную через админку или вернуть деньги).
    SUPPORT_PHONE: str = "5454"

    # Admin — стартовый аккаунт создаётся один раз при первом запуске, если
    # таблица пользователей пуста. Дальше пользователей заводит сам admin
    # через раздел «Пользователи» в панели.
    ADMIN_BOOTSTRAP_USERNAME: str = "admin"
    ADMIN_BOOTSTRAP_PASSWORD: str = "change-me"

    # Каталог для загруженных фото товаров. Вне git-дерева, чтобы деплой
    # (git reset --hard) их не трогал. Отдаётся статикой по /uploads.
    UPLOAD_DIR: str = "/opt/vending-project/uploads"
    UPLOAD_MAX_BYTES: int = 4_000_000   # ~4 МБ на фото

    class Config:
        env_file = ".env"


settings = Settings()
