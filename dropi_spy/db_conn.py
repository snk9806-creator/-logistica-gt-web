"""Conexión a la base de datos que sirve para los dos escenarios:

- Sin `DATABASE_URL`: SQLite en el disco (como siempre, uso local de una persona).
- Con `DATABASE_URL`: Postgres en la nube, para que varias personas compartan
  los mismos datos desde ciudades distintas.

Para no reescribir cada consulta, se envuelve la conexión de Postgres en un
objeto que imita la interfaz de sqlite3 (`conn.execute(...)`, `?` como
marcador, `.fetchall()`, `.rowcount`).
"""
import os
import re
import sqlite3
from pathlib import Path

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ES_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

SQLITE_PATH = Path(__file__).resolve().parent / "data" / "guias.db"


def _a_postgres(sql: str) -> str:
    """Traduce el SQL de SQLite a Postgres."""
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    ignorar = "INSERT OR IGNORE INTO" in sql
    sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
    if ignorar and "ON CONFLICT" not in sql.upper():
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # Los marcadores ? de sqlite son %s en psycopg2 (sin tocar los de texto).
    sql = re.sub(r"\?", "%s", sql)
    return sql


class _Resultado:
    """Imita lo que devuelve conn.execute() en sqlite3."""

    def __init__(self, cur):
        self._cur = cur
        self.rowcount = cur.rowcount

    def fetchall(self):
        return self._cur.fetchall()

    def fetchone(self):
        return self._cur.fetchone()


class _ConexionPG:
    """Envoltura de psycopg2 con la interfaz de sqlite3.Connection."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(_a_postgres(sql), params)
        return _Resultado(cur)

    def executemany(self, sql, seq):
        cur = self._conn.cursor()
        cur.executemany(_a_postgres(sql), seq)
        return _Resultado(cur)

    def executescript(self, sql):
        cur = self._conn.cursor()
        for parte in sql.split(";"):
            if parte.strip():
                cur.execute(_a_postgres(parte))
        self._conn.commit()

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def conectar():
    if ES_POSTGRES:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        cn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return _ConexionPG(cn)

    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cn = sqlite3.connect(SQLITE_PATH)
    cn.row_factory = sqlite3.Row
    return cn


def describir() -> str:
    return "Postgres (compartida)" if ES_POSTGRES else "SQLite (solo esta computadora)"
