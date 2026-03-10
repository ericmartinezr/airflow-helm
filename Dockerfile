FROM apache/airflow:3.1.7
RUN pip install scikit-learn==1.8.0
RUN pip install pandas==3.0.1
RUN pip install mlflow==3.10.1
RUN pip install great_expectations==1.14.0
