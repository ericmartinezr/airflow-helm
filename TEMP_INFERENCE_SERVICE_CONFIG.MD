gs://k8s-mlflow-mlruns

---

pip install google-cloud-storage

--

https://mlflow.org/docs/latest/ml/deployment/deploy-model-to-kubernetes/tutorial/

---

https://mlflow.org/docs/latest/self-hosting/architecture/artifact-store/#google-cloud-storage

---

https://mlflow.org/docs/latest/self-hosting/architecture/tracking-server/#tracking-server-artifact-store

---

https://cert-manager.io/docs/installation/

--

https://kserve.github.io/website/docs/admin-guide/kubernetes-deployment

---

PROJECT_ID=k8s-mlflow

gcloud auth application-default set-quota-project $PROJECT_ID
gcloud config set project $PROJECT_ID

gcloud auth login

# Crear bucket

gcloud storage buckets create gs://k8s-mlflow-mlruns

# Crear cuenta de servicio

gcloud iam service-accounts create k8s-mlflow-sa

# Agregar permisos para el bucket

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:mlflow-artifact-sa@$PROJECT_ID.iam.gserviceaccount.com" \
 --role="roles/storage.admin"

# Crear credenciales

# Paso manual ya que lo usaré luego en GOOGLE_CLOUD_CREDENTIALS

# 1. Crear llave (JSON) en la cuenta de servicio anterior

# 2. Descargar y copiar en ruta /home/<user>/.config/cloud

--

# Crear namespace

kubectl create namespace mlflow-kserve

# Instalar Cert Manager

kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.20.0/cert-manager.yaml

# Instalar controlador de red

kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml

# Instalar KServe CRDs

helm install kserve-crd oci://ghcr.io/kserve/charts/kserve-crd --version v0.17.0

# Instalar recursos de KServe

helm install kserve-resources oci://ghcr.io/kserve/charts/kserve-resources --version v0.17.0 \
 --namespace mlflow-kserve \
 --set kserve.controller.deploymentMode=Standard \
 --set kserve.controller.gateway.ingressGateway.enableGatewayApi=true \
 --set kserve.controller.gateway.ingressGateway.kserveGateway=kserve/kserve-ingress-gateway

# Aplicar el servicio

kubectl apply -f kserve-service.yaml

---

# Obtener estado del servicio

kubectl get inferenceservice mlflow-iris-classifier -n mlflow-kserve
kubectl get inferenceservice mlflow-iris-classifier -oyaml -n mlflow-kserve

---

# Configurar GCP en servidor y cliente

https://www.youtube.com/watch?v=MWfKAgEHsHo

---

Give me the full step by step, very clearly and without verbosity, how to implement GCP as an artifact root (only the artifact, the backend is in my local postgresql) to save the artifacts with MLFlow so I can later deploy from GS the inference service with KServe pointing to that artifact folder
