# Search

Сервис поиска медиа. PostgreSQL хранит проекции публикаций, текстовые признаки,
эмбеддинги и временные поисковые сессии. Используются FTS, pg_trgm и pgvector.

POST /v1/search поддерживает текстовый, семантический, гибридный поиск
и поиск по приватному образцу. Результаты сгруппированы по вариациям.
Семантические запросы обрабатываются через Processing и ML-воркер.
Временные запросы и файлы образцов удаляются после истечения срока хранения.

Настройки задаются окружением с префиксом MEMES_: DATABASE_URL,
INTERNAL_API_KEY, RELEASE_ID, ACCEPTED_EMBEDDING_VERSION,
адреса соседних сервисов, параметры S3, HOST и PORT.
Схемы REST API доступны в /docs.

Из корня репозитория после настройки окружения:

```sh
poetry run memes-search
poetry run memes-search-maintenance
```

Миграции выполняются отдельной командой memes-search-migrate до запуска API.

Тесты и фикстуры находятся в tests/. Из этой директории:

```sh
poetry install --with test
poetry run pytest -p no:cacheprovider
```

Интеграционные проверки требуют MEMES_SEARCH_TEST_DATABASE_URL
и параметров MEMES_SEARCH_TEST_S3_*.
