import asyncio
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
import database
from tts_cleaner import clean_discord_message
from tts_player import TTSManager
from twitch_reader import TwitchMultiChannelListener, clean_twitch_message
from dashboard_server import DashboardServer

# ตั้งค่า Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("TTSBot")


class TTSBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None
        )
        self.tts_manager = TTSManager(self)
        self.disconnect_tasks = {}
        self.dashboard_server = DashboardServer(self, host="0.0.0.0", port=config.DASHBOARD_PORT)

    async def setup_hook(self):
        # เริ่มต้นระบบฐานข้อมูล SQLite
        try:
            database.init_db()
        except Exception as e:
            logger.error(f"ล้มเหลวในการเริ่มต้นฐานข้อมูล SQLite: {e}")

        # ซิงค์ Slash Commands กับ Discord
        logger.info("กำลังซิงค์ Slash Commands...")
        try:
            synced = await self.tree.sync()
            logger.info(f"ซิงค์ Slash Commands สำเร็จแล้ว {len(synced)} คำสั่ง")
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการซิงค์คำสั่ง: {e}")

        # เริ่มต้น Web Dashboard Server
        try:
            await self.dashboard_server.start()
        except Exception as e:
            logger.error(f"ล้มเหลวในการเริ่ม Dashboard Web Server: {e}")

    async def close(self):
        if self.dashboard_server:
            await self.dashboard_server.stop()
        await super().close()

    def ensure_twitch_listener(self, guild_id: int):
        state = self.tts_manager.get_state(guild_id)
        if state.twitch_listener is None and state.twitch_channels:
            bot_instance = self

            async def on_twitch_message(channel: str, display_name: str, message_text: str, tags: dict):
                logger.info(f"[Twitch IRC Recv] Guild {guild_id} | #{channel} <{display_name}>: {message_text}")
                target_guild = bot_instance.get_guild(guild_id)
                current_vc = target_guild.voice_client if target_guild else None
                if not current_vc or not current_vc.is_connected():
                    logger.warning(f"[Twitch Dropped] บอทไม่ได้อยู่ในห้องเสียงในเซิร์ฟเวอร์ {guild_id}")
                    return

                user_id = (tags.get("user-id") or display_name).lower()
                badges = tags.get("badges", "")
                is_broadcaster = "broadcaster" in badges
                is_mod = "moderator" in badges
                is_vip = "vip" in badges

                # Anti-Spam User Cooldown (เจ้าของช่องและ Mod ได้รับการยกเว้น)
                now = time.time()
                if not (is_broadcaster or is_mod):
                    last_time = state.user_last_seen.get(user_id, 0.0)
                    if now - last_time < state.user_cooldown_seconds:
                        return
                    state.user_last_seen[user_id] = now
                    if len(state.user_last_seen) > 1000:
                        state.user_last_seen.clear()

                # กรอง Emotes (Twitch/7TV/BTTV), สแลงเกม, ลิงก์, และ Blacklist
                emotes_tag = tags.get("emotes", "")
                cleaned = clean_twitch_message(
                    text=message_text,
                    emotes_tag=emotes_tag,
                    strip_emotes=state.strip_emotes,
                    max_length=config.MAX_TEXT_LENGTH,
                    blacklist=state.blacklist
                )
                if not cleaned:
                    logger.info(f"[Twitch Filter] ข้อความถูกกรองออก: '{message_text}'")
                    return

                badge_prefix = ""
                if is_broadcaster:
                    badge_prefix = "สตรีมเมอร์ "
                elif is_vip:
                    badge_prefix = "วีไอพี "

                channel_prefix = ""
                if state.twitch_announce_channel and len(state.twitch_channels) > 1:
                    channel_prefix = f"จากช่อง {channel}, "

                if state.twitch_read_name:
                    spoken = f"{channel_prefix}{badge_prefix}{display_name} ว่า {cleaned}"
                else:
                    spoken = f"{channel_prefix}{cleaned}"

                logger.info(f"[Twitch TTS Enqueued] {spoken}")
                await bot_instance.tts_manager.enqueue(
                    guild_id=guild_id,
                    text=spoken,
                    author_name=display_name,
                    source=f"Twitch (#{channel})"
                )

            listener = TwitchMultiChannelListener(
                channels=state.twitch_channels,
                on_message_callback=on_twitch_message,
                max_length=config.MAX_TEXT_LENGTH
            )
            state.twitch_listener = listener
            listener.start()
            logger.info(f"[Twitch Multi] สตาร์ต Listener สำเร็จสำหรับกิลด์ {guild_id} ช่อง: {state.twitch_channels}")

    async def on_ready(self):
        logger.info(f"ล็อกอินสำเร็จในชื่อ: {self.user} (ID: {self.user.id})")
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name="/join หรือ /help เพื่อเริ่มใช้งาน"
        )
        await self.change_presence(activity=activity)
        print("=" * 50)
        print(f" บอทพร้อมทำงานแล้ว: {self.user.name}")
        print(f" ชวนบอทเข้าห้องเสียงด้วยคำสั่ง: /join")
        print(f" 🌐 Web Dashboard: http://localhost:{config.DASHBOARD_PORT} (PIN: {config.DASHBOARD_PIN})")
        print("=" * 50)

        # บังคับซิงค์ Slash Commands เข้าทุกเซิร์ฟเวอร์ทันที (Instant Guild Sync ไม่ต้องรอแคช Global)
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info(f"ซิงค์คำสั่งเข้าเซิร์ฟเวอร์ '{guild.name}' สำเร็จ ({len(synced)} คำสั่ง)")
            except Exception as e:
                logger.warning(f"ซิงค์เซิร์ฟเวอร์ '{guild.name}' ไม่สำเร็จ: {e}")

        # Auto-Rejoin ห้องเสียงเดิมอัตโนมัติจากฐานข้อมูล SQLite
        reconnect_list = database.get_all_guilds_for_reconnect()
        if reconnect_list:
            logger.info(f"🔄 พบรายการกิลด์ที่ต้องเชื่อมต่อใหม่อัตโนมัติ: {len(reconnect_list)} กิลด์")
            for item in reconnect_list:
                guild_id = item["guild_id"]
                voice_ch_id = item.get("voice_channel_id")
                guild = self.get_guild(guild_id)
                if not guild or not voice_ch_id:
                    continue

                voice_channel = guild.get_channel(voice_ch_id)
                if isinstance(voice_channel, discord.VoiceChannel):
                    try:
                        if guild.voice_client and guild.voice_client.is_connected():
                            await guild.voice_client.move_to(voice_channel)
                        else:
                            await voice_channel.connect()

                        state = self.tts_manager.get_state(guild_id)
                        state.voice_channel_id = voice_ch_id
                        state.persist()
                        if state.twitch_channels:
                            self.ensure_twitch_listener(guild_id)
                        logger.info(f"⚡ [Auto-Rejoin สำเร็จ] เชื่อมต่อห้องเสียง '{voice_channel.name}' ใน '{guild.name}' อัตโนมัติแล้ว")
                    except Exception as e:
                        logger.warning(f"⚠️ [Auto-Rejoin ล้มเหลว] เซิร์ฟเวอร์ '{guild.name}': {e}")

    async def on_message(self, message: discord.Message):
        # ละเว้นข้อความจากบอทเอง หรือบอทตัวอื่น
        if message.author.bot or not message.guild:
            return

        # ละเว้นข้อความที่เป็นคำสั่ง (ขึ้นต้นด้วย !, /, $, .)
        if message.content and message.content[0] in ("!", "/", "$", "."):
            await self.process_commands(message)
            return

        state = self.tts_manager.get_state(message.guild.id)

        # ตรวจสอบว่าห้องนี้เป็นห้องที่ผูกไว้สำหรับอ่านเสียงหรือไม่
        if state.bound_channel_id and state.bound_channel_id == message.channel.id:
            voice_client: Optional[discord.VoiceClient] = message.guild.voice_client

            # ตรวจสอบว่าบอทอยู่ในห้องเสียงหรือไม่
            if voice_client and voice_client.is_connected():
                # ตรวจสอบ Blacklist
                if state.blacklist:
                    raw_lower = message.content.lower()
                    if any(banned and banned.lower() in raw_lower for banned in state.blacklist):
                        return

                # กรองและแปลงข้อความ
                cleaned = clean_discord_message(message, max_length=config.MAX_TEXT_LENGTH)
                if cleaned:
                    await self.tts_manager.enqueue(
                        guild_id=message.guild.id,
                        text=cleaned,
                        author_name=message.author.display_name
                    )
                    # เพิ่มอีโมจิแสดงว่าได้รับข้อความเข้าคิวแล้ว
                    try:
                        await message.add_reaction("🔊")
                    except Exception:
                        pass

        await self.process_commands(message)

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # ตรวจสอบเมื่อตัวบอทเปลี่ยนสถานะห้องเสียง
        if member == self.user:
            if after.channel:
                state = self.tts_manager.get_state(after.channel.guild.id)
                state.voice_channel_id = after.channel.id
                state.persist()
            elif before.channel and not after.channel:
                # ถ้าบอทถูกเตะหรือหลุดออกจากห้องเสียง
                state = self.tts_manager.get_state(before.channel.guild.id)
                state.clear_queue()
                state.voice_channel_id = None
                state.persist()
                await state.stop_twitch()
            return

        # ตรวจสอบห้องเสียงที่บอทอยู่ ว่าเหลือบอทคนเดียวหรือไม่
        guild = member.guild
        voice_client: Optional[discord.VoiceClient] = guild.voice_client

        if voice_client and voice_client.channel:
            # นับจำนวนสมาชิกที่ไม่ใช่บอทในห้องเสียง
            non_bot_members = [m for m in voice_client.channel.members if not m.bot]

            if len(non_bot_members) == 0:
                # ไม่มีคนอยู่ในห้องเสียง เริ่มนับถอยหลังตัดการเชื่อมต่อ
                if guild.id not in self.disconnect_tasks:
                    self.disconnect_tasks[guild.id] = asyncio.create_task(
                        self._auto_disconnect(guild.id, config.AUTO_DISCONNECT_SECONDS)
                    )
            else:
                # ยังมีคนอยู่ ขอยกเลิกนับถอยหลัง
                if guild.id in self.disconnect_tasks:
                    self.disconnect_tasks[guild.id].cancel()
                    del self.disconnect_tasks[guild.id]

    async def _auto_disconnect(self, guild_id: int, delay: int):
        try:
            await asyncio.sleep(delay)
            guild = self.get_guild(guild_id)
            if guild and guild.voice_client and guild.voice_client.is_connected():
                non_bot_members = [m for m in guild.voice_client.channel.members if not m.bot]
                if len(non_bot_members) == 0:
                    await guild.voice_client.disconnect()
                    state = self.tts_manager.get_state(guild_id)
                    state.clear_queue()
                    state.voice_channel_id = None
                    state.persist()
                    await state.stop_twitch()
                    logger.info(f"[Guild {guild_id}] ออกจากห้องเสียงอัตโนมัติเนื่องจากไม่มีคนอยู่")
        except asyncio.CancelledError:
            pass
        finally:
            if guild_id in self.disconnect_tasks:
                del self.disconnect_tasks[guild_id]


