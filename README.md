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
RUN pip install great_expectations==1.14.0
RUN pip install google-cloud-storage
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

### 3.5 Extraer Configuración Base (`airflow-values.yaml`)

Extrae los valores por defecto del Chart hacia un archivo local (`airflow-values.yaml`). Este archivo servirá como plantilla base para personalizar tu despliegue de forma estructurada e incluir nuestra imagen Docker:

```sh
helm show values apache-airflow/airflow > airflow-values.yaml
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

### 3.7 Configuración de Almacenamiento en Google Cloud (GCS)

Para utilizar Google Cloud Storage como Artifact Store de MLflow, es necesario crear un bucket y una cuenta de servicio (Service Account) con los permisos adecuados.

**1. Configurar proyecto y autenticación**

```sh
export PROJECT_ID="k8s-mlflow"

gcloud auth application-default set-quota-project $PROJECT_ID
gcloud config set project $PROJECT_ID
gcloud auth login
```

**2. Crear el bucket**

```sh
gcloud storage buckets create --recursive gs://k8s-mlflow-mlruns/models/iris
```

**3. Crear y configurar la Cuenta de Servicio**

```sh
# Crear cuenta de servicio
gcloud iam service-accounts create k8s-mlflow-sa

# Agregar permisos de administrador de almacenamiento para el bucket
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:k8s-mlflow-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/storage.admin"
```

**4. Generar y descargar la llave JSON**

Genera una clave de acceso (JSON) para la cuenta de servicio y guárdala localmente, ya que se utilizará para que los pods se autentiquen:
1. Desde la [Consola de Google Cloud](https://console.cloud.google.com/), dirígete a **IAM y administración > Cuentas de servicio**.
2. Selecciona `k8s-mlflow-sa`, ve a la pestaña **Claves**, haz clic en **Agregar clave > Crear clave nueva**, elige **JSON** y descárgala.
3. Mueve y renombra el archivo descargado a la ruta local donde centralizas tus credenciales, por ejemplo, `/home/eric/.config/gcloud/k8s-mlflow-key.json`

### 3.8 Gestión de Secretos en Kubernetes

Tanto MLflow como Airflow necesitan acceso a credenciales para la base de datos y Google Cloud.

**1. Copiar el secreto de base de datos al namespace `airflow`**

La configuración de MLflow referencia las credenciales a través de un secreto. Como el cluster de PostgreSQL se encuentra en el namespace `database` y MLflow en `airflow`, debemos clonarlo:

```sh
kubectl create secret generic database-cluster-superuser \
  --from-literal=username=$(kubectl get secret database-cluster-superuser -n database -o jsonpath='{.data.username}' | base64 -d) \
  --from-literal=password=$(kubectl get secret database-cluster-superuser -n database -o jsonpath='{.data.password}' | base64 -d) \
  -n airflow
```

**2. Crear el secreto de GCP para el namespace de Airflow**

Esto permitirá a Airflow y MLflow autenticarse contra Google Cloud Storage:

```sh
kubectl create secret generic gcp-mlflow-key \
  --from-file=key.json=/home/eric/.config/gcloud/k8s-mlflow-key.json -n airflow
```
_(Nota: Más adelante se creará otro secreto con el formato específico para KServe en su propio namespace)._

### 3.9 Levantar servidor MLFlow

La configuración de MLFlow se encuentra en los archivos **[MLFlow Deployment](mlflow-deployment.yaml)** y **[MLFlow Service](mlflow-service.yaml)**.

El deployment referencia el secret `database-cluster-superuser` (generado automáticamente por CloudNativePG y copiado en el paso anterior) y el secreto `gcp-mlflow-key`.

**1. Montar el secreto de GCP en MLflow**

En tu archivo `mlflow-deployment.yaml`, en la especificación de contenedores (`spec.containers`), asegúrate de montar el secreto y definir la variable de entorno para GCP:

```yaml
      env:
        - name: GOOGLE_APPLICATION_CREDENTIALS
          value: /var/secrets/google/key.json
      volumeMounts:
        - name: gcp-key
          mountPath: /var/secrets/google
          readOnly: true
  volumes:
    - name: gcp-key
      secret:
        secretName: gcp-mlflow-key
```

**2. Aplicar archivos de configuración**

```sh
kubectl apply -f mlflow-deployment.yaml
kubectl apply -f mlflow-service.yaml

