# 验收脚本只依赖 Python 标准库，用 slim 镜像即可。
FROM python:3.11-slim

WORKDIR /verify
COPY verify.py ./

# 一次性运行，退出码即验收结论（0 通过 / 1 失败）。
ENTRYPOINT ["python", "/verify/verify.py"]