bot = TTSBot()


@bot.command(name="sync")
async def sync_commands(ctx: commands.Context):
    """คำสั่งด่วน !sync สำหรับซิงค์ Slash Commands เข้าเซิร์ฟเวอร์ทันที 0 วินาที"""
    if not ctx.guild:
        return
    try:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"⚡ ซิงค์คำสั่ง Slash สำเร็จแล้ว {len(synced)} คำสั่งในเซิร์ฟเวอร์นี้! (พิมพ์ `/` ดูได้เลย)")
    except Exception as e:
        await ctx.send(f"❌ เกิดข้อผิดพลาดในการซิงค์: {e}")


# ==========================================
# SLASH COMMANDS
# ==========================================

@bot.tree.command(name="join", description="เรียกบอทเข้าห้องเสียงที่คุณอยู่ และผูกห้องข้อความนี้เพื่ออ่านเสียงอัตโนมัติ")
async def join(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์เท่านั้น", ephemeral=True)
        return

    author = interaction.user
    if not isinstance(author, discord.Member) or not author.voice or not author.voice.channel:
        await interaction.response.send_message("❌ คุณต้องอยู่ในห้องเสียงก่อนจึงจะเรียกบอทได้ครับ", ephemeral=True)
        return

    target_channel = author.voice.channel
    voice_client: Optional[discord.VoiceClient] = interaction.guild.voice_client

    if voice_client:
        if voice_client.channel.id != target_channel.id:
            await voice_client.move_to(target_channel)
    else:
        await target_channel.connect()

    # ผูกห้องข้อความปัจจุบันสำหรับการอ่านอัตโนมัติ
    state = bot.tts_manager.get_state(interaction.guild.id)
    state.bound_channel_id = interaction.channel.id
    state.voice_channel_id = target_channel.id
    state.persist()
    if state.twitch_channels:
        bot.ensure_twitch_listener(interaction.guild.id)

    embed = discord.Embed(
        title="🔊 บอทพร้อมอ่านข้อความแล้ว!",
        description=(
            f"📍 **ห้องเสียง:** `{target_channel.name}`\n"
            f"💬 **ห้องข้อความ:** {interaction.channel.mention}\n\n"
            f"👉 พิมพ์ข้อความในห้องนี้ บอทจะนำไปพูดในห้องเสียงให้ทันที\n"
            f"👉 พิมพ์ `/help` เพื่อดูคำสั่งทั้งหมด"
        ),
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leave", description="ให้บอทออกจากห้องเสียงและยกเลิกการอ่านอัตโนมัติ")
async def leave(interaction: discord.Interaction):
    if not interaction.guild:
        return

    voice_client: Optional[discord.VoiceClient] = interaction.guild.voice_client
    if voice_client:
        state = bot.tts_manager.get_state(interaction.guild.id)
        state.clear_queue()
        state.bound_channel_id = None
        state.voice_channel_id = None
        state.persist()
        await state.stop_twitch()
        await voice_client.disconnect()
        await interaction.response.send_message("👋 บอทออกจากห้องเสียงและเคลียร์คิวเรียบร้อยแล้วครับ")
    else:
        await interaction.response.send_message("❌ บอทไม่ได้อยู่ในห้องเสียงครับ", ephemeral=True)


@bot.tree.command(name="say", description="ให้อ่านข้อความที่ระบุทันทีในห้องเสียง")
@app_commands.describe(text="ข้อความที่ต้องการให้บอทอ่าน")
async def say(interaction: discord.Interaction, text: str):
    if not interaction.guild:
        return

    voice_client: Optional[discord.VoiceClient] = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ บอทยังไม่ได้เข้าห้องเสียง กรุณาใช้คำสั่ง `/join` ก่อนครับ", ephemeral=True)
        return

    if not text.strip():
        await interaction.response.send_message("❌ กรุณาระบุข้อความ", ephemeral=True)
        return

    cleaned = text.strip()[:config.MAX_TEXT_LENGTH]
    await bot.tts_manager.enqueue(
        guild_id=interaction.guild.id,
        text=cleaned,
        author_name=interaction.user.display_name
    )
    await interaction.response.send_message(f"📢 กำลังต่อคิวอ่าน: *\"{cleaned}\"*", ephemeral=True)


@bot.tree.command(name="bind", description="ผูกห้องข้อความนี้หรือห้องที่ระบุ เพื่อให้อ่านเสียงอัตโนมัติ")
@app_commands.describe(channel="ห้องข้อความที่ต้องการผูก (ถ้าไม่เลือก จะใช้ห้องปัจจุบัน)")
async def bind(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not interaction.guild:
        return

    target = channel or interaction.channel
    state = bot.tts_manager.get_state(interaction.guild.id)
    state.bound_channel_id = target.id
    state.persist()

    await interaction.response.send_message(f"✅ ผูกห้องข้อความสำหรับอ่านอัตโนมัติเรียบร้อย: {target.mention}")


@bot.tree.command(name="unbind", description="ยกเลิกการอ่านข้อความอัตโนมัติในห้องนี้")
async def unbind(interaction: discord.Interaction):
    if not interaction.guild:
        return

    state = bot.tts_manager.get_state(interaction.guild.id)
    state.bound_channel_id = None
    state.persist()
    await interaction.response.send_message("🛑 ยกเลิกการอ่านข้อความอัตโนมัติแล้ว (ยังสามารถใช้ `/say` ได้ตามปกติ)")


@bot.tree.command(name="voice", description="เลือกเสียงพากย์ของบอท")
@app_commands.choices(voice=[
    app_commands.Choice(name="⚡ Google แปลภาษา (ตอบสนองไวสุด ~0.3 วินาที)", value="google-th"),
    app_commands.Choice(name="🌸 เปรมวดี (หญิงไทย - เสียงสมจริงเป็นธรรมชาติ)", value="th-TH-PremwadeeNeural"),
    app_commands.Choice(name="🧔 นิวัฒน์ (ชายไทย - เสียงทุ้มนุ่มเป็นธรรมชาติ)", value="th-TH-NiwatNeural"),
    app_commands.Choice(name="🇺🇸 Ava (หญิง - ภาษาอังกฤษ/US)", value="en-US-AvaNeural"),
    app_commands.Choice(name="🇺🇸 Guy (ชาย - ภาษาอังกฤษ/US)", value="en-US-GuyNeural"),
])
async def set_voice(interaction: discord.Interaction, voice: app_commands.Choice[str]):
    if not interaction.guild:
        return

    state = bot.tts_manager.get_state(interaction.guild.id)
    state.voice = voice.value
    state.persist()
    await interaction.response.send_message(f"🎙️ เปลี่ยนเสียงพากย์เป็น: **{voice.name}** เรียบร้อยแล้ว")


@bot.tree.command(name="speed", description="ปรับความเร็วเสียงพากย์")
@app_commands.choices(speed=[
    app_commands.Choice(name="ช้า (-20%)", value="-20%"),
    app_commands.Choice(name="ปกติ (+0%)", value="+0%"),
    app_commands.Choice(name="เร็ว (+20%)", value="+20%"),
    app_commands.Choice(name="เร็วมาก (+40%)", value="+40%"),
    app_commands.Choice(name="ติดสปีด (+60%)", value="+60%"),
])
async def set_speed(interaction: discord.Interaction, speed: app_commands.Choice[str]):
    if not interaction.guild:
        return

    state = bot.tts_manager.get_state(interaction.guild.id)
    state.rate = speed.value
    state.persist()
    await interaction.response.send_message(f"⚡ ปรับความเร็วเสียงเป็น: **{speed.name}** เรียบร้อยแล้ว")


@bot.tree.command(name="name_toggle", description="เปิด/ปิด การอ่านชื่อคนพิมพ์ก่อนอ่านข้อความ")
async def name_toggle(interaction: discord.Interaction):
    if not interaction.guild:
        return

    state = bot.tts_manager.get_state(interaction.guild.id)
    state.read_name = not state.read_name
    state.persist()
    status_text = "เปิด (จะอ่าน 'ชื่อ พูดว่า: ...')" if state.read_name else "ปิด (อ่านเฉพาะข้อความ)"
    await interaction.response.send_message(f"👤 การอ่านชื่อคนพิมพ์: **{status_text}**")


@bot.tree.command(name="skip", description="ข้ามข้อความที่บอทกำลังอ่านอยู่ในปัจจุบัน")
async def skip_audio(interaction: discord.Interaction):
    if not interaction.guild:
        return

    skipped = bot.tts_manager.skip(interaction.guild.id)
    if skipped:
        await interaction.response.send_message("⏭️ ข้ามข้อความปัจจุบันเรียบร้อยแล้ว")
    else:
        await interaction.response.send_message("ℹ️ ไม่มีเสียงที่กำลังเล่นอยู่ครับ", ephemeral=True)


@bot.tree.command(name="clear", description="ล้างคิวข้อความที่รออ่านทั้งหมด")
async def clear_queue(interaction: discord.Interaction):
    if not interaction.guild:
        return

    bot.tts_manager.clear(interaction.guild.id)
    await interaction.response.send_message("🗑️ ล้างคิวข้อความและหยุดเสียงทั้งหมดเรียบร้อยแล้ว")


@bot.tree.command(name="status", description="ดูสถานะปัจจุบันของบอทในเซิร์ฟเวอร์นี้")
async def show_status(interaction: discord.Interaction):
    if not interaction.guild:
        return

    state = bot.tts_manager.get_state(interaction.guild.id)
    voice_client: Optional[discord.VoiceClient] = interaction.guild.voice_client

    vc_name = voice_client.channel.name if voice_client and voice_client.channel else "ไม่ได้เชื่อมต่อ"
    bound_channel = f"<#{state.bound_channel_id}>" if state.bound_channel_id else "ไม่ได้ผูก"

    # หาชื่อเสียงที่เข้าใจง่าย
    voice_display = next((k for k, v in config.AVAILABLE_VOICES.items() if v == state.voice), state.voice)

    embed = discord.Embed(
        title="📊 สถานะระบบบอท TTS",
        color=discord.Color.blue()
    )
    embed.add_field(name="🔊 ห้องเสียงที่อยู่", value=f"`{vc_name}`", inline=True)
    embed.add_field(name="💬 ห้องข้อความที่ผูก", value=bound_channel, inline=True)
    embed.add_field(name="🎙️ เสียงปัจจุบัน", value=voice_display, inline=False)
    embed.add_field(name="⚡ ความเร็วเสียง", value=state.rate, inline=True)
    embed.add_field(name="👤 อ่านชื่อคนพิมพ์", value="เปิด" if state.read_name else "ปิด", inline=True)
    twitch_channels = ", ".join([f"`#{c}`" for c in sorted(state.twitch_channels)]) if state.twitch_channels else "ไม่ได้เปิดใช้งาน"
    embed.add_field(name="🟣 ช่อง Twitch ที่อ่าน", value=twitch_channels, inline=False)
    embed.add_field(name="🚫 ตัด Emotes", value="เปิด" if state.strip_emotes else "ปิด", inline=True)
    embed.add_field(name="🌐 สลับภาษา TH/EN", value="เปิด" if state.auto_language else "ปิด", inline=True)
    embed.add_field(name="⏳ คูลดาวน์สแปม", value=f"{state.user_cooldown_seconds:.1f}s", inline=True)
    embed.add_field(name="⏳ คิวที่รออ่าน", value=f"{state.queue.qsize()} ข้อความ", inline=True)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="ดูคู่มือวิธีใช้งานบอทอ่านข้อความเสียง")
async def show_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 คู่มือการใช้งานบอทอ่านเสียง (Discord TTS Bot)",
        description="บอทอ่านข้อความภาษาไทยด้วยเสียงพรีเมียมจาก Microsoft Edge TTS\n",
        color=discord.Color.gold()
    )
    embed.add_field(
        name="🚀 วิธีเริ่มต้นใช้งานง่ายๆ",
        value=(
            "1. เข้าไปอยู่ในห้องเสียงที่คุณต้องการ\n"
            "2. พิมพ์คำสั่ง `/join` ในห้องแชท\n"
            "3. พิมพ์ข้อความอะไรก็ได้ในห้องแชทนั้น บอทจะอ่านออกเสียงให้ทันที!"
        ),
        inline=False
    )
    embed.add_field(
        name="🛠️ คำสั่งหลัก",
        value=(
            "• `/join` - เรียกบอทเข้าห้องเสียง + ผูกห้องแชทให้อ่านอัตโนมัติ\n"
            "• `/leave` - สั่งให้บอทออกจากห้องเสียง\n"
            "• `/say [ข้อความ]` - สั่งให้อ่านข้อความทันที (ใช้ได้ตลอด)\n"
            "• `/bind` - เปลี่ยนหรือผูกห้องแชทสำหรับอ่านอัตโนมัติ\n"
            "• `/unbind` - ปิดการอ่านอัตโนมัติในห้องนี้"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ การตั้งค่าและจัดการเสียง",
        value=(
            "• `/voice` - เปลี่ยนเสียงพากย์ (เปรมวดี หญิง / นิวัฒน์ ชาย / ภาษาอังกฤษ)\n"
            "• `/speed` - ปรับความเร็วเสียง (ช้า / ปกติ / เร็ว / เร็วมาก)\n"
            "• `/name_toggle` - สลับเปิด/ปิดการอ่านชื่อคนพิมพ์\n"
            "• `/skip` - ข้ามข้อความปัจจุบัน\n"
            "• `/clear` - ล้างคิวข้อความที่รออ่านทั้งหมด\n"
            "• `/status` - ดูสถานะปัจจุบันของบอท"
        ),
        inline=False
    )
    embed.add_field(
        name="🟣 คำสั่งเชื่อมต่อ Twitch (อ่านแชทหลายช่องพร้อมกัน)",
        value=(
            "• `/twitch_add [channel]` - เพิ่มช่อง Twitch (เลือก DaLines, JxoExe, PaulJaw หรือพิมพ์ชื่อช่องเอง)\n"
            "• `/twitch_remove [channel]` - ถอดช่อง Twitch ออก\n"
            "• `/twitch_list` - ดูรายชื่อช่อง Twitch ที่กำลังฟังทั้งหมด\n"
            "• `/twitch_clear` (หรือ `/twitch_stop`) - หยุดอ่าน Twitch ทุกช่อง\n"
            "• `/twitch_settings` - ปรับการประกาศชื่อช่องและชื่อคนพิมพ์"
        ),
        inline=False
    )
    embed.add_field(
        name="🛡️ การกรองเสียงและป้องกันสแปม",
        value=(
            "• `/tts_filter` - ตั้งค่าตัด Emotes / สลับภาษา TH/EN / คูลดาวน์กันสแปม\n"
            "• `/blacklist_add [คำ]` - เพิ่มคำต้องห้ามที่ไม่ให้ออกเสียง\n"
            "• `/blacklist_remove [คำ]` - ถอดคำออกจาก Blacklist\n"
            "• `/blacklist_list` - ดูรายชื่อคำที่บล็อกไว้ทั้งหมด"
        ),
        inline=False
    )
    embed.add_field(
        name="🌐 ควบคุมผ่านเว็บ (Web Dashboard)",
        value="• `/dashboard` - ดูลิงก์และรหัส PIN สำหรับเข้าหน้าเว็บควบคุมบอทสด",
        inline=False
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="dashboard", description="แสดงลิงก์สำหรับเข้าสู่หน้า Web Dashboard เพื่อควบคุมบอทผ่านเบราว์เซอร์")
async def show_dashboard(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌐 Web Dashboard ควบคุมบอทสด",
        description=(
            f"คุณสามารถเปิดหน้าควบคุมบอทได้ที่:\n"
            f"👉 **`http://<IP_ของเซิร์ฟเวอร์>:{config.DASHBOARD_PORT}`**\n\n"
            f"🔑 **รหัสผ่านเข้าใช้งาน (Admin PIN):** `{config.DASHBOARD_PIN}`\n\n"
            f"✨ **ฟังก์ชันบนหน้าเว็บ:**\n"
            f"• สลับเปิด/ปิดแชท Twitch ในคลิกเดียว\n"
            f"• พิมพ์ข้อความแล้วสั่งให้บอทพูดใน Discord ทันที (Web-to-Voice)\n"
            f"• ดูคิวเสียงสด พร้อมปุ่ม Skip และ Clear\n"
            f"• ปรับเสียงพากย์, ความเร็ว, และตัวกรอง\n"
            f"• Live Activity Feed แสดงข้อความล่าสุด"
        ),
        color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================
# TWITCH CHAT INTEGRATION (MULTI-CHANNEL)
# ==========================================

PRESET_TWITCH_CHANNELS = ["DaLines", "JxoExe", "PaulJaw"]


async def twitch_channel_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    choices = []
    current_clean = current.strip().replace("https://www.twitch.tv/", "").replace("https://twitch.tv/", "").lstrip("@#")

    if current_clean and current_clean.lower() not in [c.lower() for c in PRESET_TWITCH_CHANNELS]:
        choices.append(app_commands.Choice(name=f"🔍 เพิ่มช่อง: {current_clean}", value=current_clean))

    for ch in PRESET_TWITCH_CHANNELS:
        if not current_clean or current_clean.lower() in ch.lower():
            choices.append(app_commands.Choice(name=f"🎮 {ch}", value=ch))

    return choices[:25]


async def active_twitch_channels_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    if not interaction.guild:
        return []
    state = bot.tts_manager.get_state(interaction.guild.id)
    return [
        app_commands.Choice(name=f"❌ #{ch}", value=ch)
        for ch in sorted(state.twitch_channels)
        if not current or current.lower() in ch.lower()
    ][:25]


def _ensure_twitch_listener(guild_id: int):
    bot.ensure_twitch_listener(guild_id)


@bot.tree.command(name="twitch_add", description="เพิ่มช่อง Twitch เพื่อดึงแชทมาอ่านในห้องเสียง (เปิดได้หลายช่องพร้อมกัน)")
@app_commands.describe(channel="เลือกหรือพิมพ์ชื่อช่อง Twitch ที่ต้องการเพิ่ม (เช่น DaLines, JxoExe, PaulJaw)")
@app_commands.autocomplete(channel=twitch_channel_autocomplete)
async def twitch_add(interaction: discord.Interaction, channel: str):
    if not interaction.guild:
        await interaction.response.send_message("❌ คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์เท่านั้น", ephemeral=True)
        return

    voice_client: Optional[discord.VoiceClient] = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        author = interaction.user
        if isinstance(author, discord.Member) and author.voice and author.voice.channel:
            await author.voice.channel.connect()
            voice_client = interaction.guild.voice_client
        else:
            await interaction.response.send_message("❌ บอทยังไม่ได้อยู่ในห้องเสียง กรุณาเข้าห้องเสียงแล้วใช้ `/join` ก่อนครับ", ephemeral=True)
            return

    clean_ch = channel.strip().replace("https://www.twitch.tv/", "").replace("https://twitch.tv/", "").lstrip("@#").lower()
    if not clean_ch:
        await interaction.response.send_message("❌ กรุณาระบุชื่อช่อง Twitch", ephemeral=True)
        return

    state = bot.tts_manager.get_state(interaction.guild.id)
    if clean_ch in state.twitch_channels:
        await interaction.response.send_message(f"ℹ️ ช่อง `#{clean_ch}` กำลังเชื่อมต่ออยู่แล้วครับ", ephemeral=True)
        return

    await state.add_twitch_channel(clean_ch)
    _ensure_twitch_listener(interaction.guild.id)

    channels_str = ", ".join([f"`#{c}`" for c in sorted(state.twitch_channels)])
    embed = discord.Embed(
        title="🟣 เพิ่มช่อง Twitch สำเร็จ!",
        description=(
            f"✅ เพิ่มช่อง: **`#{clean_ch}`**\n"
            f"🔊 อ่านในห้องเสียง: `{voice_client.channel.name}`\n"
            f"📺 ช่องทั้งหมดที่กำลังอ่าน ({len(state.twitch_channels)} ช่อง): {channels_str}\n\n"
            f"💡 *หากเชื่อมต่อหลายช่อง บอทจะประกาศชื่อช่องนำหน้าให้อัตโนมัติ*"
        ),
        color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="twitch_remove", description="ถอดช่อง Twitch ที่กำลังฟังอยู่ออก")
@app_commands.describe(channel="เลือกช่อง Twitch ที่ต้องการถอดออก")
@app_commands.autocomplete(channel=active_twitch_channels_autocomplete)
async def twitch_remove(interaction: discord.Interaction, channel: str):
    if not interaction.guild:
        return

    clean_ch = channel.strip().replace("https://www.twitch.tv/", "").replace("https://twitch.tv/", "").lstrip("@#").lower()
    state = bot.tts_manager.get_state(interaction.guild.id)

    if clean_ch not in state.twitch_channels:
        await interaction.response.send_message(f"❌ ไม่พบช่อง `#{clean_ch}` ในรายการที่กำลังฟังอยู่ครับ", ephemeral=True)
        return

    await state.remove_twitch_channel(clean_ch)
    if state.twitch_channels:
        channels_str = ", ".join([f"`#{c}`" for c in sorted(state.twitch_channels)])
        await interaction.response.send_message(f"🗑️ ถอดช่อง `#{clean_ch}` เรียบร้อยแล้ว (ยังเหลือ: {channels_str})")
    else:
        await interaction.response.send_message(f"🛑 ถอดช่อง `#{clean_ch}` เรียบร้อยแล้ว (ไม่มีช่องอื่นค้างอยู่ บอทหยุดฟัง Twitch ทั้งหมดแล้ว)")


@bot.tree.command(name="twitch_list", description="ดูรายชื่อช่อง Twitch ทั้งหมดที่กำลังเชื่อมต่ออยู่ในขณะนี้")
async def twitch_list(interaction: discord.Interaction):
    if not interaction.guild:
        return

    state = bot.tts_manager.get_state(interaction.guild.id)
    if not state.twitch_channels:
        await interaction.response.send_message("ℹ️ ขณะนี้ไม่มีช่อง Twitch ที่กำลังเชื่อมต่ออยู่ (ใช้คำสั่ง `/twitch_add` เพื่อเริ่มฟัง)", ephemeral=True)
        return

    channels_str = "\n".join([f"• `#{c}`" for c in sorted(state.twitch_channels)])
    embed = discord.Embed(
        title="📺 รายชื่อช่อง Twitch ที่กำลังอ่านแชท",
        color=discord.Color.purple()
    )
    embed.add_field(name=f"ช่องที่เชื่อมต่อ ({len(state.twitch_channels)} ช่อง)", value=channels_str, inline=False)
    embed.add_field(name="📢 ประกาศชื่อช่อง", value="เปิด" if state.twitch_announce_channel else "ปิด", inline=True)
    embed.add_field(name="👤 อ่านชื่อคนพิมพ์", value="เปิด" if state.twitch_read_name else "ปิด", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="twitch_clear", description="หยุดอ่านแชทและล้างช่อง Twitch ทั้งหมดในคราวเดียว")
async def twitch_clear(interaction: discord.Interaction):
    if not interaction.guild:
        return

    state = bot.tts_manager.get_state(interaction.guild.id)
    if not state.twitch_channels:
        await interaction.response.send_message("ℹ️ ขณะนี้ไม่ได้เปิดอ่านแชทจาก Twitch อยู่ครับ", ephemeral=True)
        return

    count = len(state.twitch_channels)
    await state.stop_twitch()
    await interaction.response.send_message(f"🛑 ปิดการอ่านแชทและล้างช่อง Twitch ทั้งหมด ({count} ช่อง) เรียบร้อยแล้วครับ")


@bot.tree.command(name="twitch_listen", description="เริ่มอ่านแชทจากช่อง Twitch (คำสั่งด่วน)")
@app_commands.describe(
    channel="เลือกช่อง (DaLines, JxoExe, PaulJaw) หรือพิมพ์ชื่อช่อง Twitch ที่ต้องการ",
    read_name="ให้อ่านชื่อคนพิมพ์ด้วยหรือไม่ (True = อ่านชื่อ, False = อ่านเฉพาะข้อความ)"
)
@app_commands.autocomplete(channel=twitch_channel_autocomplete)
async def twitch_listen(interaction: discord.Interaction, channel: str, read_name: bool = True):
    state = bot.tts_manager.get_state(interaction.guild.id)
    state.twitch_read_name = read_name
    await twitch_add.callback(interaction, channel)


@bot.tree.command(name="twitch_settings", description="ตั้งค่าระบบอ่านแชท Twitch")
@app_commands.describe(
    announce_channel="พูดชื่อช่องนำหน้าเมื่อฟังหลายช่องพร้อมกันหรือไม่",
    read_name="อ่านชื่อคนพิมพ์ด้วยหรือไม่"
)
async def twitch_settings(
    interaction: discord.Interaction,
    announce_channel: Optional[bool] = None,
    read_name: Optional[bool] = None
):
    if not interaction.guild:
        return

    state = bot.tts_manager.get_state(interaction.guild.id)
    if announce_channel is not None:
        state.twitch_announce_channel = announce_channel
    if read_name is not None:
        state.twitch_read_name = read_name
    state.persist()

    embed = discord.Embed(
        title="⚙️ ตั้งค่าระบบ Twitch TTS เรียบร้อยแล้ว",
        color=discord.Color.purple()
    )
    embed.add_field(name="📢 ประกาศชื่อช่องเมื่อฟังหลายช่อง", value="เปิด" if state.twitch_announce_channel else "ปิด", inline=True)
    embed.add_field(name="👤 อ่านชื่อคนพิมพ์", value="เปิด" if state.twitch_read_name else "ปิด", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="twitch_stop", description="หยุดการอ่านแชทจาก Twitch ทั้งหมด (เหมือน twitch_clear)")
async def twitch_stop(interaction: discord.Interaction):
    await twitch_clear.callback(interaction)


# ==========================================
# FILTER & BLACKLIST COMMANDS
# ==========================================

@bot.tree.command(name="tts_filter", description="ตั้งค่าตัวกรองเสียง (ตัดอีโมท, สลับภาษา TH/EN อัตโนมัติ, คูลดาวน์กันสแปม)")
@app_commands.describe(
    strip_emotes="ตัดชื่อ Emote ทิ้ง (ไม่ให้อ่าน KEKW, OMEGALUL, Pog)",
    auto_language="สลับเสียงเป็นสำเนียงอังกฤษ US อัตโนมัติเมื่อเป็นภาษาอังกฤษล้วน",
    cooldown_seconds="ระยะห่างระหว่างข้อความต่อคน (วินาที ป้องกันคนเดียวสแปม)"
)
async def tts_filter(
    interaction: discord.Interaction,
    strip_emotes: Optional[bool] = None,
    auto_language: Optional[bool] = None,
    cooldown_seconds: Optional[float] = None
):
    if not interaction.guild:
        return

    state = bot.tts_manager.get_state(interaction.guild.id)
    if strip_emotes is not None:
        state.strip_emotes = strip_emotes
    if auto_language is not None:
        state.auto_language = auto_language
    if cooldown_seconds is not None:
        state.user_cooldown_seconds = max(0.0, cooldown_seconds)
    state.persist()

    embed = discord.Embed(
        title="🛡️ การตั้งค่าระบบกรองเสียง TTS",
        color=discord.Color.green()
    )
    embed.add_field(name="🚫 ตัด Emotes (Twitch & 7TV)", value="เปิด" if state.strip_emotes else "ปิด", inline=True)
    embed.add_field(name="🌐 สลับภาษา TH/EN อัตโนมัติ", value="เปิด" if state.auto_language else "ปิด", inline=True)
    embed.add_field(name="⏳ คูลดาวน์กันสแปมต่อคน", value=f"{state.user_cooldown_seconds:.1f} วินาที", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="blacklist_add", description="เพิ่มคำต้องห้ามที่ไม่ต้องการให้บอทอ่านออกเสียง")
@app_commands.describe(word="คำหรือวลีที่ต้องการบล็อก")
async def blacklist_add(interaction: discord.Interaction, word: str):
    if not interaction.guild:
        return

    clean_word = word.strip().lower()
    if not clean_word:
        await interaction.response.send_message("❌ กรุณาระบุคำที่ต้องการบล็อก", ephemeral=True)
        return

    state = bot.tts_manager.get_state(interaction.guild.id)
    state.blacklist.add(clean_word)
    database.add_guild_blacklist_word(interaction.guild.id, clean_word)
    await interaction.response.send_message(f"🚫 เพิ่มคำว่า **\"{clean_word}\"** ลงใน Blacklist เรียบร้อยแล้ว (บอทจะข้ามข้อความที่มีคำนี้)")


@bot.tree.command(name="blacklist_remove", description="ถอดคำออกจาก Blacklist")
@app_commands.describe(word="คำที่ต้องการปลดบล็อก")
async def blacklist_remove(interaction: discord.Interaction, word: str):
    if not interaction.guild:
        return

    clean_word = word.strip().lower()
    state = bot.tts_manager.get_state(interaction.guild.id)
    if clean_word not in state.blacklist:
        await interaction.response.send_message(f"ℹ️ ไม่พบคำว่า \"{clean_word}\" ใน Blacklist ครับ", ephemeral=True)
        return

    state.blacklist.discard(clean_word)
    database.remove_guild_blacklist_word(interaction.guild.id, clean_word)
    await interaction.response.send_message(f"✅ ปลดบล็อกคำว่า **\"{clean_word}\"** เรียบร้อยแล้ว")
    await interaction.response.send_message(f"✅ ปลดบล็อกคำว่า **\"{clean_word}\"** เรียบร้อยแล้ว")


@bot.tree.command(name="blacklist_list", description="ดูรายชื่อคำต้องห้ามใน Blacklist ทั้งหมด")
async def blacklist_list(interaction: discord.Interaction):
    if not interaction.guild:
        return

    state = bot.tts_manager.get_state(interaction.guild.id)
    if not state.blacklist:
        await interaction.response.send_message("ℹ️ ขณะนี้ไม่มีคำต้องห้ามใน Blacklist ครับ", ephemeral=True)
        return

    words_str = ", ".join([f"`{w}`" for w in sorted(state.blacklist)])
    embed = discord.Embed(
        title="🚫 รายชื่อคำใน Blacklist",
        description=words_str,
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed)


# ==========================================
# MAIN EXECUTION
# ==========================================

def main():
    if not config.DISCORD_TOKEN or config.DISCORD_TOKEN == "your_bot_token_here":
        print("\n" + "!" * 60)
        print("❌ เกิดข้อผิดพลาด: ยังไม่ได้ระบุ DISCORD_TOKEN ในไฟล์ .env")
        print("กรุณาเปิดไฟล์ .env แล้วใส่ Bot Token ของคุณก่อนเริ่มใช้งานครับ")
        print("วิธีทำดูได้จากไฟล์ README.md")
        print("!" * 60 + "\n")
        return

    logger.info("กำลังเชื่อมต่อ Discord...")
    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
