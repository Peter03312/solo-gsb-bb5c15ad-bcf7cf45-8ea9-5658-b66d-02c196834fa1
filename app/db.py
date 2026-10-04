"""SQLite 存储层。

一致性要点：
- 所有"读-改-写"（标注修订号 check-and-set、发布）都在
  ``BEGIN IMMEDIATE`` 事务内完成，SQLite 写锁将其串行化，
  因此发布读到的标注修订与写入记录的修订必然一致。
- 标注矩形按 (doc_id, revision) 追加存储、永不改写，
  已发布成品对应的遮盖坐标因此不可变。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id                  TEXT PRIMARY KEY,
    filename            TEXT NOT NULL,
    media_type          TEXT NOT NULL,
    original_path       TEXT NOT NULL,
    original_sha256     TEXT NOT NULL,
    width               INTEGER NOT NULL,   -- EXIF 旋正后的宽
    height              INTEGER NOT NULL,   -- EXIF 旋正后的高
    annotation_revision INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotation_rects (
    doc_id   TEXT NOT NULL REFERENCES documents(id),
    revision INTEGER NOT NULL,
    idx      INTEGER NOT NULL,
    x        INTEGER NOT NULL,
    y        INTEGER NOT NULL,
    w        INTEGER NOT NULL,
    h        INTEGER NOT NULL,
    reason   TEXT NOT NULL,
    PRIMARY KEY (doc_id, revision, idx)
);

CREATE TABLE IF NOT EXISTS publications (
    doc_id              TEXT NOT NULL REFERENCES documents(id),
    version             INTEGER NOT NULL,
    annotation_revision INTEGER NOT NULL,
    original_sha256     TEXT NOT NULL,
    output_sha256       TEXT NOT NULL,
    output_path         TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (doc_id, version)
);
"""


def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()


def connect(db_path: str) -> sqlite3.Connection:
    # isolation_level=None：手动控制事务边界
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def read_tx(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx(db_path: str) -> Iterator[sqlite3.Connection]:
    """独占写事务：BEGIN IMMEDIATE 使并发写者串行执行。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
