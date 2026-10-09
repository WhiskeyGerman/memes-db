# Identity

Сервис аккаунтов и авторизации. PostgreSQL хранит пользователей, отпечатки
токенов и очередь уведомлений об удалении аккаунта. Пароли защищены Argon2id.

Основные маршруты: POST /v1/auth/register, POST /v1/auth/login,
GET /v1/auth/me, PATCH /v1/auth/login, PATCH /v1/auth/password,
DELETE /v1/auth/account. Подробные схемы доступны в /docs.
Access token передается как Bearer, refresh token хранится в HttpOnly cookie.
Для регистрации, входа и обновления сессии настроены проверки Origin и CSRF.

Настройки задаются окружением с префиксом MEMES_:
DATABASE_URL, INTERNAL_API_KEY, RELEASE_ID, CSRF_ALLOWED_ORIGINS,
CORS_ALLOWED_ORIGINS, HOST, PORT, адреса соседних API и параметры S3.
Полный состав окружения указан в deploy/compose.yaml.

Из корня репозитория после настройки окружения:

```sh
poetry run memes-identity
poetry run python -m memes_identity.presentation.outbox
poetry run python -m memes_identity.presentation.admin operator --login admin_name
```

Создание оператора запрашивает пароль скрытым вводом.
Миграции запускаются отдельным процессом до API.

Тесты и фикстуры находятся в tests/. Из этой директории:

```sh
poetry install --with test
poetry run pytest -p no:cacheprovider
```

Интеграционные проверки требуют MEMES_TEST_IDENTITY_DATABASE_URL.
