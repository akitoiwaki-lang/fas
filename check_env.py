import os
from dotenv import load_dotenv

load_dotenv()

print("DATABASE_URL:", bool(os.getenv("DATABASE_URL")))
print("SUPABASE_URL:", bool(os.getenv("SUPABASE_URL")))
print(
    "SECRET KEY:",
    bool(os.getenv("SECRET_KEY") or os.getenv("SUPABASE_SECRET_KEY"))
)