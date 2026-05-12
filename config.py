import os
import logging
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_SEARCH_API_KEY", "")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")

BINANCE_BASE_URL = "https://api.binance.com"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

MODEL_PRO = "deepseek-v4-pro"
MODEL_FLASH = "deepseek-v4-flash"

TRADE_PAIRS_FILE = os.path.join(DATA_DIR, "trade.json")
FAVORITES_FILE = os.path.join(DATA_DIR, "favorites.json")
TRADE_HISTORY_FILE = os.path.join(DATA_DIR, "trade_history.json")
POSITIONS_FILE = os.path.join(DATA_DIR, "positions.json")
BALANCE_HISTORY_FILE = os.path.join(DATA_DIR, "balance_history.json")

TOP_HOT_PAIRS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
]

KLINE_INTERVALS = ["1h", "4h", "1d"]
KLINE_LIMITS = {"1h": 50, "4h": 50, "1d": 50}

MAX_WEB_SEARCH_ROUNDS = 1  # single round, Flash suggests queries once, we search & summarize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
