import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import imageio_ffmpeg

# โหลด .env (รองรับทั้งตอนรันปกติ และตอนแปลงเป็น .exe)
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

# ตั้งค่า Discord Bot
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# ตั้งค่าเสียงเริ่มต้น
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "th-TH-PremwadeeNeural")
DEFAULT_RATE = os.getenv("DEFAULT_RATE", "+20%")
MAX_TEXT_LENGTH = int(os.getenv("MAX_TEXT_LENGTH", "250"))
AUTO_DISCONNECT_SECONDS = int(os.getenv("AUTO_DISCONNECT_SECONDS", "60"))

# รายชื่อเสียงที่มีให้เลือก
AVAILABLE_VOICES = {
    "Google แปลภาษา (เร็วสุดทันใจ ~0.3 วินาที)": "google-th",
    "เปรมวดี (หญิงไทย - เสียงสมจริงเป็นธรรมชาติ)": "th-TH-PremwadeeNeural",
    "นิวัฒน์ (ชายไทย - เสียงสมจริงเป็นธรรมชาติ)": "th-TH-NiwatNeural",
    "Ava (หญิงอังกฤษ/US)": "en-US-AvaNeural",
    "Guy (ชายอังกฤษ/US)": "en-US-GuyNeural",
}

# ความเร็วเสียงที่มีให้เลือก
AVAILABLE_RATES = {
    "ช้า (-20%)": "-20%",
    "ปกติ (+0%)": "+0%",
    "เร็ว (+20%)": "+20%",
    "เร็วมาก (+40%)": "+40%",
    "ติดสปีด (+60%)": "+60%",
}

# การตั้งค่า Web Dashboard
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
DASHBOARD_PIN = os.getenv("DASHBOARD_PIN", "1234").strip()

# โฟลเดอร์เก็บไฟล์เสียงชั่วคราว
TEMP_AUDIO_DIR = BASE_DIR / "temp"
TEMP_AUDIO_DIR.mkdir(exist_ok=True)

# หาพาธ ffmpeg อัตโนมัติ (เช็คไฟล์ในโฟลเดอร์โปรแกรมก่อน)
local_ffmpeg = BASE_DIR / "ffmpeg.exe"
if local_ffmpeg.exists():
    FFMPEG_EXECUTABLE = str(local_ffmpeg)
else:
    try:
        FFMPEG_EXECUTABLE = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG_EXECUTABLE = "ffmpeg"
