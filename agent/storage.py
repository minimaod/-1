import json
from pathlib import Path
import sqlite3
from typing import Optional
from datetime import datetime

class SessionStorage:

  def __init__(self, db_path: str = "data/local_agent.db"):
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    self._init_db()

  def _get_connection(self) -> sqlite3.Connection:
    """获取 SQLite 连接。"""
    return sqlite3.connect(str(self.db_path))

  def _init_db(self):
    """建表：如果不存在则创建 sessions 表。"""
    with self._get_connection() as conn:
      conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                messages_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)
      conn.commit()

  def save_session(self, session_id: str, messages: list[dict]) -> None:
    """持久化保存会话历史（UPSERT）。"""
    messages_json = json.dumps(messages, ensure_ascii=False, default=str)
    now = datetime.utcnow().isoformat()

    with self._get_connection() as conn:
      conn.execute(
          """
            INSERT INTO sessions (session_id, messages_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                messages_json = excluded.messages_json,
                updated_at = excluded.updated_at;
            """,
          (session_id, messages_json, now),
      )
      conn.commit()

  def load_session(self, session_id: str) -> Optional[list[dict]]:
    """从持久化层加载会话历史。"""
    with self._get_connection() as conn:
      cursor = conn.execute(
          "SELECT messages_json FROM sessions WHERE session_id = ?",
          (session_id,),
      )
      row = cursor.fetchone()
      if row:
        return json.loads(row[0])
      return None