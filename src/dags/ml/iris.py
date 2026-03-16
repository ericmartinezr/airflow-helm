import logging
import pendulum
from datetime import timedelta
from airflow.sdk import dag, task, Variable
from airflow.exceptions import AirflowSkipException, AirflowFailException
from airflow.providers.standard.hooks.filesystem import FSHook
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

    @task()
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

    @task()
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
    def train_model():
        """
        Entrenamiento del modelo con MLFlow.

        Guarda los splits de entrenamiento y prueba como artefactos Parquet
        en MLFlow (bajo splits/) para que las tareas posteriores los lean
        directamente sin volver a calcular el split.
        Usa log_input únicamente para trazabilidad (lineage) en la UI de MLFlow.
        """
        import tempfile
        import os
        import pandas as pd
        import mlflow
        from mlflow import sklearn
        from mlflow.tracking import MlflowClient
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import train_test_split

        try:
            mlflow_tracking_url = Variable.get("MLFlow_Tracking_URL", None)
            if not mlflow_tracking_url:
                raise ValueError(
                    "Debes configurar la URL de tracking de MLFlow"
                )

            mlflow.set_tracking_uri(mlflow_tracking_url)
            sklearn.autolog(
                log_model_signatures=True, log_input_examples=True)

            context = get_current_context()
            ds = context["logical_date"].format("YYYYMMDD")
            dag_run_id = context["run_id"]
            features_file_name = context["ti"].xcom_pull(
                task_ids="feature_engineering"
            )
            df = pd.read_csv(features_file_name)

            X = df.drop("target", axis=1)
            y = df["target"]

            # El split ocurre UNA sola vez aquí.
            # Los artefactos resultantes son consumidos por evaluate_model
            # y register_model via mlflow.artifacts.download_artifacts().
            TEST_SIZE = 0.25
            RANDOM_STATE = 42
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=TEST_SIZE, random_state=RANDOM_STATE
            )

            # Datasets para trazabilidad (lineage) en la UI de MLFlow.
            # log_input solo guarda metadatos (esquema, digest, fuente);
            # NO almacena los datos reales del split.
            train_data = pd.concat([X_train, y_train], axis=1)
            test_data = pd.concat([X_test, y_test], axis=1)
            train_dataset = mlflow.data.from_pandas(
                train_data,
                source=features_file_name,
                name="iris-entrenamiento",
                targets="target"
            )
            test_dataset = mlflow.data.from_pandas(
                test_data,
                source=features_file_name,
                name="iris-prueba",
                targets="target"
            )

            # Configura el experimento con descripción y etiquetas via MlflowClient
            experiment = mlflow.set_experiment("airflow-ml-iris")
            client = MlflowClient()
            client.set_experiment_tag(
                experiment_id=experiment.experiment_id,
                key="descripcion",
                value="Experimento de clasificación de flores Iris usando RandomForestClassifier. "
                      "Pipeline orquestado con Apache Airflow."
            )

            with mlflow.start_run(
                experiment_id=experiment.experiment_id,
                run_name=f"iris-RandomForestClassifier-{ds}",
                description=(
                    "Entrenamiento de RandomForestClassifier sobre el dataset Iris. "
                    "Incluye ingeniería de características (sepal_ratio). "
                    f"Fecha lógica del pipeline: {ds}. "
                    f"Run de Airflow: {dag_run_id}."
                )
            ) as active_run:
                logger.info(f"Run activo: {active_run.info.run_id}")

                # Hiperparámetros del modelo y del pipeline de datos
                model = RandomForestClassifier(random_state=RANDOM_STATE)
                mlflow.log_params({
                    "algoritmo": "RandomForestClassifier",
                    "n_estimators": model.n_estimators,
                    "max_depth": str(model.max_depth),
                    "random_state": RANDOM_STATE,
                    "test_size": TEST_SIZE,
                    "train_size": 1 - TEST_SIZE,
                    "fuente_datos": features_file_name,
                })

                model.fit(X_train, y_train)
                sklearn.log_model(model, name="model")

                # Lineage: registra qué datos se usaron para entrenar y testear
                mlflow.log_input(train_dataset, context="entrenamiento",
                                 tags={"descripcion": "Set de entrenamiento (75%)",
                                       "formato": "parquet",
                                       "n_muestras": str(len(X_train))})
                mlflow.log_input(test_dataset, context="prueba",
                                 tags={"descripcion": "Set de prueba (25%)",
                                       "formato": "parquet",
                                       "n_muestras": str(len(X_test))})

                # Almacena los splits reales como artefactos Parquet en MLFlow.
                # Las tareas evaluate_model y register_model los descargarán
                # con mlflow.artifacts.download_artifacts() sin re-calcular nada.
                with tempfile.TemporaryDirectory() as tmp_dir:
                    splits = {
                        "X_train": X_train,
                        "X_test": X_test,
                        "y_train": y_train.to_frame(),
                        "y_test": y_test.to_frame(),
                    }
                    for name, data in splits.items():
                        local_path = os.path.join(tmp_dir, f"{name}.parquet")
                        data.to_parquet(local_path, index=True)
                        mlflow.log_artifact(local_path, artifact_path="splits")
                        logger.info(
                            f"Split '{name}' guardado como artefacto MLFlow")

                # Solo retornamos el run_id (string) para que XCom pueda serializarlo
                return active_run.info.run_id

        except Exception as e:
            logger.error("Error entrenando el modelo")
            logger.error(e, exc_info=True)
            raise AirflowSkipException

    @task()
    def evaluate_model(run_id: str):
        """
        Evalúa el modelo.

        Descarga los splits de prueba desde los artefactos MLFlow generados
        por train_model. No vuelve a leer el CSV ni a recalcular el split.
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
        import mlflow.artifacts
        from mlflow import sklearn
        from mlflow.tracking import MlflowClient
        from sklearn.metrics import (
            accuracy_score,
            precision_score,
            recall_score,
            f1_score,
            classification_report,
            confusion_matrix,
        )

        try:
            mlflow_tracking_url = Variable.get("MLFlow_Tracking_URL", None)
            if not mlflow_tracking_url:
                raise ValueError(
                    "Debes configurar la URL de tracking de MLFlow"
                )

            mlflow.set_tracking_uri(mlflow_tracking_url)

            # Descarga X_test e y_test desde los artefactos del run de entrenamiento
            X_test_path = mlflow.artifacts.download_artifacts(
                run_id=run_id, artifact_path="splits/X_test.parquet"
            )
            y_test_path = mlflow.artifacts.download_artifacts(
                run_id=run_id, artifact_path="splits/y_test.parquet"
            )
            X_test = pd.read_parquet(X_test_path)
            y_test = pd.read_parquet(y_test_path).squeeze()

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

    @task()
    def register_model(run_id: str):
        """
        Registra el modelo en MLFlow.

        Descarga X_test desde los artefactos MLFlow generados por train_model
        para inferir la firma del modelo. No vuelve a leer el CSV ni a
        recalcular el split.
        Asigna descripción y etiquetas al modelo registrado y a la versión.
        """
        import pandas as pd
        import mlflow
        import mlflow.artifacts
        from mlflow import sklearn
        from mlflow.models import infer_signature
        from mlflow.tracking import MlflowClient

        NOMBRE_MODELO = "iris-random-forest-clasif-model"

        try:
            mlflow_tracking_url = Variable.get("MLFlow_Tracking_URL", None)
            if not mlflow_tracking_url:
                raise ValueError(
                    "Debes configurar la URL de tracking de MLFlow"
                )

            mlflow.set_tracking_uri(mlflow_tracking_url)

            # Descarga X_test desde los artefactos del run de entrenamiento
            X_test_path = mlflow.artifacts.download_artifacts(
                run_id=run_id, artifact_path="splits/X_test.parquet"
            )
            X_test = pd.read_parquet(X_test_path)

            model = sklearn.load_model(f"runs:/{run_id}/model")
            y_pred = model.predict(X_test)
            signature = infer_signature(X_test, y_pred)

            model_info = sklearn.log_model(
                sk_model=model,
                name="iris-model",
                signature=signature,
                input_example=X_test.iloc[:5],
                registered_model_name=NOMBRE_MODELO,
            )

            client = MlflowClient()

            # Descripción y etiquetas del modelo registrado (aplica a todas las versiones)
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
                client.set_registered_model_tag(NOMBRE_MODELO, clave, valor)

            # Descripción y etiquetas de esta versión específica
            version = model_info.registered_model_version
            client.update_model_version(
                name=NOMBRE_MODELO,
                version=version,
                description=(
                    f"Versión entrenada en el run MLFlow: {run_id}. "
                    "Evaluada y aprobada con exactitud ≥ 0.8 sobre el set de prueba. "
                    "Splits reproducibles almacenados como artefactos Parquet."
                )
            )
            client.set_model_version_tag(
                NOMBRE_MODELO, version, "run_id", run_id)

            # Marca el run como completado
            client.set_tag(run_id, "etapa", "registro")
            client.set_tag(run_id, "modelo.version_registrada", str(version))
            client.set_tag(run_id, "modelo.nombre_registrado", NOMBRE_MODELO)

            logger.info(
                f"Modelo '{NOMBRE_MODELO}' v{version} registrado "
                f"exitosamente (run_id={run_id})"
            )

            return run_id

        except Exception as e:
            logger.error("Error registrando el modelo")
            logger.error(e, exc_info=True)
            raise AirflowSkipException

    @task()
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

    # @task.bash(
    #    env={
    #        "MLFLOW_TRACKING_URI": "{{var.value.MLFlow_Tracking_URL}}"
    #    }
    # )
    # def generate_dockerfile(run_id: str):
    #    try:
    #        import mlflow
#
    #        mlflow.pyfunc
    #        return f"mlflow models generate-dockerfile -m runs:/{run_id}/model -d ./my_output_dir"
    #    except Exception as e:
    #        logger.error("Error generando el archivo docker")
    #        logger.error(e, exc_info=True)
    #        raise AirflowFailException
#
    # deploy_model = KubernetesPodOperator(
    #    task_id="deploy_model",
    #    name="buildah-build",
    #    image="quay.io/buildah/stable:v1.40",  # 2026 stable version
    #    cmds=["buildah", "bud"],
    #    arguments=[
    #        "--storage-driver=vfs",
    #        "-t", "my-registry.com/iris-model:latest",
    #        "./my_output_dir"
    #    ],
    #    # Buildah needs minimal extra caps to run rootless
    #    container_security_context={
    #        "capabilities": {"add": ["SETGID", "SETUID"]}
    #    },
    #    namespace="airflow",
    # )

    # @task()
    # def deploy_model(run_id: str):
    #    try:
    #        import mlflow
#
    #        mlflow_tracking_url = Variable.get("MLFlow_Tracking_URL", None)
    #        if not mlflow_tracking_url:
    #            raise ValueError(
    #                "Debes configurar la URL de tracking de MLFlow"
    #            )
#
    #        mlflow.set_tracking_uri(mlflow_tracking_url)
    #        mlflow.models.build_docker(
    #            model_uri=f"runs:/{run_id}/model",
    #            name="iris-model-img",
    #            enable_mlserver=True
    #        )
    #    except Exception as e:
    #        logger.error("Error desplegando el modelo")
    #        logger.error(e, exc_info=True)
    #        raise AirflowSkipException

    _extract_data = extract_data()
    _validate_data = validate_data(file_name=_extract_data)
    _feature_engineering = feature_engineering()
    _train_model = train_model()
    _evaluate_model = evaluate_model(run_id=_train_model)
    _register_model = register_model(run_id=_evaluate_model)
    _test_model = test_model(run_id=_register_model)
    # _generate_dockerfile = generate_dockerfile(run_id=_test_model)

    (
        _validate_data >>
        _feature_engineering >>
        _train_model >>
        _evaluate_model >>
        _register_model >>
        _test_model
        # >>
        # _generate_dockerfile >>
        # deploy_model
    )


iris()
