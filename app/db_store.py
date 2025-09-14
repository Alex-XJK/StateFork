import os
import psycopg2
from psycopg2.extras import RealDictCursor
import logging

logger = logging.getLogger(__name__)

class DBStore:
    """
    A PostgreSQL-based key-value store.
    """
    def __init__(self, preload: bool = False):
        self.db_config = {
            'host': 'localhost',
            'database': os.getenv('POSTGRES_DB', 'statefork_db'),
            'user': os.getenv('POSTGRES_USER', 'postgres'),
            'password': os.getenv('POSTGRES_PASSWORD', 'postgres'),
            'port': int(os.getenv('POSTGRES_PORT', '5433'))
        }
        self._init_db()
        if preload:
            self._preload()

    def _get_connection(self):
        """Get a database connection."""
        try:
            return psycopg2.connect(**self.db_config)
        except psycopg2.Error as e:
            logger.error(f"Database connection failed: {e}")
            raise

    def _init_db(self):
        """Initialize database table if it doesn't exist."""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS kv_store (
                            key VARCHAR(255) PRIMARY KEY,
                            value TEXT
                        )
                    """)
                    conn.commit()
                    logger.info("Database table initialized successfully")
        except psycopg2.Error as e:
            logger.error(f"Database initialization failed: {e}")
            raise

    def get(self, key: str):
        """Get value for a given key."""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
                    result = cur.fetchone()
                    return result[0] if result else None
        except psycopg2.Error as e:
            logger.error(f"Database get operation failed: {e}")
            return None

    def set(self, key: str, value: str):
        """Set value for a given key."""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO kv_store (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        (key, value)
                    )
                    conn.commit()
                    return True
        except psycopg2.Error as e:
            logger.error(f"Database set operation failed: {e}")
            return False

    def all(self):
        """Get all key-value pairs."""
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT key, value FROM kv_store")
                    results = cur.fetchall()
                    return {row['key']: row['value'] for row in results}
        except psycopg2.Error as e:
            logger.error(f"Database all operation failed: {e}")
            return {}

    def delete(self, key: str):
        """Delete a key-value pair."""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM kv_store WHERE key = %s", (key,))
                    conn.commit()
                    return cur.rowcount > 0
        except psycopg2.Error as e:
            logger.error(f"Database delete operation failed: {e}")
            return False

    def _preload(self):
        """
        Preload the database with some initial data.
        This is useful for testing and development.
        """
        initial_data = {
            "key1": "example_value1",
            "key2": "example_value2", 
            "key3": "example_value3",
            "key4": "example_value4",
            "key5": "example_value5"
        }
        
        for key, value in initial_data.items():
            self.set(key, value)
        
        logger.info("Database preloaded with initial data") 