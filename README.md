# FAQ-бот для ВКонтакте

Детерминированный FAQ-бот: фактические ответы берутся только из SQLite-таблицы `faq`. Проект не использует генеративный ИИ.

## Architecture

```text
VK Group Long Poll event
  -> vk.events (validation and typed model)
  -> vk.adapter (deduplication, rate/size limits)
  -> BotLogic.handle_message / select_clarification
  -> BotResponse
  -> vk.adapter (text and keyboard mapping)
  -> VKClientProtocol
```

`bot_logic.py` не импортирует VK-код и не выполняет сетевых запросов. Каждое событие получает отдельное SQLite-соединение.

Обработка имеет **effectively-once** семантику с приоритетом защиты от дубликатов: `event_id` атомарно резервируется до бизнес-логики. После успешной отправки статус становится `DONE`. Сбой процесса между фиксацией бизнес-логики и сетевой отправкой может привести к пропущенному ответу, зато повторная доставка не вызовет повторное переключение режима или дублирование истории. Зависшие `PROCESSING` и `FAILED` записи блокируют повтор до истечения настраиваемого dedup TTL, после чего атомарно перехватываются следующим событием либо удаляются maintenance-командой. Распределённая транзакция между SQLite и VK API намеренно не реализуется.

## Mock mode

Mock mode является режимом по умолчанию, не требует `VK_TOKEN` и не выполняет сетевых запросов:

```bash
python run_vk_bot.py
```

Для интерактивной проверки доменной логики также доступен:

```bash
python cli.py
```

## Production prerequisites

Для первого реального запуска понадобятся токен сообщества, ID сообщества, разрешение на сообщения и включённый Group Long Poll с событием `message_new`. Production-базу следует размещать вне Git checkout через `BOT_DB_PATH`.

После настройки окружения точка входа `run_vk_bot.py` создаёт `RealVKClient`, получает Group Long Poll events и передаёт их изолированному адаптеру. Конструирование клиента не выполняет сетевой запрос.

## Environment variables

- `VK_TOKEN`
- `VK_GROUP_ID`
- `VK_API_VERSION`
- `VK_MODE`
- `VK_RATE_LIMIT_PER_MINUTE`
- `VK_EVENT_DEDUP_TTL_SECONDS`
- `VK_MAX_INPUT_LENGTH`
- `BOT_DB_PATH`

Значения-примеры без секретов находятся в `.env.example`. Файл `.env` не загружается автоматически и исключён из Git.

## Security

- Токен не записывается в SQLite, ответы, exception text или application logs.
- `processed_vk_events.event_id` защищает от повторной доставки.
- Sliding-window rate limiter применяется отдельно к каждому пользователю.
- Слишком длинные сообщения не передаются в FAQ search и историю.
- Clarification callback содержит `faq_id` и случайный nonce; сервер проверяет пользователя, nonce, TTL, предложенный ID и `active`.
- `OPERATOR_MODE` не вызывает отправку пустого сообщения.
- Логи содержат correlation ID (`event_id`), но не полный event payload и не полный пользовательский текст.
- Runtime production DB должна находиться вне репозитория; `data/bot.db` — только очищенный dev artifact.

## Retention

Удаление просроченных event ID, clarification, старых сообщений и неизвестных вопросов:

```bash
python maintenance.py --db data/bot.db --messages-days 90
```

Репозиторий взаимодействий также поддерживает удаление данных конкретного `vk_user_id`.

## Run tests

```bash
python import_docx.py
python -m pytest -q
python -m compileall -q .
```

Тесты VK полностью используют `MockVKClient`; настоящий VK API во время тестов не вызывается.
