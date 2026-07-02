# Vending QR Payment System

Оплата товаров на вендинговых автоматах XY-Vending через единый QR (JetQR/Aliftech)
с автоматической выдачей и возвратом денег при сбое. Референсная архитектура —
[parking-project](https://github.com/Adminpm04/parking-project).

Полный контекст архитектуры: [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md).
Протокол автомата: [docs/VMC-Upper computer_V3.0_0411.pdf](docs/).

## Структура

```
backend/          FastAPI-сервер (один на все точки)
  main.py         kiosk API, WebSocket точек, поллинг JetQR, refund, admin API
  jetqr.py        создание/проверка/отмена инвойсов JetQR
  database.py     VendingMachine, ProductSlot, VendingSession, Blacklist
  vmc_protocol.py протокол RS232 XY-Vending (используется контроллером)
  config.py       настройки (.env)
controller/       агент точки — планшет/Pi рядом с VMC
  controller.py   RS232 POLL/ACK-цикл + WebSocket к серверу
frontend/
  kiosk.html      экран клиента: каталог → QR → статус выдачи
  admin.html      админка: точки, слоты, сессии, возвраты, статистика
```

## Запуск сервера

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp backend/.env.example backend/.env   # заполнить JetQR-реквизиты
cd backend && ../venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```

- Kiosk: `http://server:8000/kiosk/{machine_id}` (fullscreen-браузер на планшете)
- Админка: `http://server:8000/admin` (нужен ADMIN_TOKEN)

## Запуск контроллера точки

```bash
pip install pyserial websockets
cp controller/.env.example controller/.env   # MACHINE_ID + токен из админки
python3 controller/controller.py
```

## Поток покупки

1. Клиент выбирает товар на kiosk-экране → `POST /api/kiosk/{mid}/buy`
2. Сервер создаёт `VendingSession` + инвойс JetQR → kiosk показывает QR
3. Сервер поллит JetQR каждые 2 с → оплата → команда `dispense` по WebSocket контроллеру
4. Контроллер шлёт VMC `0x03` (выдача), ловит `0x04` (статус)
5. Успех (`0x02`) → сессия `dispensed`, остаток −1. Ошибка → автоматический возврат JetQR

## Статусы сессии

`pending → paid → dispensing → dispensed` | `refund_pending → refunded` | `expired`

## Открытые вопросы (уточнить у поставщиков)

- Точный merchant-facing endpoint возврата JetQR (сейчас — настройка `JETQR_CANCEL_PATH`)
- Много `terminal_id` на одного мерчанта (по одному на точку)
- Физический RS232 на XY-SLY-5C-002BL / переходник USB-RS232
