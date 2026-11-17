"""SQLite 存储：数据集原文、运行记录与完整结果快照。

约定：
- 数据集按名字唯一：同名文件重新采信即覆盖原文，历史运行仍按 digest 保留；
- 运行记录按 (dataset, digest, rules_version) 幂等：同输入重跑不新增行；
- run_artifacts 按 run_id 只插不留地保存每次运行的输入原文，同名换版后
  旧运行仍能按自己那一版字节复核（M8 对比只信这张表，不信可覆盖的 datasets）；
- comparisons 按 (run_id 无序对, rules_version) 幂等，整快照单事务写入，
  失败回滚，同一对批次重跑不新增版本；
- 分数与比率落 TEXT（Decimal 字符串），不落 float；
- 页面永远只展示一条运行/对比记录，导出读的是同一条快照。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    name TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    encoding TEXT NOT NULL,
    declared_encoding TEXT,
    bytes INTEGER NOT NULL,
    content BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    version INTEGER NOT NULL,
    score TEXT NOT NULL,
    grade TEXT NOT NULL,
    rows_in INTEGER NOT NULL,
    rows_accepted INTEGER NOT NULL,
    rows_unique INTEGER NOT NULL,
    rows_dup INTEGER NOT NULL,
    rows_quarantine INTEGER NOT NULL,
    rows_blank INTEGER NOT NULL,
    cells_total INTEGER NOT NULL,
    cells_missing INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_seq INTEGER NOT NULL,
    summary_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_identity ON runs(name, digest, rules_version);
CREATE INDEX IF NOT EXISTS idx_runs_version ON runs(name, version DESC);
CREATE TABLE IF NOT EXISTS comparisons (
    comparison_id TEXT PRIMARY KEY,
    base_run_id TEXT NOT NULL,
    target_run_id TEXT NOT NULL,
    pair_key TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    score_delta TEXT NOT NULL,
    quality_effect TEXT,
    coverage_effect TEXT,
    verdict_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_comparisons_pair
ON comparisons(pair_key, rules_version);
CREATE INDEX IF NOT EXISTS idx_comparisons_seen ON comparisons(seen_seq DESC);
CREATE TABLE IF NOT EXISTS run_artifacts (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    encoding TEXT NOT NULL,
    declared_encoding TEXT,
    bytes INTEGER NOT NULL,
    content BLOB NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _dec(text):
    return None if text is None else Decimal(str(text))


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 数据集 ----------
    def save_dataset(self, *, name: str, digest: str, encoding: str,
                     declared_encoding: str | None, data: bytes, stamp: str) -> None:
        """存原始字节：GB18030 文件重算摘要必须与首次采信逐位相同。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO datasets (name,digest,encoding,declared_encoding,bytes,
                        content,updated_at) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     digest=excluded.digest, encoding=excluded.encoding,
                     declared_encoding=excluded.declared_encoding,
                     bytes=excluded.bytes, content=excluded.content,
                     updated_at=excluded.updated_at""",
                (name, digest, encoding, declared_encoding, len(data), data, stamp))

    def get_dataset(self, name: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM datasets WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def list_datasets(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT name,digest,encoding,bytes,updated_at FROM datasets ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 运行记录 ----------
    def latest_version(self, name: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM runs WHERE name=?", (name,)).fetchone()
        return int(row["v"] or 0)

    def find_run(self, name: str, digest: str, rules_version: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE name=? AND digest=? AND rules_version=?",
                (name, digest, rules_version)).fetchone()
        return dict(row) if row else None

    def save_run(self, run: dict, summary: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO runs (run_id,name,digest,rules_version,version,score,grade,
                        rows_in,rows_accepted,rows_unique,rows_dup,rows_quarantine,
                        rows_blank,cells_total,cells_missing,created_at,finished_at,
                        last_seen_at,seen_seq,summary_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run["run_id"], run["name"], run["digest"], run["rules_version"],
                 int(run["version"]), str(run["score"]), run["grade"],
                 int(run["rows_in"]), int(run["rows_accepted"]), int(run["rows_unique"]),
                 int(run["rows_dup"]), int(run["rows_quarantine"]),
                 int(run["rows_blank"]), int(run["cells_total"]),
                 int(run["cells_missing"]), run["created_at"], run["finished_at"],
                 run["created_at"], self._next_seq(conn),
                 json.dumps(summary, ensure_ascii=False)))

    def touch_run(self, run_id: str, stamp: str) -> None:
        """命中幂等复用时刷新「最近一次查看」，导航回各页仍停在这条记录上。"""
        with self.connect() as conn:
            conn.execute("UPDATE runs SET last_seen_at=?, seen_seq=? WHERE run_id=?",
                         (stamp, self._next_seq(conn), run_id))

    def _next_seq(self, conn) -> int:
        row = conn.execute("SELECT COALESCE(MAX(seen_seq), 0) + 1 AS n FROM runs").fetchone()
        return int(row["n"])

    def latest_run(self, name: str | None = None) -> dict | None:
        with self.connect() as conn:
            if name:
                row = conn.execute(
                    "SELECT * FROM runs WHERE name=? ORDER BY version DESC LIMIT 1",
                    (name,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM runs ORDER BY seen_seq DESC LIMIT 1"
                ).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY seen_seq DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    def load_result(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT summary_json FROM runs WHERE run_id=?",
                               (run_id,)).fetchone()
        return json.loads(row["summary_json"]) if row else None

    def count_runs(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"])

    # ---------- 批次对比 ----------
    def find_comparison(self, pair_key: str, rules_version: str) -> dict | None:
        """同一对批次按 (pair_key, 规则版本) 幂等：重跑不新增、不重复累计。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM comparisons WHERE pair_key=? AND rules_version=?",
                (pair_key, rules_version)).fetchone()
        return dict(row) if row else None

    def get_comparison(self, comparison_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM comparisons WHERE comparison_id=?",
                               (comparison_id,)).fetchone()
        return dict(row) if row else None

    def list_comparisons(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM comparisons ORDER BY seen_seq DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    def save_comparison(self, record: dict, payload: dict) -> None:
        """整快照单事务写入：失败回滚，库里上一完整版本原样保留。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO comparisons (comparison_id,base_run_id,target_run_id,
                        pair_key,rules_version,score_delta,quality_effect,
                        coverage_effect,verdict_code,created_at,last_seen_at,
                        seen_seq,payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record["comparison_id"], record["base_run_id"],
                 record["target_run_id"], record["pair_key"],
                 record["rules_version"], str(record["score_delta"]),
                 None if record.get("quality_effect") is None
                 else str(record["quality_effect"]),
                 str(record["coverage_effect"]), record["verdict_code"],
                 record["created_at"], record["created_at"],
                 self._next_seq(conn),
                 json.dumps(payload, ensure_ascii=False)))

    def touch_comparison(self, comparison_id: str, stamp: str) -> None:
        """幂等命中只刷新查看顺序，不改快照、不新增行。"""
        with self.connect() as conn:
            conn.execute(
                "UPDATE comparisons SET last_seen_at=?, seen_seq=? WHERE comparison_id=?",
                (stamp, self._next_seq(conn), comparison_id))

    def count_comparisons(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) AS n FROM comparisons").fetchone()["n"])

    # ---------- 运行级不可变原文（按 run_id 留存，同名换版不覆盖） ----------
    def save_run_artifact(self, *, run_id: str, name: str, digest: str,
                          encoding: str, declared_encoding: str | None,
                          data: bytes, stamp: str) -> None:
        """只插不更新：每个 run_id 留存自己的输入原文，旧版永不被顶掉。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO run_artifacts
                   (run_id,name,digest,encoding,declared_encoding,bytes,
                    content,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (run_id, name, digest, encoding, declared_encoding,
                 len(data), data, stamp))

    def get_run_artifact(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM run_artifacts WHERE run_id=?",
                               (run_id,)).fetchone()
        return dict(row) if row else None
