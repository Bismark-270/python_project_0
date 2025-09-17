# db.py
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import certifi

# --- Atlas SRV URI (NO credentials embedded) ---
SRV_URI = "mongodb+srv://cluster0.01r3qnt.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

# --- Atlas DB user credentials (plain, no URL-encoding needed here) ---
USERNAME = "gracleand76"
PASSWORD = "0500868021Yaw"   # keep '@' as-is

# Create client
client = MongoClient(
    SRV_URI,
    username=USERNAME,
    password=PASSWORD,
    authSource="admin",         # Atlas users typically authenticate against 'admin'
    server_api=ServerApi("1"),
    tls=True,
    tlsCAFile=certifi.where(),  # helps on Windows for CA trust
    connectTimeoutMS=20000,
    socketTimeoutMS=20000,
)

# Optional: quick ping
try:
    client.admin.command("ping")
    print("✅ Connected to MongoDB successfully.")
except Exception as e:
    print("❌ MongoDB connection error:", e)

# Select database
db = client["events"]

# Convenience collection handle(s)
users_collection = db["users"]
