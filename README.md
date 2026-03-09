# Guía de Instalación y Configuración de Apache Airflow (Local)

Esta guía documenta los pasos necesarios para configurar Apache Airflow 3.1.7 en un entorno de desarrollo local utilizando Helm v4+ y Kubernetes (idealmente con Docker Desktop).

## 1. Prerrequisitos

Antes de instalar el Chart de Airflow, es necesario contar con las siguientes herramientas instaladas. Si utilizas **Docker Desktop**, puedes habilitar Kubernetes directamente desde su configuración, lo cual incluye `kubectl`.

### Instalar Kubectl (Omitir si usas Docker Desktop con Kubernetes activado)

```sh
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl.sha256"
echo "$(cat kubectl.sha256)  kubectl" | sha256sum --check
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
kubectl version --client --output=yaml
```

### Instalar Helm (v4+)

```sh
curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4
chmod 700 get_helm.sh
./get_helm.sh
```

> **Referencias:** [Guía oficial de instalación de Helm](https://helm.sh/docs/intro/install)

## 2. Instalación Local de Airflow (Entorno Virtual para Desarrollo)

Para el desarrollo local de DAGs (autocompletado, linting y pruebas) y la correcta resolución de dependencias en Python, crea y configura un entorno virtual:

```sh
python3 -m venv .venv
source .venv/bin/activate

AIRFLOW_VERSION=3.1.7
PYTHON_VERSION="$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
CONSTRAINT_URL="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

pip install "apache-airflow[postgres,fab,otel]==${AIRFLOW_VERSION}" --constraint "${CONSTRAINT_URL}"
```

## 3. Preparación del Despliegue en Kubernetes

Antes de instalar Airflow, es necesario preparar el entorno, lo cual incluye la creación de una imagen Docker personalizada con las dependencias del proyecto, descargar la configuración base y preparar las credenciales para sincronizar tus DAGs (GitSync).

### 3.1 Instalar PostgreSQL con CloudNativePG Helm Charts

**1. Instalar charts**

```sh
helm repo add cnpg https://cloudnative-pg.github.io/charts
helm upgrade --install cnpg \
  --namespace cnpg-system \
  --create-namespace \
  cnpg/cloudnative-pg

helm upgrade --install database \
  --namespace database \
  --create-namespace \
  cnpg/cluster
```

**2. Revisar instalación**

Comandos provistos por la misma instalación de `cnpg/cluster`

```sh
# Run Helm Tests
helm test --namespace database database

# Get a list of all base backups
kubectl --namespace database get backups --selector cnpg.io/cluster=database-cluster

# Connect to the cluster's primary instance
kubectl --namespace database exec --stdin --tty services/database-cluster-rw -- bash
```

**3. Crear base de datos para MLFlow**

```sh
# Conectar a la instancia primaria del cluster
kubectl --namespace database exec --stdin --tty services/database-cluster-rw -- bash
psql
```

En la consola de PostgreSQL:

```sql
-- Crear base de datos para MLFlow
CREATE DATABASE mlflow_db;

-- Para listar las bases de datos
\l

-- Para conectar a la base de datos MLFlow
\c mlflow_db

-- Para listar tablas y otros (vacío si se creó recién la BD)
\dt
```

> **Referencia**
>
> - https://cloudnative-pg.io/docs/1.28/installation_upgrade/#details-about-the-deployment
> - https://github.com/cloudnative-pg/charts
> - https://github.com/cloudnative-pg/charts/blob/main/charts/cluster/README.md
> - https://github.com/cloudnative-pg/charts/blob/main/charts/cluster/docs/Getting%20Started.md

**4. Obtener servicio PostgreSQL**

El servicio `-rw` es el que usaremos para poder leer y escribir. Dado que MLFlow se despliega en un namespace distinto (`airflow`) al de la base de datos (`database`), debemos utilizar su Fully Qualified Domain Name (FQDN) al configurarlo en el `--backend-store-uri` del archivo **[mlflow-deployment](mlflow-deployment.yaml)**. Es decir, usaremos `database-cluster-rw.database.svc.cluster.local`.

```sh
kubectl get svc -n database

# Retornará algo como
#NAME                  TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE
#database-cluster-r    ClusterIP   10.102.73.18    <none>        5432/TCP   53m
#database-cluster-ro   ClusterIP   10.96.243.211   <none>        5432/TCP   53m
#database-cluster-rw   ClusterIP   10.100.145.95   <none>        5432/TCP   53m
```

### 3.2 Creación de la Imagen Docker Personalizada

Dado que este proyecto requiere librerías específicas (como `pandas`, `scikit-learn` o `mlflow`) que no vienen preinstaladas en la imagen oficial del Helm Chart de Apache Airflow, es necesario construir una imagen propia.

**1. Archivo de configuración ([Dockerfile](Dockerfile))**

El proyecto incluye un archivo `Dockerfile` en el directorio raíz en el que se especifican las dependencias adicionales a instalar sobre la versión base de Airflow:

```dockerfile
FROM apache/airflow:3.1.7
RUN pip install scikit-learn==1.8.0
RUN pip install pandas==3.0.1
RUN pip install mlflow==3.10.1
```

**2. Construcción de la imagen**

Ejecuta el siguiente comando para construir la imagen del contenedor localmente. La etiqueta (`airflow-custom:0.0.1`) asignada aquí será la que utilicemos posteriormente en la configuración de Helm:

```sh
docker build --pull --tag airflow-custom:0.0.1 .
```

### 3.3 Añadir el Repositorio de Helm

Añade el repositorio oficial del Chart de Apache Airflow y actualiza el índice de paquetes:

```sh
helm repo add apache-airflow https://airflow.apache.org
helm repo update
```

### 3.4 Crear Namespace en Kubernetes

Crea el namespace dedicado donde residirán todos los componentes de Apache Airflow:

```sh
kubectl create namespace airflow
```

### 3.5 Extraer Configuración Base (`values.yaml`)

Extrae los valores por defecto del Chart hacia un archivo local (`values.yaml`). Este archivo servirá como plantilla base para personalizar tu despliegue de forma estructurada e incluir nuestra imagen Docker:

```sh
helm show values apache-airflow/airflow > values.yaml
```

### 3.6 Crear Credenciales Git (GitSync)

Para que Airflow sincronice tus DAGs automáticamente desde tu repositorio, debes configurar un secreto en Kubernetes que contenga las credenciales de GitHub.

**Prerrequisitos:**

1. Crear un Personal Access Token (PAT) en GitHub (se recomienda la opción "Fine-grained").
2. Asignar el permiso de sólo lectura para código (`Contents: Read`).

```sh
# Eliminar el secreto en caso de que ya exista para evitar conflictos
kubectl delete secret git-credentials -n airflow --ignore-not-found

# Generar el secreto en Kubernetes
kubectl create secret generic git-credentials \
  --from-literal=GITSYNC_USERNAME='<usuario_github>' \
  --from-literal=GITSYNC_PASSWORD='<PAT_github>' \
  --from-literal=GIT_SYNC_USERNAME='<usuario_github>' \
  --from-literal=GIT_SYNC_PASSWORD='<PAT_github>' \
  -n airflow
```

### 3.7 Levantar servidor MLFlow

La configuración de MLFlow se encuentra en los archivos **[MLFlow Deployment](mlflow-deployment.yaml)** y **[MLFlow Service](mlflow-service.yaml)**.

El deployment referencia el secret `database-cluster-superuser` (generado automáticamente por CloudNativePG) a través de variables de entorno (`DB_USER` y `DB_PASSWORD`), por lo que la contraseña nunca queda expuesta en texto plano en los archivos del proyecto.

**1. Aplicar archivos de configuración**

```sh
kubectl apply -f mlflow-deployment.yaml
kubectl apply -f mlflow-service.yaml
```

**2. Redireccionar el puerto a la interfaz de MLFlow**

```sh
# Se usa el puerto 30500 ya que el puerto 5000
# en windows está usado por otra aplicación (al menos en mi equipo)
kubectl port-forward svc/mlflow 30500:5000 -n airflow
```

### 3.8 Configurar `values.yaml`

Abre el archivo `values.yaml` descargado en el paso 3.5 y asegúrate de añadir y habilitar la sección `gitSync` para que Airflow monte el volumen correctamente y lea los DAGs. También deberás configurar la imagen oficial para utilizar nuestra imagen Docker compilada y apuntar a la rama y entorno correcto:

```yaml
# Configurar la imagen personalizada de Airflow
images:
  airflow:
    repository: airflow-custom
    tag: 0.0.1

# Sincronizar DAGs desde Git
dags:
  gitSync:
    enabled: true
    repo: https://github.com/ericmartinezr/airflow-helm.git
    branch: dev
    rev: HEAD
    ref: dev
    subPath: 'src/dags'
    credentialsSecret: git-credentials

# Logging
logs:
  persistence:
    enabled: true

# Probablemente no es requerido en todos estos, pero así me funcionó lol.
workers:
  extraVolumes:
    - name: tmp-files
      persistentVolumeClaim:
        claimName: airflow-tmp-files-pvc
  extraVolumeMounts:
    - name: tmp-files
      mountPath: '{{ .Values.airflowHome }}/tmp'

scheduler:
  extraVolumes:
    - name: tmp-files
      persistentVolumeClaim:
        claimName: airflow-tmp-files-pvc
  extraVolumeMounts:
    - name: tmp-files
      mountPath: '{{ .Values.airflowHome }}/tmp'

apiServer:
  extraVolumes:
    - name: tmp-files
      persistentVolumeClaim:
        claimName: airflow-tmp-files-pvc
  extraVolumeMounts:
    - name: tmp-files
      mountPath: '{{ .Values.airflowHome }}/tmp'

webserver:
  extraVolumes:
    - name: tmp-files
      persistentVolumeClaim:
        claimName: airflow-tmp-files-pvc
  extraVolumeMounts:
    - name: tmp-files
      mountPath: '{{ .Values.airflowHome }}/tmp'
```

## 4. Despliegue en Kubernetes mediante Helm

### 4.1 Instalar el Chart de Airflow

Con la configuración y secretos listos, procede a instalar Airflow aplicando tu archivo `values.yaml` personalizado:

```sh
helm install airflow apache-airflow/airflow --namespace airflow --create-namespace -f values.yaml
```

> **Nota:** La descarga de imágenes y creación de contenedores puede tomar unos minutos la primera vez.<br>
> **Referencias:** [Helm Chart for Apache Airflow](https://airflow.apache.org/docs/helm-chart/stable/index.html)

### 4.2 Comprobar el Despliegue

Verifica que los pods se estén iniciando correctamente:

```sh
kubectl get pods -n airflow
```

También puedes comprobar el estado general del chart instalado en Helm:

```sh
helm list -n airflow
```

## 5. Mantenimiento y Actualizaciones

### Actualizar el Chart

Si posteriormente realizas cambios en tu archivo `values.yaml` (ej. cambiar configuraciones, habilitar nuevos servicios), aplica los cambios con el comando de actualización:

```sh
helm upgrade airflow apache-airflow/airflow -n airflow -f values.yaml
```

### Eliminar el Despliegue

En caso de requerir una instalación desde cero o si ocurren errores irrecuperables, puedes limpiar todo el entorno:

```sh
helm uninstall airflow -n airflow
kubectl delete all --all -n airflow
```

## 6. Operaciones y Monitoreo

### Redireccionar el Puerto de la Interfaz Web (UI)

Para acceder a la consola web de Airflow de manera local, redirecciona el puerto `8080` de tu equipo al servicio correspondiente de la API/UI en Kubernetes.

```sh
kubectl port-forward svc/airflow-api-server 8080:8080 --namespace airflow
```

_Accede desde el navegador a: [http://localhost:8080](http://localhost:8080)_

### Revisar Logs de los Componentes

Si algo no está funcionando como se espera o algún DAG no aparece (como problemas con GitSync), puedes revisar los registros (logs) de los pods:

```sh
# 1. Obtener la lista de los pods activos
kubectl get pods -n airflow

# 2. Ver registros de un pod específico
kubectl logs pod/<nombre-del-pod-api-server> -n airflow
kubectl logs pod/<nombre-del-pod-dag-processor> -n airflow
kubectl logs pod/<nombre-del-pod-scheduler> -n airflow
kubectl logs pod/<nombre-del-pod-triggerer> -n airflow

# 3. Si un pod no inicia, inspeccionar los eventos de Kubernetes
kubectl describe pod <nombre-del-pod> -n airflow
kubectl get events -n airflow
```

### Acceder a la Terminal (Shell) de un Pod

Muy útil para validar si un DAG en particular se ha sincronizado y existe dentro del sistema de archivos del contenedor:

```sh
kubectl exec -it -n airflow <nombre-del-pod-scheduler> -- sh
```

_(Nota: Dentro del pod, los DAGs típicamente se sincronizan en `/opt/airflow/dags`)_

## 7. Material de Referencia

Recursos recomendados para ampliar información sobre la herramienta y el Helm Chart:

- [Airflow Helm Chart Quick start for Beginners (Apuntes Notion)](https://robust-dinosaur-2ef.notion.site/Airflow-Helm-Chart-Quick-start-for-Beginners-3e8ee61c8e234a0fb775a07f38a0a8d4)
- [Video explicativo original - YouTube](https://www.youtube.com/watch?v=GDOw8ByzMyY)

---

> **Nota:** Los archivos de documentación correspondiente fueron generados o asistidos mediante el uso de inteligencia artificial.
