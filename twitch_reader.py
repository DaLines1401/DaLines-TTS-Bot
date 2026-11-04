import asyncio
import logging
import random
import re
import time
from typing import Callable, Coroutine, Dict, Optional, Set

logger = logging.getLogger("TwitchReader")

# บอทที่พบบ่อยในแชท Twitch ที่ไม่ควรนำมาอ่านออกเสียง
IGNORED_USERS: Set[str] = {
    "nightbot",
    "streamelements",
    "moobot",
    "fossabot",
    "soundalerts",
    "wizebot",
    "botrix",
    "streamlabs",
    "stay_hydrated_bot",
    "sery_bot",
}

URL_PATTERN = re.compile(r"https?://\S+")

# พจนานุกรมคำสแลงและคำย่อเกมยอดนิยม
GAMER_SLANG = [
    (re.compile(r"\bdalines?\b", re.IGNORECASE), "ดาไลน์"),
    (re.compile(r"\bggwp\b", re.IGNORECASE), "กู๊ดเกมเวลเพลย์"),
    (re.compile(r"\bgg\b", re.IGNORECASE), "กู๊ดเกม"),
    (re.compile(r"\bnt\b", re.IGNORECASE), "ไนซ์ทราย"),
    (re.compile(r"\bwp\b", re.IGNORECASE), "เวลเพลย์"),
    (re.compile(r"\bns\b", re.IGNORECASE), "ไนซ์ช็อต"),
    (re.compile(r"\bez\b", re.IGNORECASE), "อีซี่"),
    (re.compile(r"\bmvp\b", re.IGNORECASE), "เอ็มวีพี"),
    (re.compile(r"\badr\b", re.IGNORECASE), "เอดีอาร์"),
    (re.compile(r"\bclutch\b", re.IGNORECASE), "คลัตช์"),
    (re.compile(r"\bdiff\b", re.IGNORECASE), "ดิฟ"),
    (re.compile(r"\bace\b", re.IGNORECASE), "เอซ"),
    (re.compile(r"\bsova\b", re.IGNORECASE), "โซวา"),
    (re.compile(r"\bjett\b", re.IGNORECASE), "เจ็ต"),
    (re.compile(r"\breyna\b", re.IGNORECASE), "เรย์น่า"),
    (re.compile(r"\bult\b", re.IGNORECASE), "อัลติ"),
    (re.compile(r"\bop\b", re.IGNORECASE), "โอพี"),
    (re.compile(r"555\+"), "ห้าห้าห้าบวก"),
]

# คลัง Emotes ยอดนิยมระดับโลก (7TV / BTTV / FrankerFaceZ)
POPULAR_THIRD_PARTY_EMOTES = [
    "kekw", "omegalul", "pog", "pogu", "pogchamp", "catjam", "pepehands",
    "monkas", "monkaw", "monkaomega", "huh", "icant", "ayaya", "gigachad",
    "xdd", "pepew", "lul", "lulw", "kappa", "biblethump", "kreygasm",
    "residentsleeper", "notlikethis", "wutface", "dansgame", "feelsgoodman",
    "feelsbadman", "pepega", "widepeepohappy", "sadge", "copium", "clueless",
    "aware", "forsene", "smoge", "pepelaugh", "batchest", "nerd", "sus",
    "modcheck", "pausechamp", "peepoclap", "gachihyper", "peeporun",
    "peeposad", "dankge", "catdespair", "pepejam", "yikes", "pepepoint",
    "prayge", "despair", "based", "bop", "peeposhy", "5head", "3head",
    "poggies", "pepel", "pressf", "rip"
]

THIRD_PARTY_EMOTE_PATTERN = re.compile(
    r"\b(?:" + "|".join(POPULAR_THIRD_PARTY_EMOTES) + r")\b",
    re.IGNORECASE
)


