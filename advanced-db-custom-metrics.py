import array
import configparser
import json
import requests
import oracledb
import logging
import schedule
import time
from datetime import datetime, timedelta, timezone

logging.getLogger().setLevel(logging.INFO)
FORMAT = "%(asctime)s %(module)s %(levelname)s %(message)s"
logging.basicConfig(format=FORMAT)


config = configparser.ConfigParser()
config.read("config.ini")

app_config = {
    "timerange_historial_data_in_min": (
        int(config["app"]["timerange_historial_data_in_min"])
        if "timerange_historial_data_in_min" in config["app"]
        else 1440
    ),
    "filters": (
        json.loads(config["app"]["filters"]) if "filters" in config["app"] else None
    ),
}
appd_config = {
    "url": config["appdynamics-api"]["url"],
    "account_name": config["appdynamics-api"]["account_name"],
    "key": config["appdynamics-api"]["key"],
    "schema": config["appdynamics-api"]["schema"],
}
db_config = {
    "dsn": config["database"]["dsn"] if "dsn" in config["database"] else None,
    "user": config["database"]["user"],
    "pw": config["database"]["pw"],
    "host": config["database"]["host"] if "host" in config["database"] else None,
    "host": config["database"]["port"] if "port" in config["database"] else None,
    "host": config["database"]["sid"] if "sid" in config["database"] else None,
    "query": config["database"]["query"],
}

initial_execution_open = ":interval" in db_config["query"]


def list_of_list_to_dict(list_of_list):
    filters = app_config["filters"]
    result = {}
    for i in list_of_list:
        if i[0] in filters[0]["Values"]:
            if i[0] not in result:
                result[i[0]] = {}
            list_of_list_to_dict_child(result[i[0]], i[1:], filters[1:])

    return result


def list_of_list_to_dict_child(result, list_element, filters):
    if list_element[0] in filters[0]["Values"]:
        if len(list_element) > 2:
            if list_element[0] not in result:
                result[list_element[0]] = {}
            list_of_list_to_dict_child(
                result[list_element[0]], list_element[1:], filters[1:]
            )
        else:
            result[list_element[0]] = list_element[1]


def find_existing_data():
    logging.info(f"Querying AppDynamics API for last data")
    start = (
        datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(minutes=app_config["timerange_historial_data_in_min"])
    ).replace(second=0, microsecond=0)

    columns = [filter["Name"] for filter in app_config["filters"]]
    query = f'SELECT {", ".join(columns)}, max(eventTimestamp) FROM {appd_config["schema"]} WHERE eventTimestamp >= "{start.strftime("%Y-%m-%dT%H:%M:%SZ")}"'

    logging.info(f"query: {query}")
    response = requests.post(
        f'{appd_config["url"]}/events/query',
        data=query,
        headers={
            "Content-type": "application/vnd.appd.events+json;v=2",
            "X-Events-API-AccountName": appd_config["account_name"],
            "X-Events-API-Key": appd_config["key"],
        },
    )
    logging.debug(f"API Response: {response.json()}")
    existing_rows = response.json()[0]["results"]

    existing_data = list_of_list_to_dict(existing_rows)
    return existing_data


def get_data_from_db(interval: int = 1, existing_data=[]):

    if db_config["dsn"] is not None:
        with oracledb.connect(
            dsn=db_config["dsn"], user=db_config["user"], password=db_config["pw"]
        ) as connection:
            return get_data_from_db_with_conn(interval, existing_data, connection)
    else:
        with oracledb.connect(
            host=db_config["host"],
            port=db_config["port"],
            sid=db_config["sid"],
            user=db_config["user"],
            password=db_config["pw"],
        ) as connection:
            return get_data_from_db_with_conn(interval, existing_data, connection)


def get_data_from_db_with_conn(interval, existing_data, connection):
    with connection.cursor() as cursor:
        logging.info(f"Reading data from database")

        sql = db_config["query"]
        sql_params = (
            {"interval": interval} if ":interval" in db_config["query"] else {}
        )
        if app_config["filters"] is not None:
            format_map = {}
            for filter in app_config["filters"]:
                placeholder = f':{filter["Name"]}_{{}}'
                format_map[filter["Name"]] = ", ".join(
                    [placeholder.format(i) for i in range(len(filter["Values"]))]
                )

                for i in range(len(filter["Values"])):
                    sql_params[f'{filter["Name"]}_{i}'] = filter["Values"][i]

            sql = sql.format_map(format_map)

        data = []
        rows = cursor.execute(sql, sql_params)
        columns = cursor.description
        for row in rows:
            last_available_data = find_row_in_existing_data(row, existing_data)

            if last_available_data is None or row[0] > datetime.fromtimestamp(
                (last_available_data / 1000)
            ):
                data_row = {"eventTimestamp": row[0].strftime("%Y-%m-%dT%H:%M:%SZ")}
                for i in range(1, len(row)):
                    data_row[columns[i][0].title()] = row[i]
                data.append(data_row)

        logging.debug(f"DB response: {data}")
        logging.info(f"Received {len(data)} rows from database")
        return data


def find_row_in_existing_data(row, existing_data):

    pointer = existing_data
    for i in range(1, len(row) - 1):
        if row[i] not in pointer:
            return None
        else:
            if i == len(row) - 2:
                return pointer[row[i]]
            else:
                pointer = pointer[row[i]]


def publish_data(data):
    n = 5000
    data_chunks = [data[i : i + n] for i in range(0, len(data), n)]

    for data_chunk in data_chunks:
        logging.info(f"Publishing {len(data_chunk)} rows to AppDynamics API")
        response = requests.post(
            f'{appd_config["url"]}/events/publish/{appd_config["schema"]}',
            json=data_chunk,
            headers={
                "Content-type": "application/vnd.appd.events+json;v=2",
                "X-Events-API-AccountName": appd_config["account_name"],
                "X-Events-API-Key": appd_config["key"],
            },
        )
        logging.info(
            f"Published {len(data_chunk)} rows to AppDynamics API. StatusCode: {response.status_code}"
        )


def load_incremental_data_to_appdynamics():
    data = get_data_from_db()
    publish_data(data)


def load_initial_data_to_appdynamics():
    existing_data = find_existing_data()
    logging.debug(f"existing_data: {existing_data}")
    data = get_data_from_db(
        app_config["timerange_historial_data_in_min"], existing_data
    )
    publish_data(data)


def load_data_to_appdynamics():
    if initial_execution_open:
        load_initial_data_to_appdynamics()
    else:
        load_incremental_data_to_appdynamics()

schedule.every().minute.at(":02").do(load_data_to_appdynamics)
logging.info("Scheduled Job")

while True:
    schedule.run_pending()
    time.sleep(0.1)
