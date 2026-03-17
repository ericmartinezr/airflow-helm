FROM apache/airflow:3.1.8

RUN pip install --no-cache-dir \
    "apache-airflow[google]==3.1.8" \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.1.8/constraints-3.12.txt"

RUN pip install  --no-cache-dir \
    scikit-learn==1.8.0 \
    #pandas==3.0.1 \
    "mlflow[mlserver]==3.10.1" \
    great_expectations==1.14.0 \
    google-cloud-storage==3.9.0