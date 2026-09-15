"""运行期配置（全部可由环境变量覆盖）。"""

import os

# SQLite 数据库文件。容器内默认放在 /data 卷上，重启后数据保留。
DATABASE_URL = os.getenv(
    "TOKEN_DATABASE_URL", "sqlite:////data/token.db"
)

# 浏览器跨域读取/移交所需；开发态 Vite 在 5173 端口。
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if o.strip()
]