# Si es una actualización de secretos y MLflow ya estaba desplegado, reinícialo:
kubectl rollout restart deployment mlflow -n airflow
```

**3. Redireccionar el puerto a la interfaz de MLFlow**

```sh
# Se usa el puerto 30500 ya que el puerto 5000
# en windows está usado por otra aplicación (al menos en mi equipo)
kubectl port-forward svc/mlflow 30500:5000 -n airflow
```

### 3.10 Configurar `airflow-values.yaml`

Abre el archivo `airflow-values.yaml` descargado en el paso 3.5. Debes añadir la sección `gitSync`, configurar nuestra imagen Docker personalizada y montar las credenciales de Google Cloud (`gcp-mlflow-key`) para que Airflow interactúe con el almacenamiento de artefactos y no falle al subir sus propios logs/resultados.

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

# Configurable globalmente (si lo soporta) o por componente
env:
  - name: GOOGLE_APPLICATION_CREDENTIALS
    value: /var/secrets/google/key.json

# Logging
logs:
  persistence:
    enabled: true

# Configurar volúmenes para componentes (tmp y GCP key)
workers:
  extraVolumes:
    - name: tmp-files
      persistentVolumeClaim:
        claimName: airflow-tmp-files-pvc
    - name: gcp-key
      secret:
        secretName: gcp-mlflow-key
  extraVolumeMounts:
    - name: tmp-files
      mountPath: '{{ .Values.airflowHome }}/tmp'
    - name: gcp-key
      mountPath: '/var/secrets/google'
      readOnly: true

scheduler:
  extraVolumes:
    - name: tmp-files
      persistentVolumeClaim:
        claimName: airflow-tmp-files-pvc
    - name: gcp-key
      secret:
        secretName: gcp-mlflow-key
  extraVolumeMounts:
    - name: tmp-files
      mountPath: '{{ .Values.airflowHome }}/tmp'
    - name: gcp-key
      mountPath: '/var/secrets/google'
      readOnly: true

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

Con la configuración y secretos listos, procede a instalar Airflow aplicando tu archivo `airflow-values.yaml` personalizado:

```sh
helm install airflow apache-airflow/airflow --namespace airflow --create-namespace -f airflow-values.yaml
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

### 4.3 Instalar el Chart de Grafana

Grafana es una plataforma de observabilidad y visualización de métricas. A continuación se documenta su despliegue en Kubernetes mediante el Helm Chart oficial.

#### 4.3.1 Registrar el Repositorio Helm

Agrega el repositorio oficial de Grafana y actualiza el índice de paquetes local:

```sh
# Agregar el repositorio oficial de Grafana
helm repo add grafana-community https://grafana-community.github.io/helm-charts

# Actualizar el índice local de repositorios
helm repo update

# (Opcional) Verificar que el repositorio fue registrado correctamente
helm repo list

# (Opcional) Buscar el chart de Grafana disponible en el repositorio
helm search repo grafana-community/grafana
```

#### 4.3.2 Despliegue en Kubernetes

Crea el namespace dedicado e instala el chart de Grafana:

```sh
# Crear el namespace dedicado para Grafana
kubectl create namespace grafana-ns

# Instalar Grafana en el namespace
helm install grafana grafana-community/grafana \
  --namespace grafana-ns
```

#### 4.3.3 Validar el Despliegue

Verifica que todos los recursos hayan sido creados y que los pods estén en estado `Running`:

```sh
# Verificar el estado del release en Helm
helm list -n grafana-ns

# Ver el estado de todos los objetos en el namespace (pods, servicios, etc.)
kubectl get all -n grafana-ns
```

Si los pods no levantan de inmediato, espera unos segundos y vuelve a ejecutar el comando. La descarga de la imagen puede tardar la primera vez.

#### 4.3.4 Acceder a Grafana desde el Navegador

El Chart de Grafana no expone el servicio externamente por defecto. Para acceder desde el navegador local, utiliza `port-forward`.

**1. Consultar las instrucciones post-instalación del chart:**

```sh
helm get notes grafana -n grafana-ns

# Nombre DNS del servicio
# grafana.grafana-ns.svc.cluster.local
```

**2. Obtener la contraseña del usuario `admin`:**

La contraseña es generada automáticamente y almacenada en un Secret de Kubernetes:

```sh
kubectl get secret --namespace grafana-ns grafana \
  -o jsonpath="{.data.admin-password}" | base64 --decode; echo
```