def strip_official_emotes(text: str, emotes_tag: str) -> str:
    """
    ตัด Official Twitch Emotes ตามช่วง Index ที่ได้รับจาก IRC tag (เช่น 25:0-4,12-16)
    """
    if not emotes_tag:
        return text

    ranges = []
    for emote_data in emotes_tag.split("/"):
        if ":" in emote_data:
            _, positions = emote_data.split(":", 1)
            for pos in positions.split(","):
                if "-" in pos:
                    try:
                        s, e = map(int, pos.split("-"))
                        ranges.append((s, e))
                    except ValueError:
                        pass

    if not ranges:
        return text

    # เรียงลำดับจากหลังมาหน้า เพื่อให้ index ก่อนหน้าไม่เลื่อนเมื่อถูกลบ
    ranges.sort(key=lambda r: r[0], reverse=True)
    chars = list(text)
    for s, e in ranges:
        if 0 <= s <= e < len(chars):
            del chars[s:e + 1]

    return "".join(chars)


def clean_twitch_message(
    text: str,
    emotes_tag: str = "",
    strip_emotes: bool = True,
    max_length: int = 250,
    blacklist: Optional[Set[str]] = None
) -> str:
    """
    กรองและปรับแต่งข้อความจากแชท Twitch:
    - ตัดคำสั่ง (!, /, $, ., %)
    - ตรวจสอบคำใน Blacklist
    - ตัด Official Twitch Emotes และ 7TV/BTTV Emotes
    - แทนที่ลิงก์ด้วย 'ส่งลิงก์'
    - แทนที่คำสแลงเกม
    - ป้องกันข้อความสแปมตัวอักษรซ้ำ
    """
    raw = text.strip()
    if not raw or raw.startswith(("!", "/", "$", ".", "%")):
        return ""

    # ตรวจสอบ Blacklist (ถ้ามีคำต้องห้าม ให้ยกเลิกข้อความนี้ทันที)
    if blacklist:
        raw_lower = raw.lower()
        for banned in blacklist:
            if banned and banned.lower() in raw_lower:
                logger.info(f"[Twitch Filter] ข้ามข้อความเนื่องจากติด Blacklist: '{banned}'")
                return ""

    # ตัด Official Twitch Emotes
    if strip_emotes and emotes_tag:
        raw = strip_official_emotes(raw, emotes_tag)

    # ตัด 7TV / BTTV Emotes
    if strip_emotes:
        raw = THIRD_PARTY_EMOTE_PATTERN.sub("", raw)

    # ตัดช่องว่าง
    text = raw.strip()
    if not text:
        return ""

    # ตรวจสอบว่ามีตัวอักษรหรือตัวเลขจริงๆ หลงเหลืออยู่หรือไม่ (ป้องกันกรณีเหลือแค่อักขระแปลกๆ)
    if not re.search(r"[\w\u0e00-\u0e7f]", text):
        return ""

    # แทนที่ URL ด้วยคำว่า 'ส่งลิงก์'
    text = URL_PATTERN.sub("ส่งลิงก์", text)

    # แทนที่คำสแลงเกม
    for pattern, replacement in GAMER_SLANG:
        text = pattern.sub(replacement, text)

    # ลดตัวอักษรหรือตัวเลขซ้ำกันเกิน 4 ตัว เช่น 55555555555 -> 5555
    text = re.sub(r"(.)\1{4,}", r"\1\1\1\1", text)

    # ตัดช่องว่างซ้ำซ้อน
    text = re.sub(r"\s+", " ", text).strip()

    # ตัดความยาวหากเกินกำหนด
    if len(text) > max_length:
        text = text[:max_length] + "... ข้อความยาวเกินไป"

    return text


