# ML-воркер

Клиент обработки медиа. Получает задания исходящими REST-запросами к Processing,
читает файлы по подписанным S3-ссылкам и возвращает результаты.
Доступ к PostgreSQL и входящие подключения не требуются.

FFmpeg и Pillow создают превью. Дополнительные ML-адаптеры выполняют OCR,
распознавание речи и построение эмбеддингов. SQLite используется
как ограниченный TTL-кэш повторных результатов.

Обязательное окружение: MEMES_WORKER_COORDINATOR_URL,
MEMES_WORKER_WORKER_ID, MEMES_WORKER_TOKEN, MEMES_WORKER_RELEASE_ID.
MEMES_WORKER_OPERATIONS задает доступные операции,
MEMES_WORKER_DEVICE выбирает cpu или cuda.

Рабочий процесс запускается командой memes-worker.
Для контейнерного запуска используется deploy/compose.worker.yaml.
Модели готовятся отдельно и монтируются только для чтения.

```sh
memes-worker-prepare-model --model all --directory /models
```

Jina и Whisper загружаются с закрепленными ревизиями.
OCR использует detector PP-OCRv4 и распознаватели ru/en.
Контрольные суммы файлов записываются в manifests и проверяются при запуске.

Тесты и фикстуры находятся в tests/. Из этой директории:

```sh
poetry install --with test
poetry run pytest -p no:cacheprovider
```

Проверки реальных моделей запускаются отдельно с маркером worker_ml
и заранее подготовленными весами.
