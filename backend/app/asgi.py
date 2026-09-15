"""uvicorn 入口：app.asgi:app。"""

from .config import DATABASE_URL
from .database import Database
from .main import create_app

database = Database(DATABASE_URL)
app = create_app(database)
