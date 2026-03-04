## Instalar Kubectl

TODO

## Instalar helm

```sh
curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4
chmod 700 get_helm.sh
./get_helm.sh
```

> **Referencias:**
>
> - [Instalar Helm](https://helm.sh/docs/intro/install)

## Instalar el Chart

```sh
helm repo add apache-airflow https://airflow.apache.org
helm upgrade --install airflow apache-airflow/airflow --namespace airflow --create-namespace
```

> Nota: este pasó tomo unos minutos

> **Referencias:**
>
> - [Helm Chart for Apache Airflow](https://airflow.apache.org/docs/helm-chart/stable/index.html)

## Actualizar el Chart

```sh
helm upgrade airflow apache-airflow/airflow --namespace airflow
```

## Listar namespaces

```sh
helm list --all-namespaces
```

## Obtener configuración del Chart

```sh
helm show values apache-airflow/airflow > values.yaml
```

## Redireccionar puerto 8080 al servicio Api Server

```sh
kubectl port-forward svc/airflow-api-server 8080:8080 --namespace airflow
```
