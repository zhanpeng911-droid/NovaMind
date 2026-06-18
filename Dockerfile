# NovaMind Docker 部署
# 多阶段构建，减小镜像体积

FROM python:3.11-slim AS base

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY novamind/ novamind/
COPY entry/ entry/
COPY pyproject.toml setup.py ./

# 安装项目本身
RUN pip install --no-cache-dir -e .

# 创建工作空间目录
RUN mkdir -p workspace/memory workspace/personas workspace/scripts workspace/office/skills logs

# 暴露的端口（未来Web API扩展用）
EXPOSE 8080

# 默认启动命令
ENTRYPOINT ["novamind"]
CMD ["run"]
