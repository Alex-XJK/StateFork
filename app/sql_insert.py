import time
import os
import psycopg2
import argparse
from datetime import datetime


LOG_PATH = "/tmp/client_report.txt"

def log(msg):
    with open(LOG_PATH, "a") as f:
        f.write(f"[{datetime.now()}] {msg}\n")


DB_HOST = "128.110.220.78"
DB_PORT = 5432
DB_NAME = "postgres"
DB_USER = "postgres"

def make_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=''
    )

def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS client_log (
                id SERIAL PRIMARY KEY,
                pid INTEGER,
                msg TEXT,
                ts TIMESTAMP DEFAULT now()
            );
        """)
        conn.commit()


COUNTER = 0

def insert_test_data(conn, pid):
    global COUNTER
    COUNTER += 1
    msg = f"Counter value {COUNTER}"

    with conn.cursor() as cur:
        cur.execute("INSERT INTO client_log (pid, msg) VALUES (%s, %s);", (pid, msg))
        conn.commit()

    return msg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reconnect', action='store_true', help='Reconnect every iteration')
    args = parser.parse_args()

    conn = None
    pid = os.getpid()
    while True:
        try:
            if args.reconnect or conn is None:
                conn = make_connection()
                ensure_table(conn)
                log(f"Connected to DB successfully, user specified reconnect {args.reconnect}")

            message = insert_test_data(conn, pid)
            log(f"Inserted: {message}")

        except Exception as e:
            log(f"Error: {e}")
            if conn:
                conn.close()
                conn = None

        if args.reconnect and conn:
            conn.close()
            conn = None

        time.sleep(5)

if __name__ == "__main__":
    main()