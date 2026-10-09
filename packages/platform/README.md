# Memes Platform

Общие Application порты и Infrastructure/Presentation адаптеры.

## Локальные тесты

Тесты и фикстуры находятся в `tests`. Из директории компонента:

```shell
poetry install --with test
poetry run pytest -p no:cacheprovider
```

Проверки Ruff и mypy запускаются из корня репозитория.
