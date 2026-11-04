import sqlite3
import logging
from pathlib import Path
from typing import Dict, Any, List, Set, Optional

import config

logger = logging.getLogger("TTSDatabase")

DB_DIR = config.BASE_DIR / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "bot_database.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """สร้างตารางที่จำเป็นทั้งหมดในฐานข้อมูล SQLite"""
    with get_connection() as conn:
        cursor = conn.cursor()

        # 1. ตารางตั้งค่าเซิร์ฟเวอร์
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                bound_channel_id INTEGER,
                voice_channel_id INTEGER,
                voice TEXT DEFAULT 'th-TH-PremwadeeNeural',
                rate TEXT DEFAULT '+20%',
                read_name INTEGER DEFAULT 0,
                strip_emotes INTEGER DEFAULT 1,
                auto_language INTEGER DEFAULT 1,
                user_cooldown REAL DEFAULT 3.0,
                twitch_announce_channel INTEGER DEFAULT 1,
                twitch_read_name INTEGER DEFAULT 1,
                auto_reconnect INTEGER DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. ตารางช่อง Twitch ที่ผูกไว้
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS guild_twitch_channels (
                guild_id INTEGER,
                channel TEXT,
                PRIMARY KEY (guild_id, channel)
            )
        """)

        # 3. ตาราง Blacklist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS guild_blacklist (
                guild_id INTEGER,
                word TEXT,
                PRIMARY KEY (guild_id, word)
            )
        """)

        # 4. ตารางพจนานุกรมคำอ่านเฉพาะ (Custom Dictionary)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS custom_dictionary (
                guild_id INTEGER,
                word TEXT,
                replacement TEXT,
                PRIMARY KEY (guild_id, word)
            )
        """)

        conn.commit()
        logger.info(f"✅ ฐานข้อมูล SQLite พร้อมใช้งานที่: {DB_PATH}")


def load_guild_settings(guild_id: int) -> Optional[Dict[str, Any]]:
    """โหลดการตั้งค่าของเซิร์ฟเวอร์"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None


def save_guild_settings(guild_id: int, **kwargs):
    """บันทึกหรืออัปเดตการตั้งค่าของเซิร์ฟเวอร์"""
    allowed_cols = {
        "bound_channel_id", "voice_channel_id", "voice", "rate",
        "read_name", "strip_emotes", "auto_language", "user_cooldown",
        "twitch_announce_channel", "twitch_read_name", "auto_reconnect"
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed_cols}
    if not updates:
        return

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT guild_id FROM guild_settings WHERE guild_id = ?", (guild_id,))
        exists = cursor.fetchone()

        if exists:
            set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
            values = list(updates.values()) + [guild_id]
            cursor.execute(f"UPDATE guild_settings SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ?", values)
        else:
            cols = ["guild_id"] + list(updates.keys())
            placeholders = ", ".join(["?"] * len(cols))
            values = [guild_id] + list(updates.values())
            cursor.execute(f"INSERT INTO guild_settings ({', '.join(cols)}) VALUES ({placeholders})", values)
        conn.commit()


def get_guild_twitch_channels(guild_id: int) -> Set[str]:
    """ดึงรายชื่อช่อง Twitch ทั้งหมดของกิลด์"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT channel FROM guild_twitch_channels WHERE guild_id = ?", (guild_id,))
        return {row["channel"] for row in cursor.fetchall()}


def add_guild_twitch_channel(guild_id: int, channel: str):
    clean = channel.lower().strip().lstrip("#")
    if not clean:
        return
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO guild_twitch_channels (guild_id, channel)
            VALUES (?, ?)
        """, (guild_id, clean))
        conn.commit()


def remove_guild_twitch_channel(guild_id: int, channel: str):
    clean = channel.lower().strip().lstrip("#")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM guild_twitch_channels WHERE guild_id = ? AND channel = ?
        """, (guild_id, clean))
        conn.commit()


def clear_guild_twitch_channels(guild_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM guild_twitch_channels WHERE guild_id = ?", (guild_id,))
        conn.commit()


def get_guild_blacklist(guild_id: int) -> Set[str]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT word FROM guild_blacklist WHERE guild_id = ?", (guild_id,))
        return {row["word"] for row in cursor.fetchall()}


def add_guild_blacklist_word(guild_id: int, word: str):
    clean = word.strip().lower()
    if not clean:
        return
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO guild_blacklist (guild_id, word) VALUES (?, ?)", (guild_id, clean))
        conn.commit()


def remove_guild_blacklist_word(guild_id: int, word: str):
    clean = word.strip().lower()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM guild_blacklist WHERE guild_id = ? AND word = ?", (guild_id, clean))
        conn.commit()


def get_custom_dictionary(guild_id: int) -> Dict[str, str]:
    """ดึงพจนานุกรมคำอ่านเฉพาะทาง"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT word, replacement FROM custom_dictionary WHERE guild_id = ?", (guild_id,))
        return {row["word"]: row["replacement"] for row in cursor.fetchall()}


def set_custom_dictionary_word(guild_id: int, word: str, replacement: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO custom_dictionary (guild_id, word, replacement)
            VALUES (?, ?, ?)
        """, (guild_id, word.lower().strip(), replacement.strip()))
        conn.commit()


def get_all_guilds_for_reconnect() -> List[Dict[str, Any]]:
    """ดึงข้อมูลกิลด์ที่ต้องเชื่อมต่อห้องเสียงอัตโนมัติเมื่อบอทเปิดขึ้นมา"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT guild_id, voice_channel_id, bound_channel_id, auto_reconnect
            FROM guild_settings
            WHERE auto_reconnect = 1 AND voice_channel_id IS NOT NULL
        """)
        return [dict(row) for row in cursor.fetchall()]
