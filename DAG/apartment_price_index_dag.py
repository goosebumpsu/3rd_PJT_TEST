from airflow import DAG
from airflow.decorators import task
#from airflow.operators.python import PythonOperator
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.models import Variable
from airflow.providers.snowflake.hooks.snowflake import SnowflakeHook
from datetime import datetime, timedelta
import pandas as pd
import requests
import xml.etree.ElementTree as ET
import io
import logging

API_KEY = "7IRWNAGZQ2N16UR2VRYG"  # Variable.get 통해 값 넣기
BASE_URL = "https://ecos.bok.or.kr/api/StatisticSearch"
FREQ = "M"
PRICE_CODE = "901Y093"     # 매매지수
ITEM_CODE1 = "H69B"        

regions = {
    "national": "R70A",    # 전국
    "capital": "R70B",     # 수도권
    "provincial": "R70C"   # 지방
}

default_args = {
    'owner': 'airflow',
    'retries': 1,
    'retry_delay': timedelta(minutes=3)
}

def get_snowflake_connection():
    hook = SnowflakeHook(snowflake_conn_id='snowflake_dev_db')
    return hook.get_conn().cursor()


# API 호출 및 전처리, csv 저장
@task
def get_apartment_price_index_data(start_date: str = "202201") -> str:
    records = []

    # 종료일 = 현재 월의 -1개월
    now = datetime.now()
    first_day_this_month = now.replace(day=1)
    last_month = first_day_this_month - timedelta(days=1)
    end = last_month.strftime("%Y%m")

    for region_name, region_code in regions.items():
        url = f"{BASE_URL}/{API_KEY}/xml/kr/1/1000/{PRICE_CODE}/{FREQ}/{start_date}/{end}/{ITEM_CODE1}/{region_code}"
        try:
            res = requests.get(url, timeout=10)
            res.raise_for_status()
            root = ET.fromstring(res.content)
        except (requests.RequestException, ET.ParseError) as e:
            raise ValueError(f"API 요청/파싱 실패: {e}")

        for row in root.iter("row"):
            time = row.find("TIME").text
            value = row.find("DATA_VALUE").text
            records.append({
                "TIME": time,
                region_name: float(value) if value not in ["", None] else None
            })

    df = pd.DataFrame(records)
    df = df.groupby("TIME").first().reset_index()
    df["TIME"] = pd.to_datetime(df["TIME"], format="%Y%m")
    df["year"] = df["TIME"].dt.year
    df["month"] = df["TIME"].dt.month
    df_final = df[["year", "month", "national", "capital", "provincial"]]

    csv_buffer = io.StringIO()
    df_final.to_csv(csv_buffer, index=False)
    return csv_buffer.getvalue()

# 데이터 S3로 적재
@task
def upload_to_s3(csv_data: str, ds_nodash: str):
    hook = S3Hook(aws_conn_id="AWS_CONN_ID")
    key = f"apartment/price_index/{ds_nodash}.csv"
    bucket = "de6-3rd-pjt"  # 실사용 시 수정 필요

    hook.load_string(
        string_data=csv_data,
        key=key,
        bucket_name=bucket,  
        replace=True
    )
    logging.info(f"S3 업로드 완료: s3://{bucket}/{key}")
    return key  # key를 다음 task로 전달

# S3 -> Snowflake COPY
@task
def s3_to_snowflake(s3_key: str, ds_nodash: str):
    cur = get_snowflake_connection()
    
    s3_bucket = "de6-3rd-pjt"
    s3_path = f"s3://{s3_bucket}/{s3_key}"

    create_replace_sql = """
    CREATE OR REPLACE TABLE raw_data.apartment_price_index (
        year INT,
        month INT,
        national FLOAT,
        capital FLOAT,
        provincial FLOAT,
        created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """

    copy_sql = f"""
    COPY INTO raw_data.apartment_price_index
    FROM 's3://{s3_bucket}/{s3_key}'
    CREDENTIALS = (
        AWS_KEY_ID = Variable.get('aws_access_key')      -- variable.get()으로 지정
        AWS_SECRET_KEY= Variable.get('aws_secret_key')  -- variable.get()으로 지정
    )
    FILE_FORMAT = (
        TYPE = 'CSV' 
        FIELD_OPTIONALLY_ENCLOSED_BY = '"' 
        SKIP_HEADER = 1);
    """

    try:
        cur.execute("BEGIN;")
        cur.execute(create_replace_sql)
        cur.execute(copy_sql)
        cur.execute("COMMIT;")
        logging.info("Snowflake COPY 성공")
    except Exception as e:
        cur.execute("ROLLBACK;")
        raise
    finally:
        cur.close()


with DAG(
    dag_id='apartment_price_index_dag',
    default_args=default_args,
    description='아파트 가격 지수 ETL DAG',
    schedule_interval='@monthly',  # 매월 1일 자정에 실행
    start_date=datetime(2023, 1, 1),
    catchup=False,
    max_active_runs=1,
) as dag:

    csv_data = get_apartment_price_index_data()
    s3_key = upload_to_s3(csv_data=csv_data, ds_nodash="{{ ds_nodash }}")
    s3_to_snowflake(s3_key=s3_key, ds_nodash="{{ ds_nodash }}")