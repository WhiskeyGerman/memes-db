# Envoy Gateway v1.9.2

[kustomization.yaml](kustomization.yaml) подключает
[официальный установочный bundle v1.9.2](https://github.com/envoyproxy/gateway/releases/download/v1.9.2/install.yaml).
Он содержит CRD Gateway API и Envoy Gateway, RBAC, контроллер и Job
для генерации внутренних сертификатов.

[controller-patch.yaml](controller-patch.yaml) задаёт одну реплику контроллера,
стратегию `Recreate` и requests/limits. Отдельный Envoy Proxy настраивается
через [gateway-class.yaml](../../gateway-class.yaml).

Из корня репозитория:

```shell
kubectl apply --server-side -k deploy/k8s/addons/envoy-gateway
```

Нужен доступ к GitHub для загрузки bundle и к реестрам контейнерных образов.
Состав стенда и маршрутизация описаны в
[отчете о развёртывании](<../../../../docs/Отчет №2.md>).
Установка рассчитана на minikube без CRD Gateway API, управляемых другим
контроллером. [Документация установки](https://gateway.envoyproxy.io/docs/install/install-yaml/).

