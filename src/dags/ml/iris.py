import logging
import pendulum
from datetime import timedelta
from airflow.sdk import dag, task, Variable
from airflow.exceptions import AirflowSkipException, AirflowFailException
from airflow.providers.standard.hooks.filesystem import FSHook
from airflow.providers.google.cloud.transfers.gcs_to_gcs import GCSToGCSOperator
from airflow.sdk import get_current_context


logger = logging.getLogger(__name__)


def configure_expectations():
    import great_expectations as gx

    suite = gx.ExpectationSuite(name="iris-gx-suite")
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToNotBeNull(
            column="sepal length (cm)")
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToNotBeNull(
            column="sepal length (cm)")
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToNotBeNull(
            column="petal length (cm)")
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToNotBeNull(
            column="petal width (cm)")
    )

    return suite


@dag(
    "iris",
    description="Pipeline para el entrenamiento de modelo ML (iris)",
    schedule="@daily",
    start_date=pendulum.datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["ml"],
    default_args={
        "depends_on_past": False,
        "pool": "ml_pool",
        "retries": 1,
        "retry_delay": timedelta(seconds=15),
    }
)
def iris():

    @task(max_active_tis_per_dag=1)
    def extract_data():
        """
        Extrae datos con los que se entrenará al modelo
        """
        from sklearn.datasets import load_iris

        try:
            context = get_current_context()
            ds = context["logical_date"].format("YYYYMMDD")
            hook = FSHook(fs_conn_id="temp_files")
            file_name = f"{hook.get_path()}/iris_{ds}.csv"

            data = load_iris(as_frame=True)
            df = data.frame
            df.to_csv(file_name, index=False)

            logger.info(f"Archivo {file_name} creado correctamente.")

            return file_name
        except Exception as e:
            logger.error("Error extracting training data")
            logger.error(e, exc_info=True)
            raise AirflowSkipException

    @task.short_circuit()
    def validate_data(file_name: str):
        import pandas as pd
        import great_expectations as gx

        df = pd.read_csv(file_name)
        context = gx.get_context(mode="ephemeral")
        suite = context.suites.add(configure_expectations())
        datasource = context.data_sources.add_pandas(name="pandas_source")
        data_asset = datasource.add_dataframe_asset(
            name="iris_df_data_asset"
        )
        batch_definition = data_asset.add_batch_definition_whole_dataframe(
            "Iris Definition"
        )
        batch = batch_definition.get_batch({"dataframe": df})
        expectation = batch.validate(suite)

        logger.info(f"Expectation result: \n{expectation}")

        return expectation.get("success")

    @task(max_active_tis_per_dag=1)
    def feature_engineering():
        """
        Ingeniería de características
        """
        import pandas as pd

        try:
            context = get_current_context()
            ds = context["logical_date"].format("YYYYMMDD")
            hook = FSHook(fs_conn_id="temp_files")
            file_name = context["ti"].xcom_pull(task_ids="extract_data")
            df = pd.read_csv(file_name)

            # Agrega una nueva feature
            features_file_name = f"{hook.get_path()}/features_{ds}.csv"
            df["sepal_ratio"] = (
                df["sepal length (cm)"] / df["sepal width (cm)"]
            )
            df.to_csv(features_file_name, index=False)
            return features_file_name
        except Exception as e:
            logger.error("Error engineering features")
            logger.error(e, exc_info=True)
            raise AirflowSkipException

    @task(max_active_tis_per_dag=1)
    def train_register_model():
        """
        Entrena el modelo, lo registra en MLFlow y en el Model Registry.

        - Realiza el split train/test una única vez.
        - Loggea el modelo con firma inferida y lo registra con nombre en el Model Registry.
        - Asigna descripción y etiquetas al modelo registrado y a la versión.
        """
        import pandas as pd
        import mlflow
        from mlflow import sklearn
        from mlflow.models import infer_signature
        from mlflow.tracking import MlflowClient
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import train_test_split

        NOMBRE_MODELO = "iris-random-forest-clasif-model"
        TEST_SIZE = 0.25
        RANDOM_STATE = 42

        try:
            mlflow_tracking_url = Variable.get("MLFlow_Tracking_URL", None)
            if not mlflow_tracking_url:
                raise ValueError(
                    "Debes configurar la URL de tracking de MLFlow"
                )

            experiment_name = Variable.get("MLFlow_Experiment_Iris", None)
            if not experiment_name:
                raise ValueError(
                    "Debes configurar el nombre del experimento de MLFlow"
                )

            mlflow.set_tracking_uri(mlflow_tracking_url)
            sklearn.autolog(log_model_signatures=True, log_input_examples=True)

            context = get_current_context()
            ds = context["logical_date"].format("YYYYMMDD")
            dag_run_id = context["run_id"]
            features_file_name = context["ti"].xcom_pull(
                task_ids="feature_engineering"
            )
            df = pd.read_csv(features_file_name)

            X = df.drop("target", axis=1)
            y = df["target"]
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=TEST_SIZE, random_state=RANDOM_STATE
            )

            experiment = mlflow.set_experiment(experiment_name)
            client = MlflowClient()

            with mlflow.start_run(
                experiment_id=experiment.experiment_id,
                run_name=f"iris-RandomForestClassifier-{ds}",
                description=(
                    f"Entrenamiento de RandomForestClassifier sobre el dataset Iris. "
                    f"Fecha lógica: {ds}. Run de Airflow: {dag_run_id}."
                )
            ) as active_run:
                run_id = active_run.info.run_id
                logger.info(f"Run activo: {run_id}")

                model = RandomForestClassifier(random_state=RANDOM_STATE)
                model.fit(X_train, y_train)

                y_pred = model.predict(X_test)
                signature = infer_signature(X_test, y_pred)

                model_info = sklearn.log_model(
                    sk_model=model,
                    name="iris-model",
                    signature=signature,
                    input_example=X_test.iloc[:5],
                    registered_model_name=NOMBRE_MODELO,
                )

                version = model_info.registered_model_version

                # Descripción y etiquetas del modelo registrado
                client.update_registered_model(
                    name=NOMBRE_MODELO,
                    description=(
                        "Modelo de clasificación multiclase para el dataset Iris. "
                        "Clasifica flores en tres especies: setosa, versicolor y virginica. "
                        "Entrenado con RandomForestClassifier de scikit-learn. "
                        "Pipeline orquestado con Apache Airflow."
                    )
                )
                for clave, valor in {
                    "algoritmo": "RandomForestClassifier",
                    "dataset": "iris",
                    "pipeline": "airflow-ml-iris",
                }.items():
                    client.set_registered_model_tag(
                        NOMBRE_MODELO, clave, valor)

                # Descripción y etiquetas de esta versión
                client.update_model_version(
                    name=NOMBRE_MODELO,
                    version=version,
                    description=(
                        f"Versión entrenada en el run MLFlow: {run_id}. "
                        "Evaluada y aprobada con exactitud ≥ 0.8 sobre el set de prueba."
                    )
                )
                client.set_model_version_tag(
                    NOMBRE_MODELO, version, "run_id", run_id)
                client.set_tag(
                    run_id, "modelo.version_registrada", str(version))
                client.set_tag(
                    run_id, "modelo.nombre_registrado", NOMBRE_MODELO)

                logger.info(
                    f"Modelo '{NOMBRE_MODELO}' v{version} registrado "
                    f"exitosamente (run_id={run_id})"
                )

                logger.info(
                    f"Id del modelo registrado '{model_info.model_id}'"
                )

                return run_id

        except Exception as e:
            logger.error("Error en train_register_model")
            logger.error(e, exc_info=True)
            raise AirflowSkipException

    @task(max_active_tis_per_dag=1)
    def evaluate_model(run_id: str):
        """
        Evalúa el modelo.

        Recalcula el split de prueba leyendo el CSV de features (mismo
        RANDOM_STATE y TEST_SIZE que train_register_model) para obtener
        X_test e y_test de forma determinista sin artefactos intermedios.
        Registra métricas completas de evaluación:
          - Exactitud global
          - Precisión, recall y F1 por clase
          - Matriz de confusión como artefacto JSON
        """
        import json
        import tempfile
        import os
        import pandas as pd
        import mlflow
        from mlflow import sklearn
        from mlflow.tracking import MlflowClient
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import (
            accuracy_score,
            precision_score,
            recall_score,
            f1_score,
            classification_report,
            confusion_matrix,
        )

        TEST_SIZE = 0.25
        RANDOM_STATE = 42

        try:
            mlflow_tracking_url = Variable.get("MLFlow_Tracking_URL", None)
            if not mlflow_tracking_url:
                raise ValueError(
                    "Debes configurar la URL de tracking de MLFlow"
                )

            mlflow.set_tracking_uri(mlflow_tracking_url)

            # Recalcula el split con la misma semilla que train_register_model
            features_file_name = get_current_context()["ti"].xcom_pull(
                task_ids="feature_engineering"
            )
            df = pd.read_csv(features_file_name)
            X = df.drop("target", axis=1)
            y = df["target"]
            _, X_test, _, y_test = train_test_split(
                X, y, test_size=TEST_SIZE, random_state=RANDOM_STATE
            )

            model = sklearn.load_model(f"runs:/{run_id}/model")
            y_pred = model.predict(X_test)
            clases = sorted(y_test.unique().tolist())

            # Cálculo de métricas
            acc = accuracy_score(y_test, y_pred)
            precision_macro = precision_score(y_test, y_pred, average="macro")
            recall_macro = recall_score(y_test, y_pred, average="macro")
            f1_macro = f1_score(y_test, y_pred, average="macro")
            matriz = confusion_matrix(y_test, y_pred).tolist()
            reporte = classification_report(
                y_test, y_pred,
                target_names=[f"clase_{c}" for c in clases]
            )

            logger.info(f"Exactitud: {acc:.4f}")
            logger.info(f"F1 macro: {f1_macro:.4f}")
            logger.info(f"Reporte:\n{reporte}")

            if acc < 0.8:
                raise ValueError(
                    f"Rendimiento del modelo por debajo del umbral "
                    f"(exactitud={acc:.4f} < 0.8)"
                )

            # Registra métricas y artefactos en el run existente
            client = MlflowClient()
            client.set_tag(run_id, "etapa", "evaluacion")
            client.set_tag(run_id, "evaluacion.resultado", "aprobado")
            client.set_tag(
                run_id, "evaluacion.umbral_exactitud", "0.8")

            client.log_metric(run_id, "exactitud", acc)
            client.log_metric(run_id, "precision_macro", precision_macro)
            client.log_metric(run_id, "recall_macro", recall_macro)
            client.log_metric(run_id, "f1_macro", f1_macro)

            # Métricas por clase
            prec_por_clase = precision_score(
                y_test, y_pred, average=None, labels=clases)
            rec_por_clase = recall_score(
                y_test, y_pred, average=None, labels=clases)
            f1_por_clase = f1_score(
                y_test, y_pred, average=None, labels=clases)
            for i, clase in enumerate(clases):
                client.log_metric(run_id, f"precision_clase_{clase}",
                                  prec_por_clase[i])
                client.log_metric(run_id, f"recall_clase_{clase}",
                                  rec_por_clase[i])
                client.log_metric(run_id, f"f1_clase_{clase}",
                                  f1_por_clase[i])

            # Guarda el reporte de clasificación y la matriz de confusión
            with tempfile.TemporaryDirectory() as tmp_dir:
                reporte_path = os.path.join(
                    tmp_dir, "reporte_clasificacion.txt")
                with open(reporte_path, "w", encoding="utf-8") as f:
                    f.write(reporte)
                client.log_artifact(run_id, reporte_path,
                                    artifact_path="evaluacion")

                matriz_path = os.path.join(tmp_dir, "matriz_confusion.json")
                with open(matriz_path, "w", encoding="utf-8") as f:
                    json.dump({"clases": clases,
                               "matriz": matriz}, f, indent=2)
                client.log_artifact(run_id, matriz_path,
                                    artifact_path="evaluacion")

            return run_id

        except Exception as e:
            logger.error("Error evaluando el modelo")
            logger.error(e, exc_info=True)
            raise AirflowSkipException

    @task(max_active_tis_per_dag=1)
    def test_model(run_id: str):
        try:
            import mlflow
            import pandas as pd
            from mlflow import sklearn

            mlflow_tracking_url = Variable.get("MLFlow_Tracking_URL", None)
            if not mlflow_tracking_url:
                raise ValueError(
                    "Debes configurar la URL de tracking de MLFlow"
                )

            mlflow.set_tracking_uri(mlflow_tracking_url)
            model = sklearn.load_model(f"runs:/{run_id}/model")
            df = pd.DataFrame(
                [
                    [5.1, 3.5, 1.4, 0.2, 0.8],
                    [6.2, 3.4, 5.4, 2.3, 1.0],
                    [5.9, 3.0, 4.2, 1.5, 0.2]
                ],
                columns=[
                    "sepal length (cm)",
                    "sepal width (cm)",
                    "petal length (cm)",
                    "petal width (cm)",
                    "sepal_ratio"
                ]
            )
            pred = model.predict(df)
            print(pred)

            return run_id
        except Exception as e:
            logger.error("Error probando el modelo")
            logger.error(e, exc_info=True)
            raise AirflowFailException

    copy_model = GCSToGCSOperator(
        task_id="copy_model",
        source_bucket="k8s-mlflow-mlruns",
        source_object="models/iris/{{ ti.xcom_pull(task_ids='test_model') }}/models/model/*",
        destination_bucket="mlflow-serving",
        destination_object="iris/latest/",
        move_object=False,
    )

    _extract_data = extract_data()
    _validate_data = validate_data(file_name=_extract_data)
    _feature_engineering = feature_engineering()
    _train_register_model = train_register_model()
    _evaluate_model = evaluate_model(run_id=_train_register_model)
    _test_model = test_model(run_id=_evaluate_model)

    (
        _validate_data >>
        _feature_engineering >>
        _train_register_model >>
        _evaluate_model >>
        _test_model >>
        copy_model
    )


iris()
