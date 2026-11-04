import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Set, List

import discord
import edge_tts
from gtts import gTTS

import config
import database

logger = logging.getLogger(__name__)

THAI_CHAR_PATTERN = re.compile(r"[\u0e00-\u0e7f]")
ENGLISH_WORD_PATTERN = re.compile(r"[a-zA-Z]{2,}")


def get_voice_for_text(text: str, current_voice: str, auto_language: bool = True) -> str:
    """
    ตรวจจับภาษาเพื่อเลือกเสียงสังเคราะห์ที่เหมาะสม:
    - หากมีภาษาไทย หรือปิด auto_language จะใช้เสียงปัจจุบัน
    - หากเป็นประโยคภาษาอังกฤษล้วน จะสลับเป็นเสียงเจ้าของภาษา US ชั่วคราว
    """
    if not auto_language:
        return current_voice

    # หากมีภาษาไทย ให้ใช้เสียงปัจจุบัน (เสียงไทย)
    if THAI_CHAR_PATTERN.search(text):
        return current_voice

    # หากไม่มีภาษาไทยเลย และมีคำภาษาอังกฤษ ให้สลับเป็นสำเนียงเจ้าของภาษา US
    if ENGLISH_WORD_PATTERN.search(text):
        if "Niwat" in current_voice or "Guy" in current_voice:
            return "en-US-GuyNeural"
        elif "Premwadee" in current_voice or "Ava" in current_voice:
            return "en-US-AvaNeural"
        elif current_voice.startswith("google-"):
            return "google-en"
        return "en-US-AvaNeural"

    return current_voice


@dataclass
class TTSItem:
    text: str
    author_name: str
    guild_id: int
    source: str = "Discord"
    voice: Optional[str] = None
    rate: Optional[str] = None