class TwitchMultiChannelListener:
    """
    จัดการการเชื่อมต่อ Twitch IRC แบบ Single Socket เชื่อมต่อหลายช่องพร้อมกัน (Multi-Channel)
    รองรับ Anonymous Login และการ Add/Remove ช่องแบบ Real-Time
    """
    def __init__(
        self,
        channels: Set[str],
        on_message_callback: Callable[[str, str, str, Dict[str, str]], Coroutine],
        max_length: int = 250,
    ):
        self.channels: Set[str] = {c.lower().strip().lstrip("#") for c in channels if c.strip()}
        self.on_message_callback = on_message_callback
        self.max_length = max_length
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    def start(self):
        if not self.running:
            self.running = True
            self._task = asyncio.create_task(self._listen_loop())

    async def stop(self):
        self.running = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info(f"[Twitch Multi] หยุดการเชื่อมต่อทุกช่องแล้ว")

    async def add_channel(self, channel: str):
        clean_ch = channel.lower().strip().lstrip("#")
        if not clean_ch:
            return
        self.channels.add(clean_ch)
        if self._writer and not self._writer.is_closing():
            try:
                self._writer.write(f"JOIN #{clean_ch}\r\n".encode("utf-8"))
                await self._writer.drain()
                logger.info(f"[Twitch Multi] ส่งคำสั่ง JOIN #{clean_ch} สำเร็จ")
            except Exception as e:
                logger.error(f"[Twitch Multi] ล้มเหลวในการส่ง JOIN #{clean_ch}: {e}")

    async def remove_channel(self, channel: str):
        clean_ch = channel.lower().strip().lstrip("#")
        self.channels.discard(clean_ch)
        if self._writer and not self._writer.is_closing():
            try:
                self._writer.write(f"PART #{clean_ch}\r\n".encode("utf-8"))
                await self._writer.drain()
                logger.info(f"[Twitch Multi] ส่งคำสั่ง PART #{clean_ch} สำเร็จ")
            except Exception as e:
                logger.error(f"[Twitch Multi] ล้มเหลวในการส่ง PART #{clean_ch}: {e}")

    async def _listen_loop(self):
        while self.running:
            reader: Optional[asyncio.StreamReader] = None
            try:
                logger.info(f"[Twitch Multi] กำลังเชื่อมต่อ Twitch IRC สำหรับช่อง: {list(self.channels)}")
                reader, self._writer = await asyncio.open_connection("irc.chat.twitch.tv", 6667)

                anon_id = random.randint(10000, 99999)
                anon_nick = f"justinfan{anon_id}"

                self._writer.write(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
                self._writer.write(f"PASS oauth:{anon_nick}\r\n".encode("utf-8"))
                self._writer.write(f"NICK {anon_nick}\r\n".encode("utf-8"))

                # เข้าร่วมทุกช่องที่กำหนดไว้
                for ch in self.channels:
                    self._writer.write(f"JOIN #{ch}\r\n".encode("utf-8"))
                await self._writer.drain()

                logger.info(f"[Twitch Multi] เข้าร่วมห้องแชทสำเร็จ: {list(self.channels)}")

                while self.running:
                    line_bytes = await reader.readline()
                    if not line_bytes:
                        break  # Connection lost

                    line = line_bytes.decode("utf-8", errors="ignore").strip()

                    # รักษาสถานะ PING / PONG
                    if line.startswith("PING"):
                        if self._writer:
                            self._writer.write(b"PONG :tmi.twitch.tv\r\n")
                            await self._writer.drain()
                        continue

                    # ประมวลผลข้อความแชท
                    if "PRIVMSG" in line:
                        await self._handle_privmsg(line)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Twitch Multi] เกิดข้อผิดพลาดในการเชื่อมต่อ: {e}")
                if self.running:
                    await asyncio.sleep(5)  # รอ 5 วินาทีก่อนลองใหม่

    async def _handle_privmsg(self, line: str):
        try:
            tags: Dict[str, str] = {}
            raw = line
            if raw.startswith("@"):
                parts = raw[1:].split(" ", 1)
                if len(parts) == 2:
                    tag_str, rest = parts
                    for item in tag_str.split(";"):
                        if "=" in item:
                            k, v = item.split("=", 1)
                            tags[k] = v
                    raw = rest

            match = re.search(r":([^!@\s]+)!\S+\s+PRIVMSG\s+#([^\s]+)\s+:(.*)", raw)
            if match:
                user, channel, message = match.groups()
                if user.lower() in IGNORED_USERS:
                    return

                display_name = tags.get("display-name") or user
                logger.info(f"[Twitch IRC] #{channel} <{display_name}>: {message}")
                await self.on_message_callback(channel.lower(), display_name, message, tags)
            else:
                logger.debug(f"[Twitch IRC Non-matching PRIVMSG] {raw}")
        except Exception as e:
            logger.error(f"[Twitch Multi] ข้อผิดพลาดในการแยกข้อความ: {e}")
