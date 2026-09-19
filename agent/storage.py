import json
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Optional
from datetime import datetime
from config import settings
class SessionStorage:

  def __init__(self, db_path: Optional[str | Path] = None) -> None:
        # 优先使用入参，缺省时对齐配置中心
        self.db_path = Path(db_path) if db_path else settings.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

  def _get_connection(self) -> sqlite3.Connection:
    """获取 SQLite 连接。"""
    return sqlite3.connect(str(self.db_path))

  def _init_db(self) -> None:
    """建表：如果不存在则创建 sessions 表。"""
    # 【底层踩坑防错点（Caveat）：为什么不能只用 `with conn:`】
    # sqlite3.Connection 的 `with` 是【事务】上下文管理器，退出时只做
    # commit/rollback，**不会**调用 conn.close()。若只写 `with conn:`，
    # 底层文件句柄会一直存活到该对象被 GC 回收为止；在 Windows 上这会
    # 长期占用 data/*.db，使其处于锁定态（WinError 32），导致 git 等外部
    # 工具无法删除/移动该文件。故外层必须再套 closing() 强制释放句柄。
    with closing(self._get_connection()) as conn:
      with conn:  # 事务边界：正常退出提交，异常自动回滚
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

    # closing 保证无论是否抛异常，退出时都显式 conn.close() 释放句柄
    with closing(self._get_connection()) as conn:
      with conn:  # 事务边界：正常退出提交，异常自动回滚
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
    with closing(self._get_connection()) as conn:
      cursor = conn.execute(
          "SELECT messages_json FROM sessions WHERE session_id = ?",
          (session_id,),
      )
      row = cursor.fetchone()
      if row:
        return json.loads(row[0])
      return None
