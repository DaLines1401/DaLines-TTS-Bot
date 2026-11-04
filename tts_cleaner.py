import re
import discord

URL_PATTERN = re.compile(r"https?://\S+")
DISCORD_EMOJI_PATTERN = re.compile(r"<a?:([a-zA-Z0-9_]+):\d+>")
USER_MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_PATTERN = re.compile(r"<@&(\d+)>")
CHANNEL_MENTION_PATTERN = re.compile(r"<#(\d+)>")
CODE_BLOCK_PATTERN = re.compile(r"```[\s\S]*?```")
SPOILER_PATTERN = re.compile(r"\|\|.*?\|\|")
INLINE_CODE_PATTERN = re.compile(r"`([^`]+)`")
MARKDOWN_PATTERN = re.compile(r"[*_~]")

# รายการแปลงคำอ่านสำหรับคำสแลงเกม/คำทับศัพท์/ชื่อเฉพาะ (Gamer & Streamer Slang Dictionary)
DEFAULT_PRONUNCIATION_MAP = [
    (re.compile(r"(?i)\bdalines?\b"), "ดาไลน์"),
    (re.compile(r"(?i)\bggwp\b"), "กู๊ดเกมเวลเพลย์"),
    (re.compile(r"(?i)\bgg\b"), "กู๊ดเกม"),
    (re.compile(r"(?i)\bez\b"), "อีซี่"),
    (re.compile(r"(?i)\bnt\b"), "ไนซ์ทราย"),
    (re.compile(r"(?i)\bns\b"), "ไนซ์ช็อต"),
    (re.compile(r"(?i)\bclutch\b"), "คลัตช์"),
    (re.compile(r"(?i)\bdiff\b"), "ดิฟ"),
    (re.compile(r"(?i)\bace\b"), "เอซ"),
    (re.compile(r"(?i)\bsova\b"), "โซวา"),
    (re.compile(r"(?i)\bjett\b"), "เจ็ต"),
    (re.compile(r"(?i)\breyna\b"), "เรย์น่า"),
    (re.compile(r"(?i)\bomen\b"), "โอเมน"),
    (re.compile(r"(?i)\bbrim\b"), "บริม"),
    (re.compile(r"(?i)\bviper\b"), "ไวเปอร์"),
    (re.compile(r"(?i)\bcypher\b"), "ไซเฟอร์"),
    (re.compile(r"(?i)\bkilljoy\b"), "คิลจอย"),
    (re.compile(r"(?i)\bchamber\b"), "แชมเบอร์"),
    (re.compile(r"(?i)\biso\b"), "ไอโซ"),
    (re.compile(r"(?i)\bclove\b"), "โคลฟ"),
    (re.compile(r"(?i)\bvyse\b"), "ไวส์"),
    (re.compile(r"(?i)\bult\b"), "อัลติ"),
]


def apply_pronunciation_fixes(text: str, custom_dict: dict = None) -> str:
    """แปลงคำอ่านเฉพาะทางให้เสียงพูดภาษาไทยอ่านได้ถูกต้องเป็นธรรมชาติ"""
    if custom_dict:
        for word, repl in custom_dict.items():
            pattern = re.compile(rf"(?i)\b{re.escape(word)}\b")
            text = pattern.sub(repl, text)

    for pattern, repl in DEFAULT_PRONUNCIATION_MAP:
        text = pattern.sub(repl, text)

    return text


def clean_discord_message(message: discord.Message, max_length: int = 250, custom_dict: dict = None) -> str:
    """
    แปลงและกรองข้อความจาก Discord ให้พร้อมสำหรับส่งไปอ่าน TTS
    """
    raw_content = message.content or ""
    guild = message.guild

    # ถ้าไม่มีเนื้อหา แต่มีไฟล์แนบหรือสติกเกอร์
    if not raw_content.strip():
        if message.attachments:
            # เช็คว่ามีรูปภาพหรือไม่
            is_image = any(att.content_type and "image" in att.content_type for att in message.attachments)
            return "ส่งรูปภาพ" if is_image else "ส่งไฟล์แนบ"
        if message.stickers:
            return "ส่งสติกเกอร์"
        return ""

    # แทนที่ Code Block
    text = CODE_BLOCK_PATTERN.sub("ส่งโค้ด", raw_content)

    # แทนที่ Spoiler
    text = SPOILER_PATTERN.sub("ข้อความสปอยล์", text)

    # แปลง URL
    text = URL_PATTERN.sub("ส่งลิงก์", text)

    # แปลง Mention ผู้ใช้ เป็นชื่อผู้ใช้
    if guild:
        def replace_user(match):
            user_id = int(match.group(1))
            member = guild.get_member(user_id)
            if member:
                return f"@{member.display_name}"
            return "@ผู้ใช้"
        text = USER_MENTION_PATTERN.sub(replace_user, text)

        def replace_role(match):
            role_id = int(match.group(1))
            role = guild.get_role(role_id)
            if role:
                return f"@{role.name}"
            return "@ยศ"
        text = ROLE_MENTION_PATTERN.sub(replace_role, text)

        def replace_channel(match):
            ch_id = int(match.group(1))
            ch = guild.get_channel(ch_id)
            if ch:
                return f"#{ch.name}"
            return "#ห้อง"
        text = CHANNEL_MENTION_PATTERN.sub(replace_channel, text)
    else:
        text = USER_MENTION_PATTERN.sub("@ผู้ใช้", text)
        text = ROLE_MENTION_PATTERN.sub("@ยศ", text)
        text = CHANNEL_MENTION_PATTERN.sub("#ห้อง", text)

    # แปลง Discord Custom Emoji ให้เหลือแค่ชื่อ หรือตัดออก
    text = DISCORD_EMOJI_PATTERN.sub(r":\1:", text)

    # จัดการ Inline code และ Markdown ทั่วไป
    text = INLINE_CODE_PATTERN.sub(r"\1", text)
    text = MARKDOWN_PATTERN.sub("", text)

    # ตัดช่องว่างซ้ำซ้อน และขึ้นบรรทัดใหม่
    text = re.sub(r"\s+", " ", text).strip()

    # ตัดความยาวหากเกินกำหนด
    if len(text) > max_length:
        text = text[:max_length] + "... ข้อความยาวเกินไป"

    # แปลงคำอ่านเฉพาะทาง/คำสแลงเกม
    text = apply_pronunciation_fixes(text, custom_dict)

    return text
