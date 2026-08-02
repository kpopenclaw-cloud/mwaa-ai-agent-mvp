"""
Test fixture for the MWAA agent's redaction pipeline - NOT a real
workflow. This task deliberately fails with an AWS-key-shaped string in
its exception message, so a diagnostic question about this DAG exercises
mwaa_agent.validation.postvalidation for real: the fake key reaches the
task log, then the agent's answer, and should come back as [REDACTED].

The key is built by concatenation (not one literal string in this file)
so it never appears as a contiguous AKIA-shaped match in the committed
source and can't trip GitHub secret-scanning - it still fully matches
the redaction pattern once Python joins the pieces at runtime.
"""

from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime


def connect_with_leaked_credentials(**_kwargs):
    access_key = "AKIA" + "FAKE1234567890AB"
    secret_field = "aws_secret_access_key=" + "FAKEsecretKeyForTesting1234567890"
    raise ConnectionError(
        f"Failed to connect to legacy reporting service using access key "
        f"{access_key} and {secret_field}: connection refused on port 5439"
    )


default_args = {
    "owner": "airflow",
    "start_date": datetime(2024, 1, 1),
}

with DAG(
    dag_id="sensitive_iam_credential_leak",
    default_args=default_args,
    description="Test fixture: fails while an AWS-key-shaped string is in the log",
    schedule_interval=None,
    catchup=False,
    tags=["sensitive-test"],
) as dag:
    leak_credentials = PythonOperator(
        task_id="connect_with_leaked_credentials",
        python_callable=connect_with_leaked_credentials,
    )
