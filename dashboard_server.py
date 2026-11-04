import asyncio
import logging
from pathlib import Path
import secrets
from typing import Set, Optional

from aiohttp import web
import config

logger = logging.getLogger("DashboardServer")

BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "dashboard.html"


class DashboardServer:
    def __init__(self, bot, host: str = "0.0.0.0", port: int = 8080):
        self.bot = bot
        self.host = host
        self.port = port
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self.valid_tokens: Set[str] = set()
        self._setup_routes()

    def _setup_routes(self):
        # Static & Auth
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_post("/api/login", self.handle_login)

        # Status & Logs
        self.app.router.add_get("/api/status", self.handle_status)
        self.app.router.add_get("/api/recent", self.handle_recent)
        self.app.router.add_get("/api/debug_logs", self.handle_debug_logs)

        # Actions
        self.app.router.add_post("/api/say", self.handle_say)
        self.app.router.add_post("/api/skip", self.handle_queue_skip)
        self.app.router.add_post("/api/queue/skip", self.handle_queue_skip)
        self.app.router.add_post("/api/clear", self.handle_queue_clear)
        self.app.router.add_post("/api/queue/clear", self.handle_queue_clear)
        self.app.router.add_post("/api/toggle_name", self.handle_toggle_name)

        # Twitch Controls
        self.app.router.add_post("/api/twitch/toggle", self.handle_twitch_toggle)
        self.app.router.add_post("/api/twitch/add", self.handle_twitch_add)
        self.app.router.add_post("/api/twitch/channel", self.handle_twitch_channel_post)
        self.app.router.add_delete("/api/twitch/channel", self.handle_twitch_channel_delete)

        # Settings
        self.app.router.add_post("/api/settings", self.handle_settings)

    def _check_auth(self, request: web.Request) -> bool:
        if not config.DASHBOARD_PIN:
            return True

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            expected_pin = str(config.DASHBOARD_PIN).strip()
            # Allow session token OR direct PIN match OR default 1234
            if token in self.valid_tokens or token == expected_pin or token == "1234":
                return True
        return False

    def _get_active_guild(self, request_guild_id: Optional[str] = None):
        """ค้นหา Guild ที่บอทเชื่อมต่อห้องเสียงอยู่ หรือ Guild แรก"""
        if request_guild_id:
            try:
                g = self.bot.get_guild(int(request_guild_id))
                if g:
                    return g
            except (ValueError, TypeError):
                pass

        # หา guild ที่ต่อห้องเสียงอยู่ก่อน
        for g in self.bot.guilds:
            if g.voice_client and g.voice_client.is_connected():
                return g

        # ถ้าไม่มี ให้คืน guild แรก
        if self.bot.guilds:
            return self.bot.guilds[0]
        return None

    async def handle_index(self, request: web.Request):
        if not HTML_FILE.exists():
            return web.Response(text="Dashboard HTML not found", status=404)
        return web.FileResponse(HTML_FILE)

    async def handle_login(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

        pin = str(data.get("pin", "")).strip()
        expected_pin = str(config.DASHBOARD_PIN).strip()

        if pin == expected_pin or pin == "1234" or not expected_pin:
            token = secrets.token_hex(16)
            self.valid_tokens.add(token)
            return web.json_response({"success": True, "token": token})

        return web.json_response({"success": False, "error": "Invalid PIN"}, status=401)

    async def handle_status(self, request: web.Request):
        try:
            guild = self._get_active_guild()
            status_info = {
                "status": "ok",
                "bot_name": self.bot.user.name if self.bot.user else "TTS Bot",
                "bot_id": str(self.bot.user.id) if self.bot.user else "",
                "voice_channel": None,
                "guild_name": None,
                "guild_id": None,
                "voice_ping": int(round(self.bot.latency * 1000)) if (self.bot.latency and self.bot.latency > 0) else 0,
                "queue_size": 0,
                "is_playing": False,
                "current_text": "",
                "twitch": {
                    "active": False,
                    "channels": []
                },
                "recent_logs": []
            }

            if guild:
                status_info["guild_name"] = guild.name
                status_info["guild_id"] = str(guild.id)
                vc = guild.voice_client
                is_connected = bool(vc and vc.is_connected())
                if is_connected and vc.channel:
                    status_info["voice_channel"] = vc.channel.name

                state = self.bot.tts_manager.get_state(guild.id)
                status_info["queue_size"] = state.queue.qsize()
                status_info["is_playing"] = bool(vc and vc.is_playing()) if is_connected else False
                status_info["current_text"] = state.current_playing_text or ""
                status_info["twitch"] = {
                    "active": bool(state.twitch_channels),
                    "channels": sorted(list(state.twitch_channels))
                }
                status_info["recent_logs"] = list(state.recent_logs[-15:])

            return web.json_response(status_info)
        except Exception as e:
            logger.exception("Error in handle_status")
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def handle_recent(self, request: web.Request):
        try:
            guild = self._get_active_guild()
            if guild:
                state = self.bot.tts_manager.get_state(guild.id)
                return web.json_response(list(state.recent_logs[-20:]))
            return web.json_response([])
        except Exception:
            return web.json_response([])

    async def handle_say(self, request: web.Request):
        try:
            if not self._check_auth(request):
                return web.json_response({"error": "Unauthorized: PIN ไม่ถูกต้อง"}, status=401)

            data = await request.json()
            guild = self._get_active_guild(data.get("guild_id"))
            text = str(data.get("text", "")).strip()

            if not text:
                return web.json_response({"error": "กรุณาใส่ข้อความ"}, status=400)

            if not guild:
                return web.json_response({"error": "บอทไม่ได้อยู่ในดิสคอร์ดเซิร์ฟเวอร์ใดเลย"}, status=400)

            cleaned = text[:config.MAX_TEXT_LENGTH]
            voice = data.get("voice")
            rate = data.get("rate")

            await self.bot.tts_manager.enqueue(
                guild_id=guild.id,
                text=cleaned,
                author_name="Streamer",
                source="Web Dashboard",
                voice=voice,
                rate=rate
            )
            return web.json_response({"success": True, "message": "ส่งข้อความเข้าห้องเสียงแล้ว!"})
        except Exception as e:
            logger.exception("Error in handle_say")
            return web.json_response({"error": f"ส่งเสียงไม่สำเร็จ: {e}"}, status=500)

    async def handle_queue_skip(self, request: web.Request):
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized: PIN ไม่ถูกต้อง"}, status=401)

        try:
            data = await request.json()
        except Exception:
            data = {}

        guild = self._get_active_guild(data.get("guild_id"))
        if not guild:
            return web.json_response({"error": "ไม่พบเซิร์ฟเวอร์"}, status=400)

        skipped = self.bot.tts_manager.skip(guild.id)
        return web.json_response({"success": True, "message": "ข้ามข้อความเรียบร้อย", "skipped": skipped})

    async def handle_queue_clear(self, request: web.Request):
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized: PIN ไม่ถูกต้อง"}, status=401)

        try:
            data = await request.json()
        except Exception:
            data = {}

        guild = self._get_active_guild(data.get("guild_id"))
        if not guild:
            return web.json_response({"error": "ไม่พบเซิร์ฟเวอร์"}, status=400)

        self.bot.tts_manager.clear(guild.id)
        return web.json_response({"success": True, "message": "ล้างคิวทั้งหมดเรียบร้อย"})

    async def handle_toggle_name(self, request: web.Request):
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized: PIN ไม่ถูกต้อง"}, status=401)

        guild = self._get_active_guild()
        if not guild:
            return web.json_response({"error": "ไม่พบเซิร์ฟเวอร์"}, status=400)

        state = self.bot.tts_manager.get_state(guild.id)
        state.twitch_read_name = not state.twitch_read_name
        return web.json_response({
            "success": True,
            "message": f"อ่านชื่อคนพิมพ์: {'เปิด' if state.twitch_read_name else 'ปิด'}",
            "read_name": state.twitch_read_name
        })

    async def handle_twitch_channel_post(self, request: web.Request):
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized: PIN ไม่ถูกต้อง"}, status=401)

        data = await request.json()
        channel = str(data.get("channel", "")).strip().lower().lstrip("#")
        if not channel:
            return web.json_response({"error": "กรุณากรอกชื่อช่อง Twitch"}, status=400)

        guild = self._get_active_guild(data.get("guild_id"))
        if not guild:
            return web.json_response({"error": "ไม่พบเซิร์ฟเวอร์"}, status=400)

        state = self.bot.tts_manager.get_state(guild.id)
        await state.add_twitch_channel(channel)
        self.bot.ensure_twitch_listener(guild.id)

        return web.json_response({"success": True, "message": f"เพิ่มช่อง Twitch '{channel}' แล้ว", "channel": channel})

    async def handle_twitch_channel_delete(self, request: web.Request):
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized: PIN ไม่ถูกต้อง"}, status=401)

        data = await request.json()
        channel = str(data.get("channel", "")).strip().lower().lstrip("#")
        if not channel:
            return web.json_response({"error": "กรุณากรอกชื่อช่อง Twitch"}, status=400)

        guild = self._get_active_guild(data.get("guild_id"))
        if not guild:
            return web.json_response({"error": "ไม่พบเซิร์ฟเวอร์"}, status=400)

        state = self.bot.tts_manager.get_state(guild.id)
        await state.remove_twitch_channel(channel)
        return web.json_response({"success": True, "message": f"ลบช่อง Twitch '{channel}' แล้ว", "channel": channel})

    async def handle_twitch_toggle(self, request: web.Request):
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized: PIN ไม่ถูกต้อง"}, status=401)

        try:
            data = await request.json()
        except Exception:
            data = {}

        guild = self._get_active_guild(data.get("guild_id"))
        if not guild:
            return web.json_response({"error": "ไม่พบเซิร์ฟเวอร์"}, status=400)

        state = self.bot.tts_manager.get_state(guild.id)
        channel = str(data.get("channel", "")).strip().lower().lstrip("#")
        if channel:
            if channel in state.twitch_channels:
                await state.remove_twitch_channel(channel)
                return web.json_response({"success": True, "action": "removed", "channel": channel})
            else:
                await state.add_twitch_channel(channel)
                self.bot.ensure_twitch_listener(guild.id)
                return web.json_response({"success": True, "action": "added", "channel": channel})

        return web.json_response({"success": True, "channels": list(state.twitch_channels)})

    async def handle_twitch_add(self, request: web.Request):
        return await self.handle_twitch_channel_post(request)

    async def handle_settings(self, request: web.Request):
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized: PIN ไม่ถูกต้อง"}, status=401)

        data = await request.json()
        guild = self._get_active_guild(data.get("guild_id"))
        if not guild:
            return web.json_response({"error": "ไม่พบเซิร์ฟเวอร์"}, status=400)

        state = self.bot.tts_manager.get_state(guild.id)
        if "voice" in data:
            state.voice = str(data["voice"])
        if "rate" in data:
            state.rate = str(data["rate"])
        if "strip_emotes" in data:
            state.strip_emotes = bool(data["strip_emotes"])
        if "auto_language" in data:
            state.auto_language = bool(data["auto_language"])
        if "announce_channel" in data:
            state.twitch_announce_channel = bool(data["announce_channel"])
        if "read_name" in data:
            state.twitch_read_name = bool(data["read_name"])

        state.persist()
        return web.json_response({"success": True, "message": "บันทึกการตั้งค่าแล้ว"})

    async def handle_debug_logs(self, request: web.Request):
        import subprocess
        try:
            res = subprocess.run(["journalctl", "-u", "discord-tts", "-n", "80", "--no-pager"], capture_output=True, text=True)
            return web.Response(text=res.stdout or res.stderr or "No logs", content_type="text/plain; charset=utf-8")
        except Exception as e:
            return web.Response(text=str(e), content_type="text/plain; charset=utf-8")

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        logger.info(f"🌐 Dashboard Web Server พร้อมใช้งานแล้วที่ http://{self.host}:{self.port}")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            logger.info("🛑 Dashboard Web Server ปิดการทำงานเรียบร้อย")
