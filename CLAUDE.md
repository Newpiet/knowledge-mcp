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

# 调试经验教训

## 修 Bug 前必须先定位来源
- **禁止凭猜测连续尝试**：每次修改前必须有明确的"问题在这里"的证据
- 做法：先加 `logger.info(f"关键变量: {xxx}")` 打印出来，确认来源再动手
- 反例：`history_turns` 问题，猜是 Cherry Studio 注入 → 错；猜是函数签名 → 改崩了；最终靠日志才发现来自 `kb_config`

## 关键路径用 info 不用 debug
- 调试关键逻辑时用 `logger.info`，不要用 `logger.debug`
- debug 级别默认不输出，出问题时看不到有没有触发，浪费排查时间

## 用第三方框架前先查约束
- 改函数签名前先查框架是否支持，例如 FastMCP 明确不支持 `**kwargs` 工具函数
- 查法：搜官方文档或直接看报错信息（`ValueError: Functions with **kwargs are not supported as tools`）

## 版本兼容问题用自适应方案
- 不要硬编码"这个版本支持/不支持"，用反射动态检查
- 正例：`inspect.signature(QueryParam.__init__).parameters.keys()` 自动适配任意版本
- 反例：手动维护一个"不支持字段"黑名单，换版本就失效

## docker cp 后必须清 .pyc 再重启
- Python 有字节码缓存，docker cp 更新 .py 文件后如果 .pyc 没清，可能仍跑旧代码
- 标准流程：`docker cp file container:/path && docker exec container find /app -name '*.pyc' -delete && docker restart container`

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