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

    # Payment
    PAYMENT_POLL_INTERVAL: float = 2.0    # сек; чаще нельзя — JetQR блокирует терминал
    PAYMENT_POLL_TIMEOUT: int = 300       # сек; сколько ждём оплату после показа QR
    DISPENSE_TIMEOUT: int = 60            # сек; сколько ждём результат выдачи от контроллера

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
