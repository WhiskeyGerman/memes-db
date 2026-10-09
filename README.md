# Memes DB

Приложение для публикации и поиска мемов. Frontend написан на TypeScript и React,
backend на Python и FastAPI. Состояние хранится в PostgreSQL, файлы медиа в S3.

## Запуск

Нужны Docker с Compose v2 и Python 3.11 или новее. Из корня проекта:

```sh
python deploy/scripts/generate_env.py --release-id submission-v1 --public-host localhost
docker compose --env-file deploy/.env -f deploy/compose.yaml build identity frontend
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

Генератор не перезаписывает существующий deploy/.env.
Frontend доступен на http://localhost:5173.
OpenAPI Identity на http://localhost:8080/docs, Catalog на порту 8081,
Processing на порту 8082, Search на порту 8083.
Для базовых операций ML-воркер и GPU не требуются.

## Структура

- frontend: пользовательский интерфейс.
- services/identity: аккаунты и авторизация.
- services/catalog: публикации и вариации.
- services/processing: задания обработки и регистрация воркеров.
- services/search: текстовый и семантический поиск.
- services/worker: клиент обработки медиа.
- packages: общие библиотеки.
- deploy: Docker Compose, миграции и административные скрипты.
- tests: проверки взаимодействия компонентов и архитектуры.

Код сервисов разделен на Domain, Application, Infrastructure и Presentation.

## Проверки

```sh
poetry install
poetry run ruff check .
poetry run ruff format --check .
poetry run mypy
poetry run pytest -p no:cacheprovider
```

Интеграционные тесты используют отдельные PostgreSQL/S3 ресурсы.
Без их конфигурации соответствующие проверки пропускаются.

Домен, CRUD и соответствие 12 факторам описаны в [отчете](Отчёт.md).
