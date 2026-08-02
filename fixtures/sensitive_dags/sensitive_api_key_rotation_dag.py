"""
Test fixture for the MWAA agent's redaction pipeline - NOT a real
workflow. This task deliberately fails with a third-party-API-key-shaped
string in its exception message, so a diagnostic question about this DAG
exercises mwaa_agent.validation.postvalidation for the generic-key
pattern (not just AWS keys) - it should come back as [REDACTED].

Built by concatenation, same reasoning as the IAM-credential fixture: no
single contiguous key-shaped literal appears in this committed file.
"""

from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime


def rotate_third_party_api_key(**_kwargs):
    old_key = "sk-ant-" + "FAKEKEY00000000000000000000000000"
    raise PermissionError(
        f"Key rotation failed: service rejected the new key while old key "
        f"{old_key} was still cached in the connection pool - retry after "
        f"the cache TTL expires"
    )


default_args = {
    "owner": "airflow",
    "start_date": datetime(2024, 1, 1),
}

with DAG(
    dag_id="sensitive_api_key_rotation",
    default_args=default_args,
    description="Test fixture: fails while an API-key-shaped string is in the log",
    schedule_interval=None,
    catchup=False,
    tags=["sensitive-test"],
) as dag:
    rotate_key = PythonOperator(
        task_id="rotate_third_party_api_key",
        python_callable=rotate_third_party_api_key,
    )
