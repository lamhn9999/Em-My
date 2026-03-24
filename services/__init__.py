import os 
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent.parent / ".env")

ZALOOA_ACCESS_TOKEN = os.getenv("ZALOOA_ACCESS_TOKEN")
ZALOOA_ID = os.getenv("ZALOOA_ID")