**3. Redirigir el puerto y acceder desde el navegador:**

```sh
# Obtener el nombre del pod de Grafana
export POD_NAME=$(kubectl get pods \
  --namespace grafana-ns \
  -l "app.kubernetes.io/name=grafana,app.kubernetes.io/instance=grafana" \
  -o jsonpath="{.items[0].metadata.name}")

# Redirigir el puerto local 3100 al puerto 3000 del pod
kubectl --namespace grafana-ns port-forward $POD_NAME 3100:3000
```

_Accede desde el navegador a: [http://localhost:3100](http://localhost:3100) con el usuario `admin` y la contraseña obtenida en el paso anterior._

> **Referencia:** [Grafana on Helm Charts](https://grafana.com/docs/grafana/latest/setup-grafana/installation/helm/)

### 4.4 Instalar el Chart de Prometheus (kube-prometheus-stack)

Como parte de la estrategia de monitoreo y observabilidad, configuraremos Prometheus como origen de métricas para Grafana. Se instalará en el namespace `grafana-ns` para facilitar su integración conjunta con Grafana.

**1. Añadir el Repositorio de Prometheus**

```sh
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
```

**2. Instalación Base**

Como el namespace `grafana-ns` ya fue creado en la sección anterior (4.3.2), puedes proceder directamente con la instalación:

```sh
helm install prometheus prometheus-community/kube-prometheus-stack -n grafana-ns
```

**3. Extraer y Actualizar Configuración (`prometheus-values.yaml`)**

Para asegurar un correcto funcionamiento en un entorno de desarrollo local y evitar errores con `node-exporter` (como caídas o reinicios inesperados al intentar montar volúmenes raíz del host), se deben modificar sus valores base.

Primero, extrae los valores por defecto:

```sh
helm show values prometheus-community/kube-prometheus-stack > prometheus-values.yaml
```

A continuación, abre el archivo generado `prometheus-values.yaml`, busca el bloque de configuración `prometheus-node-exporter` y asegúrate de configurar el parámetro `hostRootFsMount` de la siguiente manera:

```yaml
prometheus-node-exporter:
  hostRootFsMount:
    enabled: false
```

También agrega la siguiente configuración de scraping para capturar las métricas de Airflow

```sh
additionalScrapeConfigs:
  - job_name: airflow-statsd
    scrape_interval: 10s
    metrics_path: /metrics
    static_configs:
      - targets:
          - airflow-statsd.airflow.svc.cluster.local:9102
```

Luego en grafana genera un nuevo dashboard con la configuración descrita en el archivo **[grafana-airflow-dashboard.json](grafana-airflow-dashboard.json)** _(Nota: el dashboard no pretende ser productivo, sino demostrativo del funcionamiento)_

**4. Aplicar los Nuevos Valores**

Aplica la actualización utilizando el nuevo archivo de configuración en el clúster:

```sh
helm upgrade prometheus prometheus-community/kube-prometheus-stack -n grafana-ns -f prometheus-values.yaml
```

**5. Configurar Datasource y Dashboard en Grafana**

Accede a la interfaz de Grafana (ver sección 4.3.4) y realiza lo siguiente para enlazar Prometheus con Grafana:

1. **Crear Datasource de Prometheus:** Dirígete a la configuración de conexiones (_Data sources_), añade uno nuevo de tipo Prometheus e ingresa la siguiente URL interna del clúster:
   `http://prometheus-kube-prometheus-prometheus.grafana-ns.svc.cluster.local:9090`
2. **Importar Dashboard:** Dirígete al panel de Dashboards, entra en la opción **Import** y arrastra o selecciona el archivo local `grafana-kubernetes-dashboard.json`.

> **Referencia**<br>
> https://medium.com/@gayatripawar401/deploy-prometheus-and-grafana-on-kubernetes-using-helm-5aa9d4fbae66<br>
> https://github.com/prometheus-community/helm-charts/pkgs/container/charts%2Fkube-prometheus-stack

### 4.5 Despliegue de Modelos con KServe

KServe es el componente responsable del despliegue en Kubernetes (model serving) de los modelos entrenados por MLflow.

#### 4.5.1 Instalación de Dependencias (Cert Manager y Gateway API)

KServe requiere Cert Manager para el manejo de certificados webhooks y el Gateway API como controlador de red.

```sh
# Crear namespace de destino
kubectl create namespace mlflow-kserve

# Instalar Cert Manager
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.20.0/cert-manager.yaml

# Instalar controlador de red (Gateway API)
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml
```

#### 4.5.2 Instalar KServe vía Helm

Una vez instaladas las dependencias, despliega KServe.

```sh
# Instalar KServe CRDs
helm install kserve-crd oci://ghcr.io/kserve/charts/kserve-crd --version v0.17.0

# Instalar recursos de KServe
helm install kserve-resources oci://ghcr.io/kserve/charts/kserve-resources --version v0.17.0 \
  --namespace mlflow-kserve \
  --set kserve.controller.deploymentMode=Standard \
  --set kserve.controller.gateway.ingressGateway.enableGatewayApi=true \
  --set kserve.controller.gateway.ingressGateway.kserveGateway=kserve/kserve-ingress-gateway
```

#### 4.5.3 Configuración de Credenciales GCP en KServe

Para que los contenedores de inferencia puedan descargar el modelo alojado en el bucket GCS:

```sh
# Crear el secreto con el formato y nombre de archivo que espera KServe
kubectl create secret generic gcp-mlflow-key \
  --from-file=gcloud-application-credentials.json=/home/eric/.config/gcloud/k8s-mlflow-key.json \
  -n mlflow-kserve

# Vincular el secreto a la Service Account principal en el namespace
kubectl patch serviceaccount default \
  -n mlflow-kserve \
  -p '{"secrets": [{"name": "gcp-mlflow-key"}]}'
```

#### 4.5.4 Levantar el Deployment de Inferencia (Inferenceservice)

Aplica tus manifiestos para levantar el API con el modelo Iris entrenado.

```sh
kubectl apply -f kserve-csr.yaml
kubectl apply -f kserve-gateway.yaml
kubectl apply -f kserve-service.yaml

# Obtener estado del servicio (su primera inicialización puede tardar unos minutos)
kubectl get inferenceservice mlflow-iris-classifier -oyaml -n mlflow-kserve
```

## 5. Mantenimiento y Actualizaciones

### Actualizar el Chart

Si posteriormente realizas cambios en tu archivo `airflow-values.yaml` (ej. cambiar configuraciones, habilitar nuevos servicios), aplica los cambios con el comando de actualización:

```sh
helm upgrade airflow apache-airflow/airflow -n airflow -f airflow-values.yaml --timeout 15m --wait
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
- https://docs.greatexpectations.io/docs/core/connect_to_data/dataframes/
- https://grafana.com/docs/grafana/latest/setup-grafana/installation/helm/
- https://mlflow.org/docs/latest/ml/deployment/deploy-model-to-kubernetes/tutorial/
- https://kserve.github.io/website/docs/admin-guide/kubernetes-deployment
- https://cert-manager.io/docs/installation/
- https://mlflow.org/docs/latest/self-hosting/architecture/artifact-store/#google-cloud-storage

---

## 🚀 Referencia Rápida: Comandos de Port-Forward

Lista consolidada de todos los comandos necesarios para redirigir los puertos y acceder a las interfaces web de las aplicaciones desplegadas:

```sh
# Apache Airflow (UI / API Server)
# Acceso: http://localhost:8080
kubectl port-forward svc/airflow-api-server 8080:8080 --namespace airflow &

# MLflow (Tracking Server)
# Acceso: http://localhost:30500
# Nota: se usa el puerto 30500 porque el 5000 puede estar ocupado en Windows
kubectl port-forward svc/mlflow 30500:5000 -n airflow &

# Grafana (Dashboard de Métricas)
# Acceso: http://localhost:3100
# Nota: exportar la variable con el nombre del pod antes de ejecutar el port-forward
export POD_NAME=$(kubectl get pods \
  --namespace grafana-ns \
  -l "app.kubernetes.io/name=grafana,app.kubernetes.io/instance=grafana" \
  -o jsonpath="{.items[0].metadata.name}")
kubectl --namespace grafana-ns port-forward $POD_NAME 3100:3000 &
```

Comandos para detener los procesos

```sh
kill -9 $(lsof -t -i:8080)
kill -9 $(lsof -t -i:30500)
kill -9 $(lsof -t -i:3100)
```

---

> **Nota:** Los archivos de documentación correspondiente fueron generados o asistidos mediante el uso de inteligencia artificial.
