import os
import certifi
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from datetime import timezone
import logging
logger = logging.getLogger(__name__)

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_DB", "RecruiterBot")

if not MONGODB_URI:
    logger.error("MONGODB_URI environment variable not set")
    raise RuntimeError("MONGODB_URI is not set")

# ✅ Create client with explicit TLS + CA bundle
client = AsyncIOMotorClient(
    MONGODB_URI,
    tz_aware=True,
    tzinfo=timezone.utc,
    tls=True,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=30_000,
    connectTimeoutMS=20_000,
    socketTimeoutMS=20_000,
    retryWrites=True,
    appname="RecruiterBot",
)

db = client[MONGODB_DB]

def get_db():
    return db

async def ping_db():
    try:
        logger.info("Pinging MongoDB...")
        await client.admin.command("ping")
        logger.info("MongoDB connection successful")
    except Exception as e:
        logger.exception("MongoDB ping failed")
        raise