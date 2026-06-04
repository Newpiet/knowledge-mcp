# Ignore Patterns
CLAUDE.md
.aider*
attic/
docs/

# Coding
- Use Python 3.12 features and syntax
- Follow PEP 8 style guide for Python code
- Use type hints everywhere possible
- Use list, dict, and set comprehensions when appropriate for concise and readable code.
- Prefer pathlib over os.path for file system operation
- Use explicit exception handling. Catch specific exceptions rather than using bare except clauses
- Keep functions and methods small and focused on a single task
- Use docstrings for all public modules, functions, classes, and methods
- Use dataclasses for data containers when appropriate
- Prefer composition over inheritance where possible
- Use logging for debugging and monitoring
- Use meaningful variable and method names

# Development
- Use pytest for unit testing
- Do not create tests unless requested by the user
- Use uv for dependency management

## Using uv 
- use 'uv add <dependency name>' to add dependencies
- use 'uv remove <dependency name>' to remove dependencies
- Create uv scripts for running scripts in pyproject.toml [project.scripts]
- Use hatchling as the build-system

## Running
- Ensure activate the venv before spawning a new console session: `source .venv/bin/activate`
- Use uv scripts to run something

# Using Tools (MCP)
- Use the sequential-thinking tool if the problem is complex and you need to think hard, you need to think step by step. This is mandatory.
- Use the context7 tool to look up the documentation of a library. 

# Project
- Project sepecification is in the specs directory: 
  - prd.md: project requirements
  - plan.md: development plan

# 部署经验（阿里云 ECS）

## 服务器信息
- IP: 139.196.25.63，用户: root，系统: Ubuntu 24.04 LTS，2核 3.4GB
- 项目部署目录: /opt/knowledge-mcp
- SSH 连接工具: 本地用 `expect` 模拟密码登录（无 sshpass）
- 文件传输: `expect` 包装 `scp` 完成密码认证

## Docker 构建注意事项
- **Docker Hub 无法直接访问**：需配置镜像加速，已写入 `/etc/docker/daemon.json`：
  ```json
  {"registry-mirrors": ["https://docker.m.daocloud.io", "https://hub-mirror.c.163.com", "https://mirror.baidubce.com"]}
  ```
  配置后执行 `systemctl restart docker`
- **PyPI 官方源超时**：Dockerfile 中 pip/uv 必须指定国内镜像，**不能加 `--extra-index-url https://pypi.org/simple/`**（会超时回落）：
  ```dockerfile
  RUN pip install --no-cache-dir uv -i https://mirrors.aliyun.com/pypi/simple/ \
      && uv pip install --system --no-cache -e . \
          --index-url https://mirrors.aliyun.com/pypi/simple/ \
          --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/
  ```
- **后台构建用 nohup**：`nohup docker compose build --no-cache > /tmp/docker-build.log 2>&1 &`，直接 `&` 会在 SSH 断开时被杀掉
- 构建日志实时查看：`tail -f /tmp/docker-build.log`

## .env 配置
- WEB_PORT=80（直接用 80 端口，无需 nginx 反代）
- ALLOWED_ORIGINS=http://<服务器IP>
- JWT_SECRET 用 `openssl rand -hex 32` 生成