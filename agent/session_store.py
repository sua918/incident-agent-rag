"""Wave 12 — 프로세스 밖(SQLite 파일)에 두는 세션 저장소.

서버가 재시작돼도(새 프로세스, 새 인스턴스) 같은 session.db 파일을 다시 열므로
대화 이력이 유지된다. in-memory dict였다면 재시작 즉시 소실됐을 상태다.
"""
import sqlite3
import json
from uuid import uuid4
from contextlib import closing
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "session.db"


class SqliteSessionStore:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " thread_id TEXT, role TEXT, content TEXT, created TEXT)"
        )
        self.conn.execute("CREATE TABLE IF NOT EXISTS diagnoses(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, query TEXT NOT NULL, reply TEXT NOT NULL, status TEXT NOT NULL, validation_passed INTEGER, attempts INTEGER NOT NULL, failures_json TEXT NOT NULL, created TEXT NOT NULL)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_diagnoses_created ON diagnoses(created)")
        self.conn.commit()

    def load(self, thread_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE thread_id=? ORDER BY rowid",
            (thread_id,),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in rows]

    def append(self, thread_id: str, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?)",
            (thread_id, role, content, datetime.now().isoformat()),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def save_diagnosis(self, thread_id: str, query: str, result: dict) -> str | None:
        if result.get("route") != "incident":
            return None
        diagnosis_id = "D-" + uuid4().hex[:12].upper()
        passed = result.get("validation_passed")
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db, db:
            db.execute("INSERT INTO diagnoses VALUES(?,?,?,?,?,?,?,?,?)", (
                diagnosis_id, thread_id, query, result["final"], result.get("status", "completed"),
                None if passed is None else int(passed), result.get("validation_attempts", 0),
                json.dumps(result.get("failed_sources", []), ensure_ascii=False), datetime.now().astimezone().isoformat(timespec="seconds")))
        return diagnosis_id

    def list_diagnoses(self, query: str = "", status: str = "") -> list[dict]:
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT id,query,status,created,attempts FROM diagnoses WHERE (query LIKE ? OR reply LIKE ?) AND (?='' OR status=?) ORDER BY created DESC,rowid DESC LIMIT 100", ("%"+query+"%", "%"+query+"%", status, status)).fetchall()
        return [dict(row) for row in rows]

    def get_diagnosis(self, diagnosis_id: str) -> dict | None:
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM diagnoses WHERE id=?", (diagnosis_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["failed_sources"] = json.loads(value.pop("failures_json"))
        value["validation_passed"] = None if value["validation_passed"] is None else bool(value["validation_passed"])
        return value