class GuildTTSState:
    def __init__(self, guild_id: int, bot: discord.Client):
        self.guild_id = guild_id
        self.bot = bot
        self.queue: asyncio.Queue[TTSItem] = asyncio.Queue()
        self.bound_channel_id: Optional[int] = None
        self.voice_channel_id: Optional[int] = None
        self.voice: str = config.DEFAULT_VOICE
        self.rate: str = config.DEFAULT_RATE
        self.read_name: bool = False
        self.worker_task: Optional[asyncio.Task] = None
        self.current_audio_file: Optional[Path] = None
        self.current_playing_text: Optional[str] = None
        self.recent_logs: List[Dict[str, str]] = []
        self.twitch_listener = None
        self.twitch_channels: Set[str] = set()
        self.twitch_announce_channel: bool = True
        self.twitch_read_name: bool = True
        self.strip_emotes: bool = True
        self.auto_language: bool = True
        self.user_cooldown_seconds: float = 3.0
        self.blacklist: Set[str] = set()
        self.user_last_seen: Dict[str, float] = {}
        self.auto_reconnect: bool = True

        # โหลดการตั้งค่าจากฐานข้อมูล SQLite
        self.load_from_db()

    def load_from_db(self):
        saved = database.load_guild_settings(self.guild_id)
        if saved:
            self.bound_channel_id = saved.get("bound_channel_id")
            self.voice_channel_id = saved.get("voice_channel_id")
            self.voice = saved.get("voice") or config.DEFAULT_VOICE
            self.rate = saved.get("rate") or config.DEFAULT_RATE
            self.read_name = bool(saved.get("read_name", 0))
            self.strip_emotes = bool(saved.get("strip_emotes", 1))
            self.auto_language = bool(saved.get("auto_language", 1))
            self.user_cooldown_seconds = float(saved.get("user_cooldown") or 3.0)
            self.twitch_announce_channel = bool(saved.get("twitch_announce_channel", 1))
            self.twitch_read_name = bool(saved.get("twitch_read_name", 1))
            self.auto_reconnect = bool(saved.get("auto_reconnect", 1))

        self.twitch_channels = database.get_guild_twitch_channels(self.guild_id)
        self.blacklist = database.get_guild_blacklist(self.guild_id)

    def persist(self):
        """บันทึกสถานะและการตั้งค่าปัจจุบันลง SQLite"""
        database.save_guild_settings(
            self.guild_id,
            bound_channel_id=self.bound_channel_id,
            voice_channel_id=self.voice_channel_id,
            voice=self.voice,
            rate=self.rate,
            read_name=1 if self.read_name else 0,
            strip_emotes=1 if self.strip_emotes else 0,
            auto_language=1 if self.auto_language else 0,
            user_cooldown=self.user_cooldown_seconds,
            twitch_announce_channel=1 if self.twitch_announce_channel else 0,
            twitch_read_name=1 if self.twitch_read_name else 0,
            auto_reconnect=1 if self.auto_reconnect else 0
        )

    @property
    def is_playing(self) -> bool:
        guild = self.bot.get_guild(self.guild_id)
        if guild and guild.voice_client:
            return bool(guild.voice_client.is_playing())
        return bool(self.current_playing_text)

    async def stop_twitch(self):
        if self.twitch_listener:
            try:
                await self.twitch_listener.stop()
            except Exception as e:
                logger.error(f"[Guild {self.guild_id}] เกิดข้อผิดพลาดในการหยุด Twitch: {e}")
            self.twitch_listener = None
        self.twitch_channels.clear()
        database.clear_guild_twitch_channels(self.guild_id)

    async def add_twitch_channel(self, channel: str):
        clean_ch = channel.lower().strip().lstrip("#")
        if not clean_ch:
            return
        self.twitch_channels.add(clean_ch)
        database.add_guild_twitch_channel(self.guild_id, clean_ch)
        if self.twitch_listener:
            await self.twitch_listener.add_channel(clean_ch)

    async def remove_twitch_channel(self, channel: str):
        clean_ch = channel.lower().strip().lstrip("#")
        self.twitch_channels.discard(clean_ch)
        database.remove_guild_twitch_channel(self.guild_id, clean_ch)
        if self.twitch_listener:
            await self.twitch_listener.remove_channel(clean_ch)
        if len(self.twitch_channels) == 0:
            await self.stop_twitch()

    def start_worker(self):
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self._worker_loop())

    def stop_worker(self):
        if self.worker_task and not self.worker_task.done():
            self.worker_task.cancel()
            self.worker_task = None

    def clear_queue(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except (asyncio.QueueEmpty, ValueError):
                break

    async def _worker_loop(self):
        while True:
            try:
                item = await self.queue.get()
            except asyncio.CancelledError:
                break

            guild = self.bot.get_guild(self.guild_id)
            if not guild:
                self.queue.task_done()
                continue

            voice_client: Optional[discord.VoiceClient] = guild.voice_client
            if not voice_client or not voice_client.is_connected():
                # บอทไม่ได้อยู่ในห้องเสียง ให้ข้ามไป
                self.queue.task_done()
                continue

            # Anti-flood: หากคิวสะสมเกิน 15 ข้อความ ให้ตัดข้อความเก่าออกเพื่อไล่ให้ทันแชทสด
            if self.queue.qsize() > 15:
                dropped = 0
                while self.queue.qsize() > 8:
                    try:
                        self.queue.get_nowait()
                        self.queue.task_done()
                        dropped += 1
                    except Exception:
                        break
                if dropped:
                    logger.warning(f"[Guild {self.guild_id}] ข้ามข้อความแชทเก่า {dropped} ข้อความ เพื่อเร่งให้ทันสตรีมสด")

            # ประกอบข้อความที่จะอ่าน
            spoken_text = item.text
            if self.read_name and item.author_name:
                spoken_text = f"{item.author_name} พูดว่า {spoken_text}"

            # สร้างชื่อไฟล์เฉพาะ
            audio_id = uuid.uuid4().hex[:8]
            audio_path = config.TEMP_AUDIO_DIR / f"tts_{self.guild_id}_{audio_id}.mp3"
            self.current_audio_file = audio_path

            # ปรับสปีดอัตโนมัติเมื่อมีคิวรออ่านเยอะ (Dynamic Speed Boost)
            q_size = self.queue.qsize()
            effective_rate = item.rate or self.rate
            if q_size >= 8:
                effective_rate = "+40%"
            elif q_size >= 4:
                effective_rate = "+25%"

            # ตรวจสอบภาษาเพื่อเลือกเสียงสังเคราะห์ที่เหมาะสม (Auto Language Switch)
            selected_voice = get_voice_for_text(
                spoken_text,
                current_voice=item.voice or self.voice,
                auto_language=self.auto_language
            )

            try:
                # สังเคราะห์เสียง
                if selected_voice.startswith("google-"):
                    lang = selected_voice.split("-")[1]
                    def generate_gtts():
                        tts = gTTS(text=spoken_text, lang=lang)
                        tts.save(str(audio_path))
                    await asyncio.to_thread(generate_gtts)
                else:
                    # สังเคราะห์เสียงด้วย Microsoft Edge TTS
                    communicate = edge_tts.Communicate(
                        text=spoken_text,
                        voice=selected_voice,
                        rate=effective_rate
                    )
                    await communicate.save(str(audio_path))

                if not voice_client or not voice_client.is_connected():
                    continue

                # รอให้เสียงเดิมหยุดเล่นก่อน (ถ้ามี)
                while voice_client.is_playing() or voice_client.is_paused():
                    await asyncio.sleep(0.1)

                # บันทึกประวัติล่าสุดสำหรับ Dashboard
                self.current_playing_text = spoken_text
                self.recent_logs.append({
                    "time": time.strftime("%H:%M:%S"),
                    "author": item.author_name or ("Web" if item.source == "Web" else "Chat"),
                    "text": spoken_text,
                    "source": item.source
                })
                if len(self.recent_logs) > 30:
                    self.recent_logs.pop(0)

                # เล่นเสียงผ่าน Discord Voice
                audio_source = discord.FFmpegPCMAudio(
                    str(audio_path),
                    executable=config.FFMPEG_EXECUTABLE
                )

                voice_client.play(audio_source)

                # รอจนกว่าเสียงจะเล่นจบ
                while voice_client.is_playing() or voice_client.is_paused():
                    await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"[Guild {self.guild_id}] เกิดข้อผิดพลาดในการเล่น TTS: {e}")
            finally:
                self.current_playing_text = None
                # ลบไฟล์เสียงชั่วคราว
                if audio_path.exists():
                    try:
                        audio_path.unlink()
                    except Exception:
                        pass
                self.current_audio_file = None
                self.queue.task_done()


class TTSManager:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.states: Dict[int, GuildTTSState] = {}

    def get_state(self, guild_id: int) -> GuildTTSState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildTTSState(guild_id, self.bot)
        return self.states[guild_id]

    async def enqueue(self, guild_id: int, text: str, author_name: str = "", source: str = "Discord", voice: Optional[str] = None, rate: Optional[str] = None):
        state = self.get_state(guild_id)
        state.start_worker()
        await state.queue.put(TTSItem(
            text=text,
            author_name=author_name,
            guild_id=guild_id,
            source=source,
            voice=voice,
            rate=rate
        ))

    def skip(self, guild_id: int) -> bool:
        guild = self.bot.get_guild(guild_id)
        if guild and guild.voice_client and guild.voice_client.is_playing():
            guild.voice_client.stop()
            return True
        return False

    def clear(self, guild_id: int):
        state = self.get_state(guild_id)
        state.clear_queue()
        guild = self.bot.get_guild(guild_id)
        if guild and guild.voice_client and guild.voice_client.is_playing():
            guild.voice_client.stop()
