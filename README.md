<h1 align="center">LootlyHub</h1>
<p align="center">Бот автоматизации магазина на <a href="https://lootly.net">Lootly</a> через официальный
<a href="https://docs.seller.lootly.net/ru/docs">Seller API</a></p>

# <p align="center"><a href="https://t.me/fairyvpn_bot">VPN</a></p></p>

Если проект полезен - поставь ⭐  
Связаться / отблагодарить - Telegram [@fairydarkns](https://t.me/fairydarkns)

Бот продавца для [IziMarket](https://izi.market): автовыдача, автодеактивация лотов, пополнение склада
и управление магазином из Telegram. Работает через официальное
[публичное API продавца](https://izi.market/settings/api/docs).

**Автор:** [@fairydarkns](https://t.me/fairydarkns)

## Возможности

- **Автовыдача** ключей, аккаунтов и кодов из `goods/*.txt` (одна строка — одна единица). Если в заказе `quantity > 1`, выдаётся сразу нужное число строк.
- **Статичная выдача**: ссылка или инструкция, которая не расходуется.
- **Своя выдача через плагин**: например, генерация кода через API поставщика.
- **Автоответ** на команды в чате заказа (`!помощь`, `!заказ` …) с переменными.
- **Приветствие** после взятия заказа и **сообщение после выдачи**.
- **Автоскрытие** товара, когда кончился файл, и возврат в продажу при пополнении.
- **Нет товара**: бот уведомляет, может написать покупателю или отменить заказ с возвратом. Когда файл пополнят, выдаёт сам.
- **Вебхук «запрос данных»**: проверка того, что вводит покупатель (логин, ID и т.п.), регулярками или плагином.
- **Панель в Telegram**:
  - уведомления о заказах, выдачах и сообщениях;
  - ответ покупателю прямо из ТГ;
  - ручная выдача и отмена;
  - пополнение `goods` текстом или `.txt`-файлом;
  - переключатели функций.
- **Надёжность**:
  - идемпотентные `take/deliver/cancel`;
  - ретраи при 429/5xx;
  - если бот упал посреди выдачи, после перезапуска он доведёт её с теми же ключами, ничего не потеряет и не выдаст дважды;
  - если заказ отменили или его взял человек, ключи возвращаются в файл.

## Установка

Нужен Python 3.11+.

```bash
git clone <repo> LootlyHub && cd LootlyHub
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp configs/main.example.toml configs/main.toml
cp configs/delivery.example.toml configs/delivery.toml
cp configs/autoresponse.example.toml configs/autoresponse.toml
```

1. **API-ключ**: Магазин → Настройки → API ключи → Сгенерировать. Ключ вставьте в `lootly.api_key`. Он показывается **один раз**.
2. **Внешняя автоматизация**: включите её у товаров, которые должен выдавать бот. Без этого их заказы в API не попадут.
3. **Telegram**:
   - токен от [@BotFather](https://t.me/BotFather) вставьте в `telegram.token`;
   - свой Telegram ID впишите в `telegram.admins`;
   - или включите вход по паролю: `python main.py hash-password`, результат вставьте в `telegram.password_hash`.
4. **Проверка**: `python main.py check` проверит ключ и покажет, какие товары из `delivery.toml` не найдены или не под автоматизацией.
5. **Запуск**: `python main.py` (подробные логи: `--debug`).

### Docker

```bash
docker compose up -d --build
```

## Автовыдача — `configs/delivery.toml`

```toml
[[product]]
id = "6a0593652deddeb68627138c"   # или title = "Точное название"
goods = "steam_keys.txt"          # goods/steam_keys.txt, по ключу на строку
message = "Ваш ключ: $value"       # необязательно, иначе messages.after_delivery

[[product]]
title = "Гайд"
value = "https://example.com/guide"
```

ID товаров показывают `/products` в Telegram и `python main.py check`. После правки файла отправьте `/reload` в ТГ.

**Переменные в текстах:** `$order_id`, `$product`, `$quantity`, `$amount`, `$value`, `$fields`, `$date`.

## Как работает

```
GET /orders?status=new  ──► новый заказ ──► есть правило? ──нет──► уведомление в ТГ, выдача вручную
   (каждые 10с)                                   │ да
                                                  ▼
                             резерв строк из goods/ (записывается в БД)
                                                  ▼
                 POST /take ─► приветствие ─► POST /deliver ─► сообщение после выдачи
                                                  ▼
                       статус awaiting_confirmation: деньги придут, когда покупатель подтвердит
```

Чаты заказов (новые, в работе, ждут подтверждения) опрашиваются каждые 20 с. Push-вебхуков на заказы у Lootly пока нет, поэтому бот опрашивает API. Не ставьте интервалы слишком маленькими: лимиты считаются по ключу.

## Telegram

| | |
|---|---|
| 📦 Заказы, `/orders [new\|processing\|awaiting\|all]` | список заказов, карточка с кнопками Ответить / Выдать / Отменить |
| `/order <id>` | карточка заказа |
| 🗃 Товары, `/goods` | остатки в `goods/` и кнопки пополнения |
| `/addgoods имя.txt` | пополнить или создать файл: текстом или `.txt` |
| `/products` | товары магазина и их правила выдачи |
| ⚙️ Настройки | переключатели (сохраняются в `storage/`) |
| `/reload` | перечитать `delivery.toml` и `autoresponse.toml` |
| `/plugins`, `/vars`, `/status`, `/cancel` | плагины, переменные, статус, отмена ввода |

## Плагины

Плагин — это `.py` файл в `plugins/` с функцией `setup(hub)`. Пример: [`plugins/_example.py`](plugins/_example.py). Файлы с `_` в начале имени не загружаются.

```python
from lootlyhub.events import Event

NAME, VERSION, AUTHOR, DESCRIPTION = "Мой плагин", "1.0", "@me", "..."

def setup(hub):
    @hub.events.on(Event.NEW_ORDER)
    async def on_order(hub, order): ...

    hub.add_deliverer(my_fn, product_id="...")   # своя выдача: fn(order) -> list[str] | None
    hub.add_data_validator(check_fn)             # вебхук: fn(product_id, field_id, label, value) -> bool | None
```

**События:** `started`, `new_order`, `order_delivered`, `order_cancelled`, `no_stock`, `delivery_failed`, `new_message`, `command`, `product_hidden`.

Через `hub.api` доступен полный клиент Seller API: товары, категории, медиа, курсы валют, заказы, чат.

> ⚠️ Плагин выполняется с правами бота и видит API-ключ, а ключ даёт полный доступ к магазину, включая отмены с возвратом. Ставьте только плагины, код которых вы прочитали. Загрузки плагинов через Telegram нет специально.

## Безопасность

- `configs/*.toml`, `goods/` и `storage/` есть в `.gitignore`, не публикуйте их.
- Если ключ утёк, отзовите его в настройках магазина и создайте новый.
- Вход в ПУ по паролю: 5 попыток на аккаунт, счётчик хранится на диске. Сообщение с паролем бот удаляет.
- Вебхук «запрос данных» не подписывается Lootly. Добавьте в `webhook.path` случайную строку и выставьте его наружу только через HTTPS (nginx или Caddy).

## Разработка

```bash
pip install -e ".[dev]"
pytest
```

Тесты гоняют весь цикл выдачи против имитации API (`tests/fake_lootly.py`).

---

LootlyHub · автор **@fairydarkns** · идея в духе FunPay Cardinal, код написан с нуля под Seller API Lootly.
