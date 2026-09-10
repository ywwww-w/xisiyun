# 录音转写服务 API — 开发 Tickets

> 对应 spec.md v1.0。每个 ticket 一个会话完成、一个独立 commit。严格按依赖顺序从上往下做，禁止跳票。
>
> 本文件只描述「要做什么/怎么验」，不写实现代码。

---

## Ticket 1：项目骨架 + 可启动 Health 端点

### 目标
从零搭建可运行的 FastAPI 空工程，`uvicorn app.main:app --port 8000` 启动后访问 `/health` 返回 `{"status":"ok"}`。搭目录结构、装依赖、配环境变量模板。

### 背景
来自 **[spec §3 P0-1 项目骨架与基础设施](file:///d:/code/xisiyun/spec.md#L124-L144)**。所有后续功能的地基。对应 spec §7 R1 的第 1 个 commit。

### 修改范围
- 新建：`requirements.txt`、`.env.example`、`.gitignore`、`app/__init__.py`、`app/main.py`、`app/config.py`
- 新建空目录占位（`__init__.py` 或 `.gitkeep`）：`app/api/v1/`、`app/services/`、`app/utils/`、`tests/`、`uploads/`、`logs/`、`migrations/versions/`

### 任务
1. 写 `requirements.txt`（版本钉死大版本号，避免将来漂移）：
   ```
   fastapi>=0.110,<0.116
   uvicorn[standard]>=0.27,<0.31
   sqlalchemy[asyncio]>=2.0.29,<2.1
   asyncmy>=0.2.8,<0.3
   alembic>=1.13,<1.14
   pydantic-settings>=2.2,<3
   httpx>=0.27,<1
   python-multipart>=0.0.9,<1
   python-dotenv>=1.0,<2
   # 测试
   pytest>=8,<9
   pytest-asyncio>=0.23,<1
   pytest-httpx>=0.28,<1
   ```
2. 写 `.env.example`（按 spec §七的全部参数，空字符串占位敏感项）：
   ```
   MYSQL_HOST=127.0.0.1
   MYSQL_PORT=3306
   MYSQL_USER=root
   MYSQL_PASSWORD=
   MYSQL_DATABASE=xisiyun_asr
   DEEPSEEK_API_KEY=
   DEEPSEEK_BASE_URL=https://api.deepseek.com
   DEEPSEEK_MODEL=deepseek-chat
   DEEPSEEK_TIMEOUT_SECONDS=30
   MAX_CONCURRENT_TASKS=3
   MAX_UPLOAD_SIZE_MB=50
   STORAGE_UPLOAD_DIR=./uploads
   PIPELINE_MAX_AUTO_RETRIES=3
   ```
3. 写 `.gitignore`：`__pycache__/`、`*.pyc`、`.env`、`uploads/*`（保留 `.gitkeep`）、`logs/*`（保留 `.gitkeep`）、`.pytest_cache/`、`*.egg-info/`、`htmlcov/`、`.coverage`、`.venv/`
4. 写 `app/config.py`：`pydantic_settings.BaseSettings` 子类，model_config 读 `.env`；所有 `.env` 字段一一对应；类型转换（`MAX_CONCURRENT_TASKS: int = 3`、`STORAGE_UPLOAD_DIR: Path = Path("./uploads")` 等）
5. 写 `app/main.py`：创建 FastAPI(title="Xisiyun ASR Service")；挂载一个 `GET /health` → return `{"status":"ok", "timestamp": datetime.now().isoformat()}`；lifespan 先只做 `yield`（后面 T7/T9 再塞 startup hook）

### 验收标准
1. `pip install -r requirements.txt` 执行无报错（允许 Python 3.10.11）。
2. `cp .env.example .env`（随便填个不存在的 MySQL 密码都没关系，这个 ticket 还不连 DB）后 `uvicorn app.main:app --port 8000` 启动无异常退出。
3. 另开终端 `curl http://localhost:8000/health` 返回 HTTP 200，body 是合法 JSON 且含 `status=="ok"`。
4. `python -c "from app.config import Settings; s = Settings(); print(s.MAX_CONCURRENT_TASKS, type(s.MAX_CONCURRENT_TASKS))"` 输出 `3 <class 'int'>`（证明 pydantic 自动类型转换生效）。
5. 所有计划中的目录（`app/api/v1/`、`app/services/`、`app/utils/`、`tests/`、`uploads/`、`logs/`、`migrations/versions/`）都存在，import 不会报 ModuleNotFound。

### 测试要求
此 ticket **不写 pytest 用例**。Health 端点用 curl 手动验即可；pytest 框架在 T12 搭建。

### 依赖
无。

### 不包含
1. 不连 MySQL（不写 database.py/engine，留到 T2）。
2. 不写业务路由（`/v1/*` 留到 T6/T10/T11）。
3. 不写 ORM 模型（留到 T3）。
4. 不初始化 Alembic（留到 T3 配好 DB 连接再 init）。
5. 不做日志（留到 T2 utils/logger.py）。

---

## Ticket 2：基础设施 — 统一错误层 + DB 连接基类 + 日志配置

### 目标
写好三个独立的底层模块，所有后续 ticket 都依赖它们：① 统一错误响应（AppBaseException + FastAPI handler）；② 异步 SQLAlchemy 数据库连接（engine + async_sessionmaker + get_session Depends）；③ 带 task_id 字段的结构化日志配置（控制台 + logs/app.log 轮转）。

### 背景
来自 **[spec §3 P0-1 收尾](file:///d:/code/xisiyun/spec.md#L124-L144)** + **[spec §3 P0-7 工程规范](file:///d:/code/xisiyun/spec.md#L239-L249)**。对应 spec §7 R9（错误结构不统一扣分）。

### 修改范围
- 新建：`app/utils/__init__.py`、`app/utils/errors.py`、`app/utils/logger.py`
- 新建：`app/database.py`
- 修改：`app/main.py`（注册 exception_handler + 日志初始化调用）

### 任务
1. **errors.py**：
   - 定义 `AppBaseException(Exception)`：属性 `http_status: int`、`code: str`、`message: str`、`details: dict | None = None`。
   - 继承出子类：`NotFoundException(code="NOT_FOUND", http=404)`、`ConflictException(code="CONFLICT", http=409)`、`BadRequestException(code="BAD_REQUEST", http=400)`、`UnsupportedMediaTypeException(code="UNSUPPORTED_MEDIA_TYPE", http=415)`、`PayloadTooLargeException(code="PAYLOAD_TOO_LARGE", http=413)`、`PipelineStateException(code="PIPELINE_STATE_ERROR", http=409)`、`LLMCallException(code="LLM_CALL_ERROR", http=502)`、`LLMResponseFormatException(code="LLM_RESPONSE_FORMAT_ERROR", http=502)`。
   - 每个子类都可以 `raise NotFoundException("Recording", recording_id)` 内部拼 message。
2. **main.py** 注册 4 个 handler：
   - `@app.exception_handler(AppBaseException)` → 返回 `JSONResponse(status_code=e.http_status, content={"error":{"code":e.code,"message":e.message,"details":e.details}})`
   - `@app.exception_handler(RequestValidationError)`（FastAPI 参数 422）→ 转成统一结构：`{error:{code:"VALIDATION_ERROR", message:"Request validation failed", details: <原始 errors 列表>}}`，HTTP 422。
   - `@app.exception_handler(HTTPException)`（万一有人直接 raise HTTPException，兜底）→ 包装成 `{error:{code:"HTTP_XXX", message:e.detail}}`，status 取 e.status_code。
   - `@app.exception_handler(Exception)`（未捕获异常兜底）→ **响应体只返回** `{error:{code:"INTERNAL_SERVER_ERROR", message:"Internal server error"}}`（HTTP 500，不泄露堆栈），同时 `logger.exception(...)` 把完整堆栈打日志文件里。
3. **database.py**：
   - 从 `app.config import Settings` 拿到 settings。
   - 拼 SQLAlchemy 异步 URL：`f"mysql+asyncmy://{user}:{password}@{host}:{port}/{db}?charset=utf8mb4"`（密码为空时要去掉 `:`，不然 asyncmy 连接报错）。
   - `engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=10, echo=False)`。
   - `AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)`。
   - 写 Base = declarative_base()（T3 的 models 要 import 这个 Base）。
   - 写 FastAPI Depends `async def get_session() -> AsyncIterator[AsyncSession]`：yield session；异常时 rollback，finally 时 close。
4. **logger.py**：
   - 标准库 logging 配置。不要用 loguru（符合 spec §6 不造轮子）。
   - 日志格式：`[%(asctime)s] %(levelname)-8s [%(name)s:%(lineno)d] [task_id=%(task_id)s] %(message)s`；其中 `task_id` 用 `logging.Filter` 注入，默认值 `-`，业务代码里能通过 `logger.info("...", extra={"task_id": task_id})` 塞。
   - 两个 handler：① StreamHandler（stdout，level INFO）；② `RotatingFileHandler("logs/app.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")`（level DEBUG，轮转 5×10MB）。
   - 暴露 `get_logger(name: str) -> logging.Logger` 函数，root level=INFO。
5. **main.py lifespan** 里调用 `logger = get_logger("app.main"); logger.info("Service starting...")`，保证启动时日志格式先验证。

### 验收标准
1. 手动在 `main.py` 临时加一个 `GET /error/notfound` → `raise NotFoundException("Recording", "123")` → curl 返回 **HTTP 404**，body == `{"error":{"code":"NOT_FOUND","message":"Recording '123' not found","details":null}}`（结构一字不差）。
2. 临时加 `GET /error/validation` → 故意写一个带 `Field(ge=1)` 但传 0 的接口 → 返回 HTTP 422，顶级结构和其他错误一致（不能是 FastAPI 默认的 `{"detail":[...]}`）。
3. 临时加 `GET /error/crash` → `1/0` → 返回 HTTP 500，body message 是 "Internal server error"（不泄露 ZeroDivisionError），但打开 `logs/app.log` 能看到完整 Traceback。
4. `python -c "from app.database import engine, AsyncSessionLocal; import asyncio; async def f(): async with engine.begin() as c: pass; asyncio.run(f())"` —— 如果 `.env` 里 MySQL 密码对，能连上成功/密码错就抛 Access denied（不管哪种只要不是 import 或语法错误就算通过，连接正确性 T3 正式验）。
5. `python -c "from app.utils.logger import get_logger; l = get_logger('test'); l.info('hello %s', 'world', extra={'task_id':'abc'});"` → 控制台输出行里包含 `[task_id=abc]`；`logs/app.log` 文件里相同行的 task_id 也是 abc。

### 测试要求
本 ticket **不写 pytest**。错误场景用 3 个临时接口手动 curl（验完后删掉临时接口，只留注册的 handler 代码）。

### 依赖
Ticket 1（Settings 类、app/main.py FastAPI 实例、logs/ uploads/ 目录）。

### 不包含
1. 不写 ORM 模型（T3）。
2. 不 init Alembic（T3）。
3. 不写任何业务路由（T6/T10/T11）。
4. 不启动任何后台任务协程（T7）。

---

## Ticket 3：ORM 模型 + Alembic 初始化 + 迁移脚本

### 目标
完成两张核心表的 SQLAlchemy ORM 定义、Alembic 配置，然后跑 `alembic revision --autogenerate` 生成第一张迁移脚本，再 `alembic upgrade head` 成功建表。

### 背景
来自 **[spec §3 P0-2 数据库表结构](file:///d:/code/xisiyun/spec.md#L146-L159)** + **[spec §4.4 数据结构设计](file:///d:/code/xisiyun/spec.md#L425-L496)**。对应 spec §7 R10（外键 CASCADE 漏写会导致 DELETE 报错）。

### 修改范围
- 新建：`app/models.py`
- 新建：`alembic.ini`、`migrations/env.py`、`migrations/script.py.mako`、`migrations/README`（`alembic init migrations` 自动生成，然后手改 asyncmy 适配）
- 新建：`migrations/versions/0001_initial_tables_*.py`（autogenerate 生成后手改一点）

### 任务
1. **models.py**（严格按 spec §4.4 字段、类型、约束**逐条对齐**，每一条注释写清楚对应 spec 哪张表的哪个字段）：
   - 先定义 `TaskStatus = enum.Enum("TaskStatus", ["pending","transcribing","summarizing","done","failed"])` 或用 SQLAlchemy `Enum(TaskStatusEnum, values_callable=lambda x:[e.value for e in x])` 让 DB 里存字符串。
   - `Recording` 表：id(UUID str36 PK) / original_filename / file_ext / file_size_bytes(BigInteger) / **file_hash(String64, unique=True)** / storage_path / transcript(Text) / summary_json(JSON) / **last_status(Enum TaskStatus, index=True)** / created_at(DateTime(timezone=True), server_default=func.now(), index) / updated_at(..., onupdate=func.now())。
   - `Task` 表：id(UUID PK) / **recording_id(String36, ForeignKey("recordings.id", ondelete="CASCADE"), index=True)**（**重点：ondelete="CASCADE" 绝对不能丢，spec R10**） / status(Enum TaskStatus, index=True) / current_stage_retry_count(SmallInteger, default=0) / total_retry_count(Integer, default=0) / error_message(Text) / created_at / updated_at。
2. **Alembic 初始化**：
   - 执行 `alembic init -t async migrations`（-t async 生成异步模板）。
   - 改 `alembic.ini` 的 `sqlalchemy.url` → **注释掉默认值**，说明「从 migrations/env.py 动态读 settings」（避免把密码写 ini）。
   - 改 `migrations/env.py`：① 顶部 `from app.config import Settings; from app.database import Base; from app import models`（import models 让 metadata 被注册）；② `target_metadata = Base.metadata`；③ 异步 `run_migrations_offline/online` 函数里用 `Settings().database_url` 动态拼 URL，不要硬编码。
3. **生成 + 应用迁移**：
   - `alembic revision --autogenerate -m "initial tables: recordings, tasks"` → 生成 `migrations/versions/xxxx_initial_tables.py`。
   - 打开生成的脚本**人工检查** 4 件事：① `recordings.file_hash` 是否有 UNIQUE CONSTRAINT；② `tasks.recording_id` ForeignKey 是否真的是 `ondelete="CASCADE"`（autogenerate 有时候识别不出来，要手改）；③ `idx_tasks_status`、`idx_recordings_last_status`、`idx_recordings_created_at` 三个索引是否存在；④ MySQL 字符集是否 utf8mb4（不行就在 upgrade() 最开头手动加 `op.execute("ALTER DATABASE CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")`）。
   - `.env` 里填对本机 MySQL 账号密码，先手动 `mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS xisiyun_asr CHARACTER SET utf8mb4;"` 建好库。
   - `alembic upgrade head` → 无报错。
   - 验证：`mysql -u root -p xisiyun_asr -e "DESCRIBE recordings; DESCRIBE tasks; SHOW INDEX FROM tasks;"` → 打印结果与 spec §4.4 对比。
4. 写一个 `alembic downgrade base` → 表删除成功 → 再 `alembic upgrade head` → 表回来，证明回滚脚本正确。

### 验收标准
1. `python -c "from app.models import Recording, Task; r = Recording(id='a'*36); print([c.name for c in Recording.__table__.columns])"` → 输出列名数与 spec §4.4.1 完全相等（含 11 个字段）。
2. `alembic check` 输出 "No new upgrade operations detected."，证明 ORM 模型和 DB 表结构 100% 一致。
3. MySQL 命令行 `SHOW CREATE TABLE tasks` 输出中 `CONSTRAINT ... FOREIGN KEY ... REFERENCES recordings (id) ON DELETE CASCADE` **字符串存在**（不接受 ON DELETE RESTRICT / NO ACTION，spec R10）。
4. MySQL 命令行 `SHOW INDEX FROM recordings` 结果里 Key_name 含 `uk_recordings_file_hash`（UNIQUE）、`idx_recordings_last_status`、`idx_recordings_created_at`；`SHOW INDEX FROM tasks` 有 `idx_tasks_status`、`idx_tasks_recording_id`。
5. `alembic downgrade base && alembic upgrade head` 连跑两次无报错，idempotent。

### 测试要求
本 ticket **不写 pytest**。用 3. 和 4. 的 mysql CLI + alembic check 直接验。

### 依赖
Ticket 2（database.py 的 Base + Settings.database_url 能拼对）。

### 不包含
1. 不写任何 Services（T4 Storage / T8 LLM / T9 Pipeline）。
2. 不写任何 API 路由。
3. 不写 Schemas（T5 写 Pydantic）。
4. 不塞任何测试数据（DB 空表即可，T12 测试用 SQLite 内存库）。

---

## Ticket 4：Storage 服务层 — 文件保存 + 删除 + 流式 MD5

### 目标
写 `app/services/storage.py` 三个纯函数（不依赖 DB，只碰磁盘 IO）：`compute_file_md5_streaming(file_like, chunk_size=1MB) -> str`、`save_uploaded_file(file_like: UploadFile, recording_id: str, ext: str) -> Tuple[Path, int]`（返回保存后的绝对路径和实际字节数）、`delete_file_if_exists(storage_path_or_relative: str | Path) -> None`（永远不抛 FileNotFound）。

### 背景
来自 **[spec §3 P0-3 上传接口](file:///d:/code/xisiyun/spec.md#L161-L183)** 存储部分实现 + **[spec §4.5 边界 R6](file:///d:/code/xisiyun/spec.md#L546-L554)**（50MB 文件一次性读爆内存）。

### 修改范围
- 新建：`app/services/__init__.py`、`app/services/storage.py`
- 不修改任何已有文件

### 任务
1. `compute_file_md5_streaming(file_obj, chunk_size=1024*1024)`：
   - 入参是任意 `AsyncGenerator[bytes]` 可读对象（FastAPI UploadFile 的 file 属性）或同步文件对象都行（设计成 async，内部 `await read(chunk_size)`）。
   - 循环 `while chunk := await file_obj.read(chunk_size): md5.update(chunk)`；读完 `return md5.hexdigest().lower()`。
   - **读取过程同时要 reset 文件指针到 0**（读完 MD5 后 save 时可以重新读；但如果是 spooled tempfile 就 seek(0)，注意 await 的写法）。
2. `save_uploaded_file(upload_file: UploadFile, recording_id: str, ext: str) -> Tuple[Path, int]`：
   - 从 `config.settings.STORAGE_UPLOAD_DIR` 拿目录，`settings.storage_upload_dir.mkdir(parents=True, exist_ok=True)` 保证目录存在。
   - ext 必须先 `.lower()` 再判断 ∈ {wav,mp3,m4a,aac}（**在函数里做一次，双保险**；上传接口 P0-3 还会在外层再抛 415，但 storage 层这里再拦一次避免有人直接调 storage 传 exe）。不在白名单里 → 直接 `raise UnsupportedMediaTypeException(...)`。
   - 目标路径 `upload_dir / f"{recording_id}.{ext}"`。
   - **流式写**：循环 `chunk = await upload_file.read(1MB)` → 写到目标 `Path.open("ab")`；累计 `total_bytes += len(chunk)`；超过 `settings.max_upload_size_mb * 1024 * 1024` 字节时立刻 `path.unlink(missing_ok=True)` → `raise PayloadTooLargeException(f"...")`（spec §4.5 R6，双保险配合 uvicorn `--limit-max-request-size`）。
   - 写完返回 `(target_path.resolve(), total_bytes)`。
3. `delete_file_if_exists(path_or_str: str | Path) -> None`：
   - 先转成 `Path`。如果是相对路径就拼 `settings.storage_upload_dir / p`。
   - `try: p.unlink() except FileNotFoundError: pass except OSError as e: log warning`（任何磁盘问题只打日志，不抛异常给上层，spec §4.5 DELETE 边界）。
4. 日志贯穿：每个函数入口出口打 INFO；大小超限时打 WARNING 附带请求源 IP（如果能拿到的话，拿不到就先不打）。

### 验收标准
1. **流式 MD5 正确性**：写一个临时 py 脚本用同步读的方式算同一二进制文件的 MD5，与 `asyncio.run(compute_file_md5_streaming(...))` 的结果相等（大小写 insensitive 比较）。
2. **流式大文件内存占用**：生成一个 200MB 的随机二进制测试文件 `test.bin`，memory_profiler 跑 save_uploaded_file，**峰值 RSS ≤ 100MB**（证明不是一次性读内存，spec R6）。
3. **扩展名白名单**：传 ext='EXE' 或 ext='sh' → raise UnsupportedMediaTypeException，且**目录里没有留下任何残留文件**。
4. **大小超限**：传一个 51MB 测试 mp3（先在 settings 临时改成 50MB 上限）→ raise PayloadTooLargeException，**uploads 目录里没残留**（非常重要！不然会产生垃圾文件）。
5. **删除幂等**：调用 delete_file_if_exists 同一个不存在的路径 10 次 → 无任何异常。删除一个存在的文件 → 第二次再删也不报错。

### 测试要求
写 3 个**临时脚本**（不要写 pytest，pytest 框架在 T12），脚本放在 `scripts_tmp/` 里，验证完**保留不删**，后面 T13 正式测试时把它们改造成 pytest 用例：
- `scripts_tmp/test_storage_md5.py`：断言 MD5 值相等
- `scripts_tmp/test_storage_size.py`：构造 51MB 假文件，断言 raise PayloadTooLargeException
- `scripts_tmp/test_storage_delete.py`：断言 50 次删同一文件不报错

### 依赖
Ticket 1（Settings.STORAGE_UPLOAD_DIR / MAX_UPLOAD_SIZE_MB）、Ticket 2（UnsupportedMediaTypeException / PayloadTooLargeException / get_logger）。

### 不包含
1. 不写 API 路由（T6 POST /v1/recordings 才调 storage）。
2. 不操作数据库。
3. 不实现上传幂等（幂等是 T6 API 层查 recordings.file_hash UNIQUE，storage 层只负责算哈希、不负责去重）。

---

## Ticket 5：Schemas 层 — 全部 Pydantic 请求/响应结构

### 目标
在 `app/schemas.py` 中一次性定义完全部 Pydantic v2 模型（Request、Response、Enum、错误、分页泛型）。所有字段严格对齐 spec §2.2 接口契约 + §4.4.3 结构。

### 背景
来自 **[spec §4.4.3 Pydantic Schemas 列表](file:///d:/code/xisiyun/spec.md#L494-L502)**。API 路由层（T6/T10/T11）每个响应模型 `response_model=xxx` 都依赖这里的定义，提前一次性写好避免每个 ticket 反复加字段。

### 修改范围
- 新建：`app/schemas.py`
- 不修改任何已有文件

### 任务
1. **基础枚举 + Config**：
   - `TaskStatus(str, Enum)`：5 个值 `pending/transcribing/summarizing/done/failed`（和 ORM 的 Enum 字符串 1:1）。
   - `BaseSchema(BaseModel)`：`model_config = ConfigDict(from_attributes=True, populate_by_name=True, use_enum_values=True)`（from_attributes 保证能 `model_validate(orm_obj)`）。
2. **错误响应结构（T2 层 handler 的返回体结构 1:1）**：
   - `ErrorBody(BaseSchema)`：`code: str`、`message: str`、`details: dict[str, Any] | None = None`。
   - `ErrorResponse(BaseSchema)`：`error: ErrorBody`。
3. **上传接口**：
   - `UploadResponse(BaseSchema)`：**严格按 PDF 三个字段名**，不能改拼写！`recording_id: str`、`task_id: str`、`status: TaskStatus`。
4. **任务相关**：
   - `TaskOut(BaseSchema)`：`id: str`、`recording_id: str`、`status: TaskStatus`、`current_stage_retry_count: int = Field(ge=0)`、`total_retry_count: int = Field(ge=0)`、`error_message: str | None = None`、`created_at: datetime`、`updated_at: datetime`。
5. **录音相关 + 摘要 + 分页**：
   - `SummaryOut(BaseSchema)`：`summary: str`、`key_points: list[str]`、`todos: list[str]`。
   - `RecordingListItem(BaseSchema)`：`id: str`、`original_filename: str`、`file_ext: str`、`file_size_bytes: int = Field(ge=0)`、`last_status: TaskStatus`、`created_at: datetime`、`updated_at: datetime`。
   - `RecordingDetailOut(RecordingListItem)`：继承 ↑，加 `transcript: str | None = None`、`summary: SummaryOut | None = None`。**注意**：不要用 recording.summary_json 原 JSON 字符串，要转成 SummaryOut 对象；序列化逻辑在「ORM → Pydantic」里做（T11 写，但 schema 先把字段定义好）。
   - `PaginationQueryParams(BaseSchema)`：`page: int = Field(1, ge=1)`、`page_size: int = Field(20, ge=1, le=100)`（spec §4.5 分页上限 100，Field(le=100) 自动拦截恶意 page_size=10000，返回 422）。
   - `PagedResponse[T]`：**用 typing.Generic 泛型**，字段 `total: int = Field(ge=0)`、`page: int = Field(ge=1)`、`page_size: int = Field(ge=1, le=100)`、`items: list[T]`。
6. **内部 LLM 返回（仅 services/llm.py 用，不出现在 API 层）**：
   - `LLMSummaryResponse(BaseSchema)`：字段和 SummaryOut 完全相同，但**加严格校验** `key_points: Annotated[list[str], Field(min_length=1)]`（要求 LLM 至少返回 1 个 key_points，不然判格式错进入重试，spec §4.5 LLM 字段缺失场景）。

### 验收标准
1. `python -c "from app.schemas import UploadResponse, TaskOut, RecordingDetailOut, PagedResponse, SummaryOut, LLMSummaryResponse, ErrorResponse"` → **零 ImportError**。
2. `ur = UploadResponse(recording_id="a", task_id="b", status="pending")` → `ur.model_dump() == {"recording_id":"a","task_id":"b","status":"pending"}`（字段不能多不能少，PDF 原文三字段）。
3. `PaginationQueryParams(page=0)` → 抛 ValidationError（ge=1 生效）；`PaginationQueryParams(page_size=10000)` 也抛 ValidationError（le=100 生效）。
4. `LLMSummaryResponse(summary="x", key_points=[], todos=[])` → 抛 ValidationError（key_points min_length=1 生效，spec R7 第三层校验）。
5. `PagedResponse[RecordingListItem](total=1, page=1, page_size=20, items=[RecordingListItem(...)])` → Pydantic 不报错，items 的每个元素自动是 RecordingListItem 类型（证明泛型正常工作）。

### 测试要求
用 `scripts_tmp/test_schemas.py`（临时脚本，保留）跑以上 5 个断言脚本即可。不写 pytest。

### 依赖
Ticket 1（无直接 import 依赖，但 Schema 里 datetime 等类型与 settings 时间格式一致性约定）。

### 不包含
1. 不写路由 / 不做 ORM 对象 model_validate（留到路由 ticket 做）。
2. 不实现任何业务逻辑。
3. 不做「从 summary_json JSON 字段转 SummaryOut 对象」的具体转换函数（留到 T11 recordings detail 接口做）。

---

## Ticket 6：POST /v1/recordings 上传接口 + 上传幂等

### 目标
实现 P0 第 1 个接口：`POST /v1/recordings`（multipart/form-data, file 字段）。完整实现校验、落盘、MD5 去重（P1-4 加分项）、两张表初始记录插入，立即返回 pending，**不等待异步处理**（异步入队先在本 ticket 末尾打 TODO，等 T7 的 queue 对象可用后再补一行调用，先保证同步逻辑全对）。

### 背景
来自 **[spec §3 P0-3](file:///d:/code/xisiyun/spec.md#L161-L183)** + **[spec §3 P1-4 上传幂等](file:///d:/code/xisiyun/spec.md#L251-L260)** + **[spec §2.2 上传接口契约原文](file:///d:/code/xisiyun/spec.md#L83-L91)**。对应 spec §7 R2（必须立即返回，不能等处理）、spec §7 R6（流式 IO）、spec §4.5 磁盘 vs DB 写入顺序。

### 修改范围
- 新建：`app/api/__init__.py`、`app/api/v1/__init__.py`、`app/api/v1/router.py`（APIRouter(prefix="/v1") 聚合子路由）、`app/api/v1/recordings.py`（本接口 + 预留 T11 其他方法）
- 新建：`app/services/__init__.py`（如果 T4 没建的话）、`app/services/recordings.py`（Recording + Task 的 DB 创建服务层，避免路由直接写 SQL）
- 修改：`app/main.py`（`app.include_router(api_v1_router)`）

### 任务
1. **services/recordings.py**（服务层，纯 DB 操作，不碰磁盘）：
   - `async def create_recording_with_task(session: AsyncSession, *, original_filename: str, file_ext: str, file_size_bytes: int, file_hash: str, storage_path: str) -> Tuple[Recording, Task]`：
     - 用 `uuid.uuid4().hex` 或 `str(uuid4())` 生成两个 id。
     - 事务内：`session.add(Recording(id=..., ...))` → `session.add(Task(id=..., recording_id=..., status=pending))` → `flush` → `commit` → 手动 `refresh(recording), refresh(task)` 或直接返回两个对象。
     - **如果 file_hash 已存在（IntegrityError UNIQUE violation）**：捕获异常 → rollback → 查询 `SELECT * FROM recordings WHERE file_hash=?` 连 tasks 表拿到对应最新的一个 task（按 created_at desc limit 1，一个 recording 暂时设计成一个 task 就够）→ 返回（旧 recording, 旧 task）。这样外层路由不用自己判断幂等，服务层统一处理。
2. **api/v1/router.py**：
   ```python
   from fastapi import APIRouter
   from app.api.v1 import recordings, tasks
   router = APIRouter(prefix="/v1")
   router.include_router(recordings.router, prefix="/recordings", tags=["recordings"])
   router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])    # tasks 在 T10 才写，先 import 放着
   ```
   （tasks.router 在 T10 新建，这里先注释掉或在 `app/api/v1/tasks.py` 先建空 router 防 ImportError）
3. **api/v1/recordings.py**：
   - router = APIRouter()
   - `@router.post("", response_model=UploadResponse, status_code=200)`
   - 参数：`file: UploadFile = File(...)`（FastAPI File 自动校验 multipart 字段存在），`session: AsyncSession = Depends(get_session)`。
   - **校验顺序严格按性能**（早失败早返回，spec §3 P0-3 顺序）：
     ① `ext = file.filename.rsplit(".", 1)[-1].lower()` 如果没后缀或后缀 ∉ 白名单 → `raise UnsupportedMediaTypeException("...")`（T2 的异常类）。
     ② **先流式算 MD5**（避免大小超限还算了完整 MD5，其实顺序无所谓，但先算能顺便累计 size）：`file_hash = await storage.compute_file_md5_streaming(file)` + 累计 size 已经算过 → `await file.seek(0)` 回到开头。
     ③ **幂等查询**（服务层 create_recording_with_task 内部会捕获 UNIQUE 并返回旧值，但我们在创建前先 SELECT 一次省得抛异常的开销也行，两种任选；选服务层捕获 UNIQUE 的实现更原子）。
     ④ 如果需要创建新的（先查 file_hash 未命中）→ **先写磁盘**：`saved_path, saved_size = await storage.save_uploaded_file(file, recording_id？不，recording_id 还没生成)` —— **关键顺序问题解决**：先生成 UUID（但还没写 DB）→ 用这个临时 UUID 当文件名写磁盘 → 再调 services.create_recording_with_task 把 storage_path 传进去；如果 create_recording_with_task 最终返回的是旧 recording（竞态下两个请求同时传同 MD5）→ 把刚写的新文件删掉（storage.delete_file_if_exists），返回旧的那对；如果是新的就保留。
     ⑤ 「立即返回」：**本 ticket 异步入队的调用先用 `# TODO: T7 impl: await pipeline_singleton.enqueue_task(task_id)` 注释占位**。返回 `UploadResponse(recording_id=r.id, task_id=t.id, status=t.status.value)`。
   - 日志：打 INFO "[upload] received ext={ext} size={size} original_name={name}"；命中幂等时打 WARNING "[upload] hit idempotent, return existing recording_id=..."；创建成功打 INFO。
4. **main.py** include router 如上。
5. 注意：uvicorn 启动加 `--limit-max-request-size $((50*1024*1024))` 这个参数 spec 里是启动命令的，但也可以用 FastAPI 中间件再限一次（本 ticket 不强求）。README 启动命令会写清楚。

### 验收标准
1. **立即返回验证**：`time curl -X POST ... -F "file=@test.wav"` 命令 `real` 时间 < 500ms（即使 Mock 转写需要 15 秒！spec §7 R2）。
2. **合法性校验**：
   - 不传 file 字段 → 422（FastAPI 自动）。
   - 传 file.txt → 415，错误结构符合 T2 的统一格式。
   - 传 51MB fake.mp3（storage 层已设 50MB 上限）→ 413，**uploads 目录下无残留垃圾文件**（spec §4.5）。
3. **上传成功**：传合法小 wav → 200 body 三个字段；recordings/tasks 两张表各一条；tasks.status=pending；uploads 下有一个 `{uuid}.wav` 文件。
4. **上传幂等 P1-4**：同一个物理文件（哪怕改名为 a.wav 和 b.wav）上传两次 → 两次响应的 `recording_id` **完全相等**；recordings 表 COUNT=1；uploads 下只有 1 个物理文件（竞态下两个并发请求传同文件也不能产生两个 recording id，服务层 UNIQUE violation 捕获要测一下，写脚本多线程跑 10 次）。
5. **响应体字段名严格一致**：上传响应的 JSON keys == `{"recording_id", "task_id", "status"}`，**不能出现 recordingId / taskId 小驼峰（直接挂面试，PDF 原文是 snake_case）**。

### 测试要求
临时脚本 `scripts_tmp/test_upload_flow.py`：
- 生成 4 个假文件：empty/a.wav(1KB)/b.wav(字节与 a 完全相同)/big.mp3(51MB)/wrong.txt
- 用 `httpx.AsyncClient(base_url="http://localhost:8000")` 分别 POST 4 次
- 断言 HTTP 状态码 + 幂等 recording_id 相同

pytest 用例在 T13 写。

### 依赖
Ticket 3（ORM 模型 + 表建好）、Ticket 4（storage 三个函数）、Ticket 5（UploadResponse schema）、Ticket 2（异常类）。

### 不包含
1. **不实现异步任务入队和 worker**（T7 + T9 做，本 ticket 只写 TODO 注释）。
2. **不写其他 5 个接口**（GET list、GET detail、DELETE、GET tasks、POST retry，留到 T10/T11）。
3. 不做 Mock 转写/LLM 调用（T9）。

---

## Ticket 7：异步任务引擎 — asyncio Queue + Worker + Startup Resume + Semaphore

### 目标
搭建后台协程驱动的执行引擎（方案 B，spec Q4）：`asyncio.Queue`（存 task_id 字符串）+ `asyncio.Semaphore(MAX_CONCURRENT_TASKS)`（并发控制，加分项 P1-5）+ 后台 `worker_loop` 协程（从队列取 id，acquire 信号量后调一个**占位空函数** `run_pipeline_placeholder(task_id)`）+ startup lifespan hook（启动 N 个 worker 协程 + 扫 DB 非终态任务入队，加分项 P1-2 重启恢复）。

### 背景
来自 **[spec §3 P0-4 异步任务引擎](file:///d:/code/xisiyun/spec.md#L185-L200)** + **[P1-2 服务重启恢复](file:///d:/code/xisiyun/spec.md#L250-L252)** + **[P1-5 并发控制](file:///d:/code/xisiyun/spec.md#L255-L258)**。对应 spec §7 R8（重启时要把 transcribing/summarizing 先重置为 pending，否则阶段乐观锁 WHERE 不命中任务卡死）。

### 修改范围
- 新建：`app/services/pipeline.py`（引擎主文件，run_pipeline 真实逻辑在 T9 填，本 ticket 只写占位 + 调度骨架）
- 修改：`app/main.py` lifespan（启动 worker + resume；shutdown 时 cancel worker task 优雅退出）
- 修改：T6 `app/api/v1/recordings.py` POST 上传接口末尾，把 T6 留的 TODO 注释**替换成真实一行** `from app.services.pipeline import enqueue_task; await enqueue_task(task_id)`（但 `run_pipeline` 还是占位空跑，所以任务会被"立即处理完成"，状态还是 pending 没关系，T9 改真实逻辑）

### 任务
1. **pipeline.py 结构**：
   - 模块级私有：`_queue: asyncio.Queue[str] | None = None`、`_sem: asyncio.Semaphore | None = None`、`_worker_tasks: list[asyncio.Task] = []`、`_logger = get_logger("pipeline")`、`_NUM_WORKERS = 5`（worker 数略大于 Semaphore 数，避免一个阻塞时其它空转；Semaphore 3 + Workers 5 是经验值）。
   - **初始化函数** `init_engine(settings: Settings) -> None`：创建 `_queue = asyncio.Queue(maxsize=settings.max_concurrent_tasks*100)`（有界队列，防无限堆）、`_sem = asyncio.Semaphore(settings.max_concurrent_tasks)`、log INFO。
   - **对外 API** `async def enqueue_task(task_id: str) -> None`：`await _queue.put(task_id)`（队列满会阻塞，这是设计的；反压）；log INFO "[pipeline] enqueued task_id=%s queue_size=%d"。
   - **worker 主循环** `async def _worker_loop(worker_id: int) -> None`：`while True: task_id = await _queue.get(); try: async with _sem: await _run_pipeline_placeholder(task_id); finally: _queue.task_done()`。这里的 `async with _sem` 就是并发控制实现 P1-5（同时最多 settings.max_concurrent_tasks=3 个 `_run_pipeline_placeholder` 在跑）。
   - **占位 pipeline** `async def _run_pipeline_placeholder(task_id: str) -> None`：**当前只做** log INFO "[pipeline] worker processing task_id=%s (placeholder)" + `await asyncio.sleep(0.1)` + log INFO "[pipeline] task_id=%s placeholder done"。**不碰 DB 不碰 LLM**，T9 替换成真实逻辑。
   - **start/shutdown 控制**：`async def start_workers(settings: Settings) -> None`：先 init_engine(settings) → `for i in range(_NUM_WORKERS): t=asyncio.create_task(_worker_loop(i), name=f"pipeline-worker-{i}"); _worker_tasks.append(t)` → log INFO "started N pipeline workers, max_concurrent=%d"；`async def shutdown_workers() -> None`：给每个 worker task `cancel()` → `gather(*_worker_tasks, return_exceptions=True)` → log INFO "pipeline workers shutdown complete"。
   - **重启恢复 P1-2（核心！spec R8）** `async def resume_pending_tasks_on_startup(session: AsyncSession) -> int`：
     ① **第一步必须是 UPDATE**（spec R8 重中之重）：`UPDATE tasks SET status='pending', current_stage_retry_count=0 WHERE status IN ('transcribing','summarizing')` → commit。理由：上次服务被杀时可能停在中间阶段，status 不是 pending，阶段乐观锁 WHERE 不命中就永远卡死；重置成 pending 就跟新任务一样跑。记录 updated 行数 log。
     ② 第二步查询：`SELECT id FROM tasks WHERE status = 'pending' ORDER BY created_at ASC` → 得到所有待处理 task_id。
     ③ 第三步：循环 `await _queue.put(tid)` 入队，不能 put_nowait（队列可能满，要阻塞等）。
     ④ 返回入队总数量，log WARNING "[pipeline] resumed %d tasks from DB (X reset from in-progress state)"。
2. **main.py lifespan 改造**（用 async context manager，FastAPI 官方推荐）：
   ```python
   @asynccontextmanager
   async def lifespan(app: FastAPI):
       # startup
       settings = Settings()
       storage.ensure_upload_dir()  # 确保 uploads 目录存在，storage 层加个小函数就行
       await pipeline.start_workers(settings)
       async with AsyncSessionLocal() as session:
           await pipeline.resume_pending_tasks_on_startup(session)
       yield
       # shutdown
       await pipeline.shutdown_workers()
   ```
   （确保 resume 时数据库已经 ready，所以在 startup 顺序最后做）
3. **T6 上传接口末尾 TODO 替换**：`await pipeline.enqueue_task(task.id)` 一行；验证上传后日志里能看到 enqueued + placeholder processing。

### 验收标准
1. **并发控制验证 P1-5**：临时把 `_run_pipeline_placeholder` 改成 `await asyncio.sleep(2)`，写脚本同时 enqueue 10 个 task_id，看日志时间戳：**同一时刻只有 3 个 placeholder processing 在跑**，第 4 个开始的时间必须约等于第 1 个结束时间（2 秒后），Semaphore 生效。
2. **重启恢复验证 P1-2 + spec R8**：
   - 往 tasks 表插入 3 条测试数据：1 条 pending / 1 条 transcribing（updated_at 很久之前）/ 1 条 summarizing，对应 recording id 随便建（INSERT 就行）。
   - 重启 uvicorn（`Ctrl+C` 再启）。
   - **验收两项**：① 启动日志里打印 "resumed 3 tasks from DB (2 reset from in-progress state)"；② 查 MySQL：那两条原来 transcribing/summarizing 的 status 现在**全部变成 pending**（spec R8，没有这个 UPDATE 就 R8 挂）。
   - 然后等 ~3 秒（placeholder 0.1s×并发 3≈0.5s 就能全处理完，但我们可以看 worker 日志），日志里有 10 个 task_id 的 processing + done。
3. **上传接口真入队**：T6 测试上传一次 → 立即能在 pipeline 日志看到 placeholder processing task_id。响应时间还是 < 500ms（enqueue 是毫秒级，spec R2 仍满足）。
4. **优雅关闭**：uvicorn 按 Ctrl+C → 日志里输出 "pipeline workers shutdown complete"；没有任何 `Task was destroyed but it is pending!` 错误（asyncio 最怕这个警告，有警告要处理完 cancel 才过验收）。
5. **有界队列反压**：临时把 maxsize 改成 2，写脚本同时塞 5 个 enqueue → 第 3~5 个调用会 await 阻塞（不会无限堆内存把进程吃死）。

### 测试要求
`scripts_tmp/test_engine_concurrent.py`：临时脚本（asyncio.run + logging）验证并发数 3、重启恢复 UPDATE 执行、Ctrl+C 无警告。不写 pytest。

### 依赖
Ticket 6（上传接口要在末尾真实调 enqueue）、Ticket 3（ORM tasks.status 字段）、Ticket 2（AsyncSessionLocal）。

### 不包含
1. **不实现真实 pipeline 状态机**（Mock 转写 + LLM + 重试，T9 做）。
2. 不修改 GET /tasks 或 POST retry 接口（T10）。
3. 不实现失败自动重试 P1-1（T9 做，在 run_pipeline 真实逻辑里）。

---

## Ticket 8：LLM 服务层封装 — DeepSeek 调用 + 超时 + 格式校验三层保险

### 目标
写 `app/services/llm.py` 对外暴露单函数：`async def summarize_transcript(transcript: str) -> dict[str, Any]`，严格返回 `{summary:str, key_points:list[str], todos:list[str]}` 结构的 dict；内部实现 DeepSeek 真实 HTTP 调用，**三层保险**防格式错（response_format + json.loads + Pydantic validate），30 秒超时从 settings 读（可改）。

### 背景
来自 **[spec §2.2 摘要阶段（真实 LLM）原文](file:///d:/code/xisiyun/spec.md#L92-L102)** + **[spec §4.5 LLM 边界](file:///d:/code/xisiyun/spec.md#L564-L571)**。对应 spec §7 R7（三层保险，漏一层扣分）。

### 修改范围
- 新建：`app/services/llm.py`
- 修改：`app/config.py`（如果 DEEPSEEK_* 配置字段之前 T1 写漏了，补）
- **不碰路由 / 不碰 pipeline**（pipeline T9 调这个）

### 任务
1. **prompt 设计**（写死在 llm.py 顶部常量，不要动 DB）：
   - SYSTEM_PROMPT：明确要求"You are a precise transcript summarizer. You MUST output a valid JSON object with EXACTLY 3 keys: 'summary' (string, one-sentence high-level overview), 'key_points' (array of strings, 2~5 bullet points), 'todos' (array of strings, 0~3 action items extracted). If no todos exist return empty array []. Do NOT include any markdown fences, explanations, or extra keys outside the JSON object."
   - USER_PROMPT_TEMPLATE：`f"Here is the transcript:\n\n{transcript}\n\nNow output the summary JSON per instructions:"`（transcript 前后空行分开，避免模型把指令当内容）。
2. **HTTP 客户端**：
   - 模块级 `_client: httpx.AsyncClient | None = None`；初始化函数 `init_client(settings: Settings) -> httpx.AsyncClient`：`timeout=httpx.Timeout(settings.deepseek_timeout_seconds, connect=10.0)`、`base_url=settings.deepseek_base_url`、`headers={"Authorization": f"Bearer {settings.deepseek_api_key}"}`、`http2=True`（DeepSeek 支持）。如果 settings.deepseek_api_key 为空（用户临时没填）→ 打 ERROR log 但不崩，后面调用会抛异常。
   - `shutdown_client()`：`await _client.aclose()`（main.py shutdown 时调，避免 httpx Client 未关闭警告）。
3. **核心调用函数** `async def summarize_transcript(transcript: str) -> dict[str, Any]`：
   - transcript 为空字符串或 len < 10 → `raise BadRequestException("Transcript too short to summarize")`（基本防御，防止 mock 阶段产出空文本直接调 LLM 浪费额度）。
   - 构造 payload：
     ```python
     {
       "model": settings.deepseek_model,
       "messages": [{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":USER_PROMPT_TEMPLATE.format(transcript=transcript[:20000])}],  # 截断，避免超出 context window
       "temperature": 0.1,  # 摘要要确定，不要高温度瞎编
       "response_format": {"type": "json_object"},  # 第一层保险（spec Q7 c1）
       "max_tokens": 2048,  # 摘要一般 < 1000 token 够
       "stream": False
     }
     ```
   - 调 `resp = await _client.post("/chat/completions", json=payload)` → **严格 HTTP 错误处理**：`if resp.status_code != 200: raise LLMCallException(status=resp.status_code, body=resp.text[:500])`（把 DeepSeek 401 Invalid API Key / 429 Rate limited 等包成 LLMCallException，T2 的类）。
   - `resp_json = resp.json()` → 取 `content = resp_json["choices"][0]["message"]["content"].strip()` → 取 content 过程中任何 KeyError（DeepSeek 返回结构变了）→ 抛 `LLMCallException(message="Unexpected response schema from LLM")`。
   - **第二层保险（json.loads）**：`try: parsed = json.loads(content) except json.JSONDecodeError as e: raise LLMResponseFormatException(f"Invalid JSON: {e}, raw_content={content[:200]}")`（spec R7 第二层）。
   - **第三层保险（Pydantic 字段校验）**：用 T5 的 schemas `LLMSummaryResponse.model_validate(parsed)` → 校验字段存在 + key_points 至少 1 个；ValidationError 时抛 `LLMResponseFormatException(f"Schema mismatch: {e}, parsed={parsed}")`（spec R7 第三层）。
   - 最终返回 `validated.model_dump()`（保证 key 就是 summary/key_points/todos）。
4. **日志**：调用前 log INFO `[llm] summarize transcript_len=%d model=%s timeout=%ds`；成功时 log INFO `[llm] success output_len=%d summary_preview=%s`；失败时把 raw_content[:500] 打 ERROR 进文件日志（响应体不泄露）。

### 验收标准
1. **真实调用通过**：`.env` 里填你的真实 DeepSeek Key；临时脚本 `scripts_tmp/test_llm_real.py` 传一段 ≥ 500 字的中文长文本（随便粘篇新闻）→ 返回 dict 三个字段齐全、类型对、summary 不是空字符串 → 通过。
2. **超时触发**：临时把 `.env` 的 DEEPSEEK_TIMEOUT_SECONDS 改成 0.01 → 调用抛 httpx.TimeoutException → 被包装成 `LLMCallException`（pipeline T9 会捕获重试，但这里直接验异常类型对）。
3. **Key 错触发**：改 API Key 为 `sk-fake123` → 调 DeepSeek 返回 401 → 抛 `LLMCallException` 含 `401` 字样。
4. **格式错三层保险**：写一个 mock httpx 返回 `content = '{"summary":"x"}'`（缺 key_points/todos）→ 第三层 Pydantic validate 抛 ValidationError → 最终抛出 `LLMResponseFormatException`（两层 JSON 对但 schema 缺字段的情况 spec 明确要求处理）。
5. **空 transcript 防御**：传 `""` → 抛 BadRequestException，**不发 HTTP 请求**（用 httpx Mock 测有没有网络调用没发就行，或者看日志里没有 `[llm] summarize` INFO 行）。

### 测试要求
`scripts_tmp/test_llm_*.py` 4 个临时脚本：真实调用 / 超时 / key 错 / 字段缺失。保留脚本，T13 pytest 化。
注意：脚本里测到第 2/3/4 失败场景后，把 .env 的 key 和 timeout 改回去，别影响后续 ticket 联调。

### 依赖
Ticket 5（LLMSummaryResponse schema）、Ticket 2（LLMCallException / LLMResponseFormatException / logger）、Ticket 1（DEEPSEEK_* 配置）。

### 不包含
1. 不做 LLM SSE 流式（spec §6 不做）。
2. 不把 LLM 返回写入 DB（T9 pipeline 层负责写）。
3. 不做失败自动重试（重试是 pipeline 的事，llm 层只抛异常让上层决定重试次数）。

---

## Ticket 9：Pipeline 状态机核心 — Mock 转写 + LLM 摘要 + 自动重试 + 乐观锁

### 目标
把 T7 的 `_run_pipeline_placeholder(task_id)` 占位函数**替换为真实状态机 `_run_pipeline_real(task_id)`**：严格 5 态流转；转写 Mock 5~15s/20% 失败；摘要调 T8 llm.summarize_transcript；**每个阶段独立 3 次指数退避自动重试 P1-1**；所有状态更新 **WHERE 乐观锁**（WHERE status='prev_state'，rowcount==0 时直接 break 退出，防止同一个任务被两个 worker 重复跑）。

### 背景
来自 **[spec §3 P0-5 状态机阶段逻辑](file:///d:/code/xisiyun/spec.md#L202-L238)** + **[P1-1 失败自动重试（3 次指数退避）](file:///d:/code/xisiyun/spec.md#L250-L252)**。这是全项目**最核心的文件**，占代码量 ~40%。对应 spec §2 状态机原文（5 态、任一环节失败进 failed）+ spec §7 R1/R8 所有风险点。

### 修改范围
- 修改：`app/services/pipeline.py`（把 `_run_pipeline_placeholder` 重命名/替换成 `_run_pipeline_real`，worker 循环里调用改名后的函数；其他 skeleton 保留不动）
- 修改：T8 `app/services/llm.py` init/shutdown（main.py lifespan 里加上 llm.init/shutdown）
- 修改：`app/main.py` lifespan（加上 llm.init_client / shutdown_client 调用；startup 顺序：storage → engine → resume → llm.init，shutdown 相反）

### 任务
1. **pipeline.py 顶层 helper**：
   - `def _random_transcript() -> str`：返回 500~2000 字的随机中文（可以用 `lorem` 库 pip install `python-lorem`？不要，spec §6 不新增不必要依赖，直接写一个函数随机选 50 段固定句子拼接 + 随机字数就行；或直接内置一段 2000 字的固定中文长文，Mock 阶段"固定或随机文本"PDF 说都可以）。
   - `def _backoff_seconds(attempt_idx_0based: int) -> float`：`return 2 ** attempt_idx_0based * 1.0` → 第 0 次失败后等 1s，第 1 次 2s，第 2 次 4s（PDF 最多 3 次，所以 attempt_idx=0/1/2 三档）。
   - 日志 helper `_log_task(level, task_id, msg, **kw)`：统一 `extra={"task_id": task_id}` 输出。
2. **核心 `_run_pipeline_real(task_id: str) -> None`**（外层先 `async with AsyncSessionLocal() as session:` 拿一个长 session 用到底，commit 手动调）：
   - 先 `SELECT * FROM tasks WHERE id=?` + JOIN recordings 拿到 `(task, recording)` 两对象；不存在就 return（可能 DB 被删了但队列还有 id，正常，静默忽略）。
   - **阶段 1：转写（含 3 次重试 P1-1）**：
     ```
     for stage1_attempt in range(settings.pipeline_max_auto_retries):  # 0/1/2
         # 乐观锁：必须前态是 pending 才能改 transcribing
         res = await session.execute(update(Task)
             .where(Task.id == task_id, Task.status == TaskStatus.pending)
             .values(status=TaskStatus.transcribing,
                     current_stage_retry_count=stage1_attempt,
                     error_message=None))
         await session.commit()
         if res.rowcount == 0:
             _log("WARN", f"stage1 lock not acquired, status is not pending, assume handled by another worker, skip")
             return
         try:
             # Mock 随机耗时 + 20% 失败
             await asyncio.sleep(random.uniform(5, 15))
             if random.random() < 0.2:
                 raise RuntimeError("Mock ASR 20% probability failure (stage 1)")
             transcript = _random_transcript()
             # 成功：写 DB recording + 改状态到 summarizing（也是乐观锁，WHERE status=transcribing）
             await session.execute(update(Recording)
                 .where(Recording.id == task.recording_id)
                 .values(transcript=transcript, last_status=TaskStatus.summarizing, updated_at=func.now()))
             res2 = await session.execute(update(Task)
                 .where(Task.id == task_id, Task.status == TaskStatus.transcribing)
                 .values(status=TaskStatus.summarizing,
                         current_stage_retry_count=0,  # 进入新阶段，阶段内重试计数清零
                         error_message=None, updated_at=func.now()))
             await session.commit()
             if res2.rowcount == 0: return   # 被别人改了，放弃
             break   # stage1 成功，跳出重试
         except Exception as e:
             error_msg = f"Stage1 attempt {stage1_attempt+1} failed: {type(e).__name__}: {str(e)[:200]}"
             _log("ERROR", error_msg)
             await session.execute(update(Task)
                 .where(Task.id == task_id)
                 .values(error_message=error_msg, current_stage_retry_count=stage1_attempt+1, updated_at=func.now()))
             await session.commit()
             if stage1_attempt == settings.pipeline_max_auto_retries - 1:
                 # 重试耗尽，进 failed
                 await session.execute(update(Task).where(...).values(status=TaskStatus.failed, error_message=error_msg + " [RETRY EXHAUSTED]"))
                 await session.execute(update(Recording).where(...).values(last_status=TaskStatus.failed))
                 await session.commit()
                 return
             else:
                 sleep_s = _backoff_seconds(stage1_attempt)
                 _log("INFO", f"stage1 will retry in {sleep_s:.1f}s...")
                 await asyncio.sleep(sleep_s)
     ```
   - **阶段 2：摘要（3 次重试 P1-1）**：结构和阶段 1 完全相同，乐观锁前态是 `summarizing`（阶段 1 成功后状态就是它），成功后把 `summary_json`（T8 返回的 dict 直接塞 JSON 列）写入 recordings 表、两表状态最终改成 `done`。
     - 捕获的异常包括：`LLMCallException`、`LLMResponseFormatException`、`httpx.TimeoutException`、`json.JSONDecodeError`（T8 已经包装过了但再兜一层保险）、`ValidationError`（Pydantic 字段错）——全部计入重试计数。
     - 重试耗尽 → 两表 status=failed。
   - **最终成功日志**：两表 update 成 done 后，log INFO `[pipeline] task_id=xxx completed SUCCESS, took_total=%.1fs`；失败则 log ERROR 一行 `FAILED retry exhausted`。
3. **main.py lifespan 加 LLM 初始化**：startup 顺序是 「storage dir → pipeline.start_workers (resume 任务先入队，但 worker 还没拿到信号量不会跑）→ llm.init_client」；等所有就绪后 yield。**其实 resume 的任务 enqueue 后就可能被 worker 立即取到、进 summarizing，那时 llm.client 还没初始化的话会崩**，所以顺序要改成：**先 init storage → init llm → start workers（含 queue 创建 + sem 创建 + resume_db → 入队）→ yield**；另外要给 pipeline 模块加 settings 的引用，让 `_NUM_WORKERS` 和 semaphore 能拿到配置。
4. **死锁兜底**：每个 UPDATE ... WHERE 乐观锁 rowcount==0 的时候，说明任务状态已被别的执行流改了，**不要 raise 异常**，直接 return 退出函数（不然这个异常会被 worker_loop 的 except 捕获但 task_done 已经在 finally 调了，不会影响队列，但 return 就行，打个 WARN 日志留痕）。

### 验收标准
1. **正常成功流**：上传 1 个合法 wav → 等 6~45 秒（5~15s mock + 1~30s LLM × 不失败 1 次 = 6~45 秒）→ 查询 DB：tasks.status=done；recordings.transcript 非空（长度 ≥ 500 字）；recordings.summary_json **是合法 MySQL JSON 类型，三个键齐全、key_points 长度 ≥1**。
2. **转写重试耗尽进 failed**：临时把 `random.random() < 0.2` 改成 `True`（强制 100% 失败）→ 上传一个 → 观察日志：**等 1s → 再 2s → 再 4s 三次失败**（共 7 秒等待时间 + 3×15s=45s mock 睡，但强制失败跳过 sleep 所以总共 7 秒左右就能看到）→ 最终 tasks.status=failed，tasks.current_stage_retry_count=3，tasks.error_message 以 "Stage1 attempt 3 failed" 开头 且 末尾有 "[RETRY EXHAUSTED]"。改完概率记得还原！
3. **LLM 重试耗尽进 failed**：把 DEEPSEEK_API_KEY 改成 `sk-fake` → 上传 → 转写成功（状态走到 summarizing 几秒）→ 观察日志：3 次 HTTP 401 间隔 1/2/4s → 最终 failed，error_message 含 "401"。.env 改回真 key。
4. **乐观锁生效**：在阶段 1 的 `asyncio.sleep(5~15s)` 中间（mock 还在睡），手动 MySQL 执行 `UPDATE tasks SET status='done' WHERE id='xxx'` → 等 sleep 结束后代码执行 UPDATE ... WHERE status='pending' → rowcount=0 → WARN 日志输出 "stage1 lock not acquired, handled by another worker, skip" → 函数 return，**没有继续跑下去也没有抛异常挂掉**。
5. **终态状态对应**：上传后任何时间点 DB 里 `tasks.status == recordings.last_status`（除了事务中间极短间隙，查询时必须相等，这个可以脚本 100 次查验证一致性成立）。
6. **日志完整性**：`grep task_id=xxx logs/app.log` 能看到按时间顺序：enqueued → stage1 start → stage1 attempt N failed → stage1 success → stage2 start → stage2 attempt N failed → stage2 success → SUCCESS/FAILED exhausted；共 8~12 条日志，顺序对没有跳步。

### 测试要求
临时脚本 `scripts_tmp/test_pipeline_state_machine.py`：
- 脚本内部用 monkeypatch 改 random 函数强制走成功/失败分支（避免等 15 秒），快速验 6 条验收标准的前 5 条（日志 grep 用 Python open 读文件 assert 关键字）。
- 本脚本**一定要留好**，是面试时快速演示状态机的利器。
- pytest 化在 T13。

### 依赖
T7（pipeline 引擎 + worker 循环）、T8（llm.summarize_transcript）、T5（schemas 校验）、T3（ORM 模型 UPDATE 用）。

### 不包含
1. **不写任何查询/重试/删除接口**（T10/T11 做）。
2. **不实现 POST /tasks/{id}/retry 手动重试**（T10 做；自动重试和手动重试是两回事，自动是阶段内 3 次，手动是用户对最终 failed 的 task 触发从头再来 pending，重置 total_retry_count）。
3. 不改上传接口。
4. **不写 README / api_test.http**（T14/T15）。

---

## Ticket 10：Tasks 路由 — GET 状态查询 + POST retry（409 幂等 + 行锁）

### 目标
实现 P0 必做的两个 tasks 接口：① `GET /v1/tasks/{task_id}`（返回 TaskOut schema，processing 阶段要能区分是转写还是摘要，也就是 status 字段直接是字符串，PDF 要求）；② `POST /v1/tasks/{task_id}/retry`（**仅 failed 状态允许**；SELECT FOR UPDATE 行锁 + 乐观判断双重幂等；重复请求返回 409；成功后把任务重新塞回 pipeline 队列 + 重置重试计数）。

### 背景
来自 **[spec §2.2 两个任务接口原文](file:///d:/code/xisiyun/spec.md#L83-L91)**（GET status 体现阶段 + retry 仅 failed + 处理重复请求）。对应 spec §7 R5（POST retry 必须行锁，否则并发重复入队）。

### 修改范围
- 新建：`app/api/v1/tasks.py`
- 修改：`app/services/recordings.py` 或新建 `app/services/tasks_service.py`（retry 业务逻辑放服务层，不写在路由里）
- 修改：T6 `app/api/v1/router.py`（把 include tasks.router 的注释打开，确保 prefix 正确 `/v1/tasks`）

### 任务
1. **新建 tasks_service.py（服务层）**：
   - `async def get_task_by_id_or_404(session, task_id) -> Task`：查 DB 不存在抛 NotFoundException("Task", task_id)。
   - `async def retry_task(session, task_id: str) -> Tuple[Task, bool]`（返回 bool=是否真的执行了 enqueue，方便路由判断日志）：
     - 事务用 `async with session.begin():` 或者手动 begin/committing，但要支持 FOR UPDATE 行锁所以必须显式事务。
     - 步骤顺序（严格按 spec §3 P0-6 POST retry 硬约束 + spec R5）：
       ① `SELECT * FROM tasks WHERE id = ? FOR UPDATE`（`for_update=True` 在 SQLAlchemy select 里加，MySQL 会拿行级写锁直到事务结束）。
       ② 如果 task 不存在：抛 NotFound（此时可以不 commit，因为没有写；但要 rollback）。
       ③ **乐观幂等判断（即使有行锁也要再判断一次 status）**：如果 task.status != TaskStatus.failed → 抛 ConflictException(code="TASK_NOT_RETRYABLE", message=f"Task {task_id} is in status '{task.status.value}', only 'failed' tasks can be retried. concurrent duplicate requests are not allowed.")（此时返回 409，spec 原文"仅 failed 状态可重试，需处理重复请求"）。
       ④ 真的可以重试：
          - 改 `task.status = pending`
          - 改 `task.current_stage_retry_count = 0`（阶段内自动重试计数清零，新的一轮自动重试有 3 次机会）
          - `task.total_retry_count += 1`（用户手动重试总次数 +1，统计用）
          - `task.error_message = None`
          - 同时更新 recording.last_status = pending（保持两表状态一致性，spec T9 验收标准 5）
          - session.flush()（写入事务，但还没 commit）
       ⑤ `await session.commit()`（提交事务，释放行锁。注意：先 commit 再 enqueue，因为 enqueue 成功不影响 DB；如果先 enqueue 成功了但 commit 因意外回滚，任务会塞到队列但 DB 还是 failed → 死锁/不一致！顺序必须先 commit 成功再 enqueue）。
       ⑥ **事务 commit 成功后**（不在事务里了，因为队列是内存的）才 `await pipeline.enqueue_task(task_id)`。如果 enqueue 时队列满阻塞了也没关系，反正是路由 handler await 阻塞直到成功，客户端超时 FastAPI 会返回 504 没关系。
       ⑦ return (task, True)。
2. **tasks.py 路由**：
   - router = APIRouter()
   - `@router.get("/{task_id}", response_model=TaskOut)`：调 service get → return `TaskOut.model_validate(task_obj)`。
   - `@router.post("/{task_id}/retry", response_model=TaskOut, status_code=200)`：调 service retry_task → return model_validate。
   - 响应码：retry 成功 200；status 不是 failed → 409（T2 的 ConflictException handler 会转）；task_id 不存在 → 404。
   - 日志：retry 成功打 WARNING `[tasks] manual retry task_id=%s total_retry_count now=%d`。
3. **行锁的并发正确性测试脚本**：后面写。

### 验收标准
1. **状态区分转写/摘要（processing 体现阶段）**：T9 阶段 1 mock 睡着时调 `GET /v1/tasks/{id}` → body.status == "transcribing"；阶段 2 调 LLM 时 status == "summarizing"。不是模糊的 "processing"（spec 原文明确要体现当前阶段，所以这点必须断言）。
2. **retry 成功路径**：找一个 failed 的 task（T9 测的强制失败那个就有）→ POST retry → 200，返回体.status=="pending"，current_stage_retry_count==0，total_retry_count==1；紧接着查 DB：tasks.status=pending，recordings.last_status=pending；pipeline 日志里看到这个 task_id 重新 enqueued 并进入转写。
3. **retry 幂等 1（非 failed 状态 → 409）**：对 status=done 的 task POST retry → HTTP 409，body.error.code=="CONFLICT"（或 TASK_NOT_RETRYABLE，你在 ConflictException 里设的 code）。对 pending / transcribing / summarizing 三个状态各测一次，全部 409。
4. **retry 幂等 2（双并发同 task → 409 第二次）**：写脚本 `scripts_tmp/test_retry_concurrency.py` 用 `asyncio.gather()` 同时发 2 个 POST retry 给同一个 failed task。**结果必须**：1 个返回 200（pending）+ 1 个返回 409（拿行锁晚的读到 status 已被改成 pending 了）。**不允许两个都 200**（那就说明行锁或乐观判断没生效，spec R5 挂，任务被入队两次跑两遍）。
5. **404 场景**：GET / POST 不存在的 task_id → HTTP 404，错误结构统一。

### 测试要求
- `scripts_tmp/test_retry_concurrency.py`：重点！用 asyncio.gather 两个 httpx 并发请求，断言 1×200 + 1×409。这个是面试时面试官大概率会手动发两个 curl 并行测的场景，一定要脚本验证过。
- `scripts_tmp/test_task_status_stages.py`：用 httpx 在状态机各阶段轮询 status，断言 5 个状态字符串出现顺序是 `pending → transcribing → summarizing → done`（失败路径则中间跳到 failed）。

### 依赖
T9（状态机真实运行，status 才会有 5 态）、T5（TaskOut schema）、T7（enqueue_task 函数）、T2（NotFoundException / ConflictException）。

### 不包含
1. 不写 recordings 相关路由（T11）。
2. 不改 pipeline 代码（T9 已经稳定了不要动）。
3. 不做自动重试（P1-1 已在 T9）。

---

## Ticket 11：Recordings 路由 — GET 列表分页 + GET 详情 + DELETE（级联删文件）

### 目标
实现 P0 必做剩下的 3 个 recordings 接口：① `GET /v1/recordings?page=&page_size=`（分页，倒序，每条含最新状态，Pydantic 泛型 PagedResponse）；② `GET /v1/recordings/{id}`（详情：status=done 才返回 transcript + SummaryOut 对象，否则两个字段 None）；③ `DELETE /v1/recordings/{id}`（先删 DB 两表，级联 tasks；再删本地文件静默失败；返回 204 No Content）。

### 背景
来自 **[spec §2.2 接口列表原文 3-6 条](file:///d:/code/xisiyun/spec.md#L83-L91)** + **[spec §3 P0-6 三个接口硬约束](file:///d:/code/xisiyun/spec.md#L240-L246)**。对应 spec §4.5 DELETE 边界（事务 vs 文件删除顺序 + 文件不存在静默）。

### 修改范围
- 修改：`app/api/v1/recordings.py`（T6 写了 POST 路由，本 ticket 加 3 个新 handler）
- 修改：`app/services/recordings.py`（加 3 个服务层函数）

### 任务
1. **services/recordings.py 加 3 个函数**：
   - `async def get_recording_list_paged(session: AsyncSession, page: int, page_size: int) -> Tuple[int, list[Recording]]`：
     ① `count = await session.scalar(select(func.count()).select_from(Recording))` → total。
     ② `stmt = select(Recording).order_by(Recording.created_at.desc()).offset((page-1)*page_size).limit(page_size)` → items。
     ③ 直接返回 (total, items)。分页 ORDER BY 的索引在 T3 建了，所以 offset 大的话性能也还可以（笔试项目数据量小，不用 cursor 分页）。
   - `async def get_recording_detail_or_404(session: AsyncSession, recording_id: str) -> Recording`：不存在抛 NotFoundException("Recording", id)。
   - `async def delete_recording_cascade(session: AsyncSession, recording: Recording) -> tuple[str, str]`：
     - ① **先查需要的信息**：拿 recording.storage_path（或 recording.id + recording.file_ext 拼），return 这两个信息（storage_path, id）给外层，外层在**事务提交成功后**调 storage.delete_file_if_exists。
     - ② 事务内：`await session.execute(delete(Task).where(Task.recording_id == recording.id))` → `await session.execute(delete(Recording).where(Recording.id == recording.id))` → commit。**级联删子表 Task 要先删（或者我们 ORM 配置了 ON DELETE CASCADE 就不用手动删 tasks，但 DELETE recordings 时 MySQL 会自动删 tasks，双保险无所谓）**；总之要保证 commit 后两张表都没数据。
     - 这个函数返回 (storage_path, id)，外层路由 handler 负责删文件（因为 service 层原则是只碰 DB，不碰磁盘 IO，spec §4.2 模块职责边界）。
2. **routes/recordings.py 加 3 个 handler**：
   - `@router.get("", response_model=PagedResponse[RecordingListItem])`：
     - 参数 `query: PaginationQueryParams = Depends()`（T5 PaginationQueryParams 带 Field(le=100) 自动拦上限）。
     - 调 service 拿 (total, items) → return `PagedResponse[RecordingListItem](total=total, page=query.page, page_size=query.page_size, items=[RecordingListItem.model_validate(r) for r in items])`。
   - `@router.get("/{recording_id}", response_model=RecordingDetailOut)`：
     - 调 service 拿 recording 对象。
     - 转成 Pydantic：`base = RecordingDetailOut.model_validate(recording)` 但 base 里的 summary 字段默认是 None（因为 ORM 的 summary_json 是 dict，我们要手动转 SummaryOut）。
     - 所以手动补：`if recording.last_status == TaskStatus.done and recording.summary_json is not None: try: valid = LLMSummaryResponse.model_validate(recording.summary_json); base.summary = SummaryOut(**valid.model_dump()); base.transcript = recording.transcript except ValidationError as e: logger.warning(f"recording {id} summary_json invalid even status done: {e}"); base.transcript = recording.transcript  # 摘要坏了但 transcript 还能用`。如果 status != done，即使 DB 里有 summary_json 也不返回（符合 PDF"若处理完成则包含"语义）。
     - return base。
   - `@router.delete("/{recording_id}", status_code=204)`：
     - r = service.get_recording_detail_or_404(id)。
     - `(storage_path, rid) = await service.delete_recording_cascade(session, r)` → DB 事务提交成功返回。
     - **事务提交成功后再删磁盘文件**（spec §4.5 DELETE 顺序要求，避免"删了文件但 DB rollback"不一致）：`await storage.delete_file_if_exists(storage_path)`。
     - 函数不返回任何 body（204 No Content 标准语义，FastAPI 自动不处理 response_model）。
3. 日志：DELETE 成功打 WARNING `[recordings] deleted recording_id=%s (file_path=%s, size=%dB)`，方便审计。

### 验收标准
1. **分页正确性**：脚本批量 POST 上传 25 个不同的假 wav（不同 MD5，每个 1KB 就够）→
   - `GET /v1/recordings?page=1&page_size=10` → total=25、page=1、page_size=10、items 长度 10。
   - `GET /v1/recordings?page=3&page_size=10` → items 长度 5。
   - `GET /v1/recordings?page_size=101` → **422**（Field le=100 拦截，spec §4.5 分页恶意参数边界）。
   - 排序：每页 items 的 created_at 是严格递减（倒序）。
   - 每个 item 都有 last_status 字段，类型是 5 态字符串之一。
2. **详情字段控制**：
   - 传一个刚上传 status=pending 的 id → body 里没有 transcript、summary 两个键或值是 null（JSON 解析出来二者都行，只要不是真值）。
   - 传一个 status=done 的 id → 有 transcript 字符串（长度 ≥ 500）、有 summary 对象（三个键齐全，key_points≥1）。
   - 手动把 done 的 recording.summary_json 改成无效 JSON（或 key_points=[]）再 GET detail → transcript 字段**有值**（即使摘要坏了转写结果还是有用的），summary 字段是 null 或不返回，日志里有一条 WARNING 级别的 "summary_json invalid even status done"（不崩是关键，spec 边界）。
3. **DELETE 级联正确性**：
   - 传一个 done 的 id → DELETE → HTTP 204（body 空）。
   - 立即再 GET detail → 404。
   - 立即 `SELECT COUNT(*) FROM tasks WHERE recording_id='xxx'` → 0 行（ON DELETE CASCADE 生效，spec R10）。
   - 立即看 `uploads/` 目录 → **物理文件已删除**。
4. **DELETE 边界：磁盘文件早被手动删了**：手动 `rm uploads/xxx.wav` → 再调 DELETE 接口 → **还是返回 204 无报错**（不是 500！spec §4.5 DELETE 边界 missing_ok=True）。
5. **404 场景**：不存在的 id GET 或 DELETE → 404，结构统一。

### 测试要求
- `scripts_tmp/test_recordings_list_paging.py`：生成 25 条小文件上传，断言分页总数、每页数量、排序、page_size 上限。
- `scripts_tmp/test_delete_cascade_missing_file.py`：手动删文件后再 API 删，断言 204。
- 这两个脚本 + T10/T6 的临时脚本，T13 全部 pytest 化（不用再新写逻辑，改写成 test_ 函数 + fixture）。

### 依赖
T6（已有 POST 路由/response_model）、T5（PagedResponse/RecordingDetailOut/SummaryOut）、T9（recordings.last_status = done / pending 等状态才存在，才能断言字段控制）、T3（ON DELETE CASCADE）、T4（storage.delete_file_if_exists）。

### 不包含
1. 不写自动化测试（T12 搭框架，T13 写用例）。
2. 不改 pipeline / llm 代码。
3. 不写 README（T15）。

---

## Ticket 12：测试框架搭建 — pytest conftest fixtures（SQLite 内存库 + AsyncClient + Mock LLM）

### 目标
写 `tests/conftest.py` 搞定 pytest 全链路基础设施：① SQLite 内存异步测试数据库（不污染你本机 MySQL，速度快）；② `httpx.AsyncClient(transport=ASGITransport(app))` 测试客户端（Depends override 把 get_session 换成测试 session）；③ 把 pipeline 的异步 sleep / LLM 真实调用全部 Mock 掉（让 T13 用例 ≤ 5 秒全跑完，不 sleep 15 秒、不消耗 DeepSeek 额度）；④ uploads 测试目录临时隔离（每个用例前新建临时目录、用例后清空）。

### 背景
T13 要写 5 个核心集成测试，全部依赖 conftest 的 fixture。对应 **[spec §3 P1-6 测试](file:///d:/code/xisiyun/spec.md#L259-L260)**。

### 修改范围
- 新建：`tests/__init__.py`、`tests/conftest.py`
- 新建：`pytest.ini` 或 `pyproject.toml`（配置 pytest-asyncio mode=auto、testpaths）

### 任务
1. **pytest 配置文件**（二选一）：
   ```ini
   # pytest.ini
   [pytest]
   asyncio_mode = auto
   testpaths = tests
   python_files = test_*.py
   log_cli = true
   log_cli_level = INFO
   ```
2. **conftest.py 核心 fixture（按依赖顺序写，注意 scope）**：
   - `@pytest.fixture(scope="session")` → `event_loop()`（asyncio loop，session 级，所有用例共用一个 loop 加速）。
   - `@pytest.fixture(scope="function")` → `test_settings()`：return Settings 覆盖字段 — `database_url = "sqlite+aiosqlite:///:memory:"`（需要额外装 `aiosqlite`，在 requirements.txt **追加一行** `aiosqlite>=0.20,<1`）；`storage_upload_dir = Path("./tmp_test_uploads")`；`max_concurrent_tasks = 2`；`pipeline_max_auto_retries = 1`（测试用只要 1 次重试快）；`deepseek_api_key = "sk-test-dummy"`；`deepseek_timeout_seconds = 3`。
   - `@pytest.fixture(scope="function")` → `test_engine(test_settings)`：用 SQLAlchemy 创建**测试 SQLite 异步 engine**，`Base.metadata.create_all(...)`（因为 SQLite 内存库，每次 create 都是空，所以 function 级建表）；yield engine；测试后 `Base.metadata.drop_all(...)` + engine.dispose。
   - `@pytest.fixture(scope="function")` → `db_session(test_engine)`：创建 `async_sessionmaker`，yield session，用例后 rollback + close。
   - `@pytest.fixture(scope="function")` → `clean_uploads(test_settings)`：用例前 `shutil.rmtree(test_settings.storage_upload_dir, ignore_errors=True)` + `mkdir(parents=True, exist_ok=True)`；用例后再删一次，保证干净。
   - `@pytest.fixture(scope="function")` → `override_dependencies(db_session, test_settings, clean_uploads)`：用 FastAPI `app.dependency_overrides[get_session] = lambda: db_session`；另外我们的 pipeline.py / llm.py / storage.py 很多地方是模块级直接读 `settings = Settings()`，所以还要用 `monkeypatch.setattr("app.services.pipeline.settings", test_settings)` + monkeypatch.setattr 所有直接 import settings 的模块，保证读的是测试配置。
   - **核心 Mock 1：跳过 pipeline 的 sleep + mock 转写 100% 成功 且 0 耗时**：`monkeypatch.setattr("app.services.pipeline.random.uniform", lambda a,b: 0)` → mock 不 sleep；`monkeypatch.setattr("app.services.pipeline.random.random", lambda: 0)` → mock 永远不触发 20% 失败（要测失败场景时，在具体用例里再临时 mock 成 1.0 强制失败）。
   - **核心 Mock 2：LLM 不发真实 HTTP，直接返回固定 dict**：`monkeypatch.setattr("app.services.llm.summarize_transcript", AsyncMock(return_value={"summary":"Mock summary","key_points":["Point 1","Point 2"],"todos":["Todo 1"]}))`（用 pytest-asyncio 的 AsyncMock）。
   - `@pytest.fixture(scope="function")` → `client(override_dependencies, clean_uploads)`：最重要 fixture — 启动测试 worker 协程（pipeline.start_workers），但要用 override 后的 settings；然后 `transport = ASGITransport(app=app)` → `async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac: yield ac`；测试后 shutdown_workers。
3. **requirements.txt 追加**：`aiosqlite`（SQLite 异步驱动），如果 T1 没写 pytest-mock 就加 `pytest-mock`（但内置 monkeypatch 够用）。

### 验收标准
1. **fixture 能跑通**：临时写 `tests/test_smoke_fixtures.py` 只有一个用例 `async def test_smoke(client): r = await client.get("/health"); assert r.status_code == 200` → 执行 `pytest tests/test_smoke_fixtures.py -v` → 用例通过，执行时间 < 5 秒（没有真 sleep，没有真 HTTP）。
2. **隔离性验证**：在上面的 smoke 用例里用 `db_session` 手动 INSERT 一条假 recording，`await db_session.commit()` → 用例跑完后下一个用例再查 `SELECT count(*) FROM recordings` → 0 行（function 级 create_all/drop_all 生效，测试之间不共享 DB 数据）。
3. **uploads 目录隔离**：第一个用例 POST 传 1 个假文件 → 看 tmp_test_uploads 里有；第二个用例开始前文件夹是空的，或只有当前用例产生的文件。
4. **LLM 不发真请求**：看 tests 执行时 logs/app.log 里**没有任何 `[llm] summarize` 日志**（因为 monkeypatch 直接把函数替换成 AsyncMock 了，根本不进 llm.py 内部逻辑）。
5. **pytest 配置生效**：`pytest --collect-only` 只收集 tests/ 下的 test_ 函数，不收集 scripts_tmp/ 里的临时脚本。

### 测试要求
自己就是测试框架本身，所以用上面 1. 的 `test_smoke_fixtures.py` 验证就够。这个用例**后面可以保留**，作为 T13 5 个用例之外的"环境校验"。

### 依赖
P0 全部完成（T1~T11 所有接口/模块都能用，fixture 才能 import 到正确的对象 override）。具体 T1/T2 配置结构、T3 ORM Base、T6 upload 路由。

### 不包含
1. 不写任何业务集成测试用例（T13 写 5 个）。
2. 不修改业务逻辑代码（只改 requirements.txt 追加 aiosqlite，其他业务不动）。

---

## Ticket 13：核心集成测试 — 5 个 P1-6 必过用例

### 目标
把 T4/T6/T9/T10/T11 所有临时脚本 `scripts_tmp/test_*.py` 改写成 **pytest 标准格式** `tests/test_api.py`，共 5 个测试函数（对应 spec §3 P1-6 列表），全部跑通 ≥ 70% 核心代码覆盖率。外部行为断言（不 mock 内部 DB 查询细节，只看 HTTP 响应+最终 DB 状态+最终文件系统状态，符合"只测外部行为"要求）。

### 背景
spec §3 P1-6 加分项，T12 建好 fixture 后一次性写完。

### 修改范围
- 新建：`tests/test_api.py`
- 不修改业务代码。如果发现业务代码有 fixture 覆盖不到的 bug（比如 retry 行锁没生效），**回到对应的原 ticket 模块改代码，单独做一个 hotfix commit，不要混在本 ticket 里**。

### 任务
写 5 个 async def 测试函数，全部使用 T12 的 `client: AsyncClient` + `db_session: AsyncSession` fixture：

1. **`async def test_upload_success_returns_pending_creates_db_and_file(client, db_session, tmp_path)`**（对应 P0-3 上传）：
   - 准备：二进制假 wav 内容 `b"RIFF...."` 或随便 1024 字节随机。
   - POST `/v1/recordings` files={"file": ("hello.wav", content, "audio/wav")}。
   - 断言：HTTP 200；json 三字段齐全且 status="pending"。
   - 断言 DB：recordings 表 count=1，tasks 表 count=1；tasks.status=pending。
   - 断言文件：tmp uploads 目录里 `glob("*.wav")` 有 1 个文件，大小 == len(content)。

2. **`async def test_upload_idempotent_by_md5_same_content_different_names(client)`**（对应 P1-4 上传幂等）：
   - 生成 `content = os.urandom(2048)`。
   - 第一次传 name=`a.wav` → 拿 `id1 = resp.json()["recording_id"]`。
   - 第二次传 name=`b.wav`、相同 bytes → `id2 = resp.json()["recording_id"]`。
   - 断言：`id1 == id2`；DB recordings 表 count=1；uploads 目录文件数 = 1（没有两个文件）。

3. **`async def test_pipeline_full_success_mock_path(client, db_session)`**（对应 P0-5 全流程状态机）：
   - T12 fixture 已经 mock sleep=0 + LLM AsyncMock 返回固定 JSON；本用例依赖这个 Mock 不真睡。
   - 上传成功拿 task_id，等一小段时间（`await asyncio.sleep(0.3)` 足够让 SQLite session 提交 + pipeline worker 跑一轮）。
   - `GET /v1/tasks/{task_id}` → assert status=="done"（因为全 mock 没 sleep，0.3 秒内状态肯定跳到 done）。
   - `GET /v1/recordings/{rid}` → assert transcript is not None；assert "summary" in resp.json() 且 resp.json()["summary"]["key_points"] == ["Point 1","Point 2"]（和 T12 Mock 返回的一致）。
   - 额外断言 last_status 两表一致：`SELECT tasks.status FROM tasks ...` == `SELECT recordings.last_status FROM recordings ...`。

4. **`async def test_retry_409_on_non_failed_task(client, db_session)`**（对应 T10 幂等 409）：
   - 上传成功 → 立即 `POST /v1/tasks/{task_id}/retry` → 断言 HTTP 409（status=pending 不能重试）。
   - 手动 DB `UPDATE tasks SET status='done', recordings.last_status='done' WHERE id=...` → 再 retry → 还是 409。
   - 再 DB `UPDATE tasks SET status='failed'` → 再 retry → HTTP 200，status 变 pending，total_retry_count = 1。
   - 紧接着再 POST 一次 retry（串行，这个不需要并发用并发断言）→ 因为刚才 retry 后 status 已经 pending → 409 正确。

5. **`async def test_delete_cascades_everything_db_file(client, db_session)`**（对应 T11 DELETE 级联 + missing_ok）：
   - 上传成功拿到 rid，上传文件大小 > 0。
   - DELETE `/v1/recordings/{rid}` → 204 无 body。
   - GET 同 rid → 404。
   - DB recordings count=0 **AND** tasks count=0（级联子表删除，T3 ON DELETE CASCADE）。
   - `list(tmp_uploads.glob("*"))` → 0 个文件（物理文件被删）。
   - **边界（delete 幂等）**：再 DELETE 同一个 rid → 404（id 不存在），不是 500。
   - **边界（文件被手动删后再 API 删）**：再传一个新文件拿 rid2，手动 `shutil.rmtree(uploads_dir)` 清空 → DELETE rid2 → HTTP 204，**不抛 500**（符合 spec §4.5 DELETE missing_ok）。

### 验收标准
1. 全绿：`pytest tests/test_api.py -v` → **5 passed**。
2. 速度：总耗时 < 10 秒（SQLite 内存 + 全 Mock，sleep=0）。
3. 覆盖率：`pip install pytest-cov` → `pytest --cov=app --cov-report=term-missing tests/` → 行覆盖率 **≥ 70%**（不强求具体数字，但至少 app/services/pipeline.py、app/api/*、app/services/llm.py 这几个核心文件覆盖率要 ≥ 60%）。
4. 独立可重复：连续跑 5 次 `pytest`（`pytest tests/ --count=5` 装个 pytest-repeat 或手动 for 循环跑 5 次）→ **全部通过 5/5**，证明没有 flaky test（因为用 function 级 fixture 完全隔离，并发不会串）。
5. 外部行为-only 原则：**测试函数内部没有出现任何 `session.execute(select(ORM 具体字段细节))` 以外的 DB 查询（比如不直接查 DB 断言 pipeline 阶段乐观锁 rowcount 的值，只断言最终 status 对）**，或者只通过 HTTP 接口断言 + 最终 count(*) / files exists 这样的外部状态。

### 测试要求
自己就是测试。跑完把 5 passed + ≥70% 的终端输出截图留到 docs_tmp/ 方便 README 吹一下。

### 依赖
T12（conftest fixtures）、T1~T11 全部 P0 功能稳定。

### 不包含
1. 不修复业务代码 bug（如果有 bug，回到原 ticket 修，本 ticket 只写断言）。
2. 不写性能测试 / 压力测试。
3. 不写 e2e 真实 LLM 调用测试（T8 已经用临时脚本测过真调用，这里全 mock 就行，不然消耗额度 + 慢）。

---

## Ticket 14：API 调试文件 — `api_test.http` 9 大场景块

### 目标
写根目录 `api_test.http`（JetBrains PyCharm/IDEA 直接点绿箭头跑，VS Code 装「REST Client」插件也能跑），覆盖 spec §5 L1 本地手动冒烟的 9 个场景。全部使用 `@name` 命名块，且第 2 块上传成功的 recording_id/task_id 自动用 `{{...}}` 变量注入后面的块，做到**按顺序从第一块点到最后一块，不用手动复制粘贴 id**。

### 背景
对应 **[spec 提交项 4 可导入 API 调试文件](file:///d:/code/xisiyun/spec.md#L11-L15)** + **[spec §5 L1 本地手动冒烟 9 块](file:///d:/code/xisiyun/spec.md#L505-L520)**。

### 修改范围
- 新建：`api_test.http`
- 根目录下，commit 进 git（不要放 .gitignore）

### 任务
1. 文件顶部注释说明怎么用：
   ```http
   ### =====================================================
   ### 录音转写服务 API 调试脚本（JetBrains .http 格式 / VS Code REST Client）
   ### 使用：
   ###   1. 启动服务：uvicorn app.main:app --port 8000
   ###   2. PyCharm 直接点击每个块左侧 ▶ 绿色箭头运行
   ###   3. VS Code 装 REST Client 插件，点击 "Send Request"
   ### 顺序：从上往下依次点 ▶（后面的块依赖第 2 块的 {{recording_id}} 变量）
   ### =====================================================
   @baseUrl = http://localhost:8000
   ```
2. 按 L1 9 个场景写块（每个块是一个 HTTP 请求，块之间用 `###` 分隔）：
   - **`### 0. Health Check`**：`GET {{baseUrl}}/health` → 断言 200。
   - **`### 1. 上传合法 wav（保存 recording_id 和 task_id）`**：`POST {{baseUrl}}/v1/recordings`，Content-Type multipart/form-data，`file=@./scripts_tmp/test_assets/sample.wav`（如果没 sample wav，就随便放一个 1KB 假文件占位在 scripts_tmp/test_assets/ 里，和脚本一起 commit）。**核心：响应后把变量注入**（JetBrains .http 语法）：
     ```
     > {%
        client.global.set("recording_id", response.body.recording_id);
        client.global.set("task_id", response.body.task_id);
        client.log("Saved: recording_id=" + recording_id + ", task_id=" + task_id);
     %}
     ```
     （VS Code REST Client 语法是 `@recordingId = {{response.body.$.recording_id}}` 类似写法，两种写法都在注释里说明一下兼容性，或者写两套）。
   - **`### 2. 上传 51MB 超大文件（返回 413）`**：POST 文件 `scripts_tmp/test_assets/51mb_fake.mp3`（T4 生成的那个 51MB 假文件，commit 时 gitignore 掉大于 100MB 的，这个 51MB 可以用 `fsutil file createnew` 生成，README 里告知生成命令），预期 413。
   - **`### 3. 上传 txt 非音频扩展名（返回 415）`**：POST `scripts_tmp/test_assets/wrong.txt`（随便 echo 一句话建的小文件，commit 进仓），预期 415。
   - **`### 4. 同 MD5 文件第二次上传（幂等：返回和第 1 块相同 recording_id）`**：POST 跟第 1 块完全相同的 sample.wav 文件，预期返回 200 + 第 1 块相同的 recording_id（手动对比或 assert）。
   - **`### 5. 查询任务状态（上传后 0~5 秒应该是 pending 或 transcribing）`**：`GET {{baseUrl}}/v1/tasks/{{task_id}}`。
   - **`### 6. 30 秒后再查任务状态（应该是 summarizing/done 或失败）`**：同 GET，间隔时间用户自己把握（T9 Mock 5~15s + LLM 30s timeout 内就出结果）。
   - **`### 7. GET 录音详情（status=done 时会有 transcript 和 summary 字段）`**：`GET {{baseUrl}}/v1/recordings/{{recording_id}}`。
   - **`### 8a. 先手动 SQL 把任务改成 failed → POST retry 成功 → 200 pending`**：
     先用注释说明「执行前请先 MySQL 执行 `UPDATE tasks SET status='failed', error_message='manual force failed for test' WHERE id='{{task_id}}'`」（或者再加一个临时内部调试接口？不行 spec §6 不做额外接口，就写注释告知用户手动执行 SQL），然后 `POST {{baseUrl}}/v1/tasks/{{task_id}}/retry`。
   - **`### 8b. 紧接着第二次 retry（幂等：返回 409）`**：立即再 POST 同 retry，预期 409。
   - **`### 9a. DELETE recording（返回 204）`**：`DELETE {{baseUrl}}/v1/recordings/{{recording_id}}`，204。
   - **`### 9b. 再 GET recording（返回 404）`**：GET，404。

3. 建 `scripts_tmp/test_assets/` 目录，加 sample.wav（1KB 假的 wav header + 填零字节就行，满足扩展名校验）、wrong.txt（1 行 hello world 文字），两个小文件 commit 进仓。51MB 的大文件放 .gitignore，README 里教怎么生成。

### 验收标准
1. **PyCharm 环境**：在 PyCharm Professional 里打开 api_test.http → 按顺序从第 0 块点到 9b 块 → **全程不需要手动复制粘贴 id**（8a/9a 都用了 {{task_id}}/{{recording_id}} 变量）。
2. **第 4 块幂等断言**：第 4 块返回 JSON 的 recording_id == 第 1 块保存的 {{recording_id}}。
3. **错误状态码正确**：第 2 块 413 / 第 3 块 415 / 第 8b 块 409 / 第 9b 块 404 — 全部命中。
4. **README 交叉对应**：README.md（T15 写）里的 curl 示例 3 条（上传/查询/删除）能对应到本文件第 1/7/9a 三块的请求内容。
5. **文件体积**：api_test.http 纯文本 < 30KB，git 不报错。

### 测试要求
本 ticket 产物就是调试文件本身，人工"按顺序点一遍"就是验收。

### 依赖
T1~T11 全部 P0 模块可用（真的要能跑通接口）；T6 上传接口；T10 GET tasks/retry；T11 GET list/GET detail/DELETE。

### 不包含
1. 不做 Postman Collection JSON 导出（.http 是通用格式，Postman 也支持导入 .http）。
2. 不做单独 curl 脚本文件（README 里贴示例，spec Q11 + c 的说明）。

---

## Ticket 15：交付文档收尾 — README（架构图 + 运行步骤 + 技术取舍 + 已知问题）+ docker-compose.yml（MySQL 备选）

### 目标
写最终交付 README.md（对应 **[spec 提交项 2 README 六项要求](file:///d:/code/xisiyun/spec.md#L11-L15)**，全部覆盖）+ 根目录 `docker-compose.yml`（只起 MySQL 8 容器，给没装本机 MySQL 的读者备用，虽然你本机装了但要符合"一键启动"备选方案）+ 清理 `scripts_tmp/` 目录注释说明。

### 背景
spec 提交项 1~4 全靠 README、api_test.http、git commit history 支撑。对应 **[spec §7 R1 commit 历史 10 步第 9/10 笔](file:///d:/code/xisiyun/spec.md#L656-L677)**。

### 修改范围
- 新建：`README.md`、`docker-compose.yml`
- 修改：`.gitignore`（忽略 51MB 测试资产、临时脚本目录的 pyc）
- 修改：`scripts_tmp/README.md`（说明这些脚本是开发时临时用，正式测试在 tests/ 下），避免别人误会 scripts_tmp 是交付物

### 任务
1. **README.md 严格 8 节结构（少一节扣分）**：
   - **# 录音转写服务 API（后端实习生笔试题）**：一句话简介 + 技术栈 badge。
   - **## 🚀 运行方式（含 3 条一键命令）**：
     - 环境要求：Python 3.10、本机 MySQL 8（或 docker compose up -d 起 MySQL）。
     - Step 1：建库命令（一行复制粘贴）。
     - Step 2：`cp .env.example .env` → 填 2 个必填项（MYSQL_PASSWORD / DEEPSEEK_API_KEY），其他默认，**所有可配置参数列表列一张表和 spec §七一致**。
     - Step 3：`pip install -r requirements.txt`。
     - Step 4：`alembic upgrade head`。
     - Step 5：`uvicorn app.main:app --reload --port 8000 --limit-max-request-size 52428800`（最后这个参数是 50MB，spec §4.5 R6 双保险）。
     - 备选 Docker 路径：没有本机 MySQL 就先 `docker compose up -d mysql`，然后步骤同上 .env 里 MYSQL_HOST=127.0.0.1 / port 3306（docker-compose.yml 端口映射 3306）。
     - **一键启动验证**：启动完成后 `curl http://localhost:8000/health` 返回 ok。
   - **## 🧱 架构 / 流程图（Mermaid）**：直接抄 **[spec §4.1 Mermaid 架构图](file:///d:/code/xisiyun/spec.md#L292-L325)**（复制粘贴就行，保证内容一致）。
   - **## 🗄️ 表结构设计说明**：两张表 recordings / tasks 的字段表，和 **[spec §4.4 数据结构设计](file:///d:/code/xisiyun/spec.md#L425-L496)** 完全一致，列清楚 4 个关键索引、外键 ON DELETE CASCADE、file_hash UNIQUE 的设计原因。
   - **## 🎯 技术取舍（面试会问）**：每条 2~3 句话，讲清楚"Why not other"：
     1. 为什么选 asyncio Queue + DB 持久化（方案 B）而不是 Celery/BackgroundTasks？（答：平衡了"重启恢复""并发控制"两个加分项的实现成本，且一键启动友好；BackgroundTasks 无内建队列恢复难，Celery 要起 Redis+Worker 运维重）。
     2. 为什么选 MySQL 本机服务而不是 SQLite/Docker 全容器？（答：用户环境已有 MySQL，开发热重载比 Docker build 快；Alembic + SQLAlchemy 方言层换 SQLite 只需改一行 .env，迁移成本极低）。
     3. 为什么 LLM 输出要做「response_format + json.loads + Pydantic validate」三层保险？（答：PDF 明确要求"必须处理返回内容不符合预期格式"；response_format 是概率保险 + loads 是语法保险 + validate 是 schema 保险，三层叠加故障率最低）。
     4. 为什么上传接口是「先写磁盘后写 DB」不是相反？（答：写磁盘失败不用 rollback 事务，DB 层不会产生脏数据；写 DB 失败只要删刚写的磁盘文件，删是幂等操作，一致性风险低）。
   - **## 🧪 测试方式**：
     - 手动调试：api_test.http 按顺序点（对应 §5 L1 9 块）。
     - 自动化集成测试：`pytest tests/test_api.py -v` 5 个核心用例全绿；`pytest --cov=app tests/` 覆盖率 ≥70%。
     - 核心 curl 示例（3 条）：POST upload / GET task status / DELETE — 对应 spec §6 Q11 选项 c。
   - **## ✅ 完成情况对照题目**：
     - P0 必做 6 大项：每项 ✅ 并说明在哪（接口列表/状态机在 T9）。
     - P1 加分项 7 个：1、2、4、5、6 打 ✅，3、7 打 ❌ 并**明确说明原因**（3 流式 SSE：未做，复杂度高边际效益低，笔试时间有限；7 公网部署：未做，无云服务器，如需部署可提供 Dockerfile）。
   - **## ⚠️ 已知问题 / 未完成项（诚实列出来加分）**：
     1. 未实现 LLM SSE 流式摘要。
     2. 未部署公网访问地址。
     3. 上传接口的"客户端幂等键"Header（`X-Idempotency-Key`）未做，当前仅基于文件 MD5 内建幂等。若客户端需要不同内容但逻辑上视为同一文件的幂等，需扩展该 Header 字段逻辑（当前不影响 P0 + P1-4 评分）。
     4. 任务历史记录：目前设计「一个 recording 只关联一个 task」，POST retry 时是 in-place 改 task.status 不是新建 task 行。如果需要保留每次 retry 的独立历史快照，要把 tasks 表改成 1:N + archive 表（目前未做，因为 PDF 没要求任务历史）。
   - **## 📦 交付物清单自检打勾**：把 spec §1 交付物 4 条列成 checklist，每一条前面打 ☑️。
   - **## 🧭 目录结构**：树形图列 app/ tests/ migrations/ uploads/ logs/ api_test.http 等，和 spec §六目录结构一致。
2. **docker-compose.yml（只起 MySQL，备选方案）**：
   ```yaml
   version: "3.8"
   services:
     mysql:
       image: mysql:8.0
       container_name: xisiyun_mysql
       ports: ["3306:3306"]
       environment:
         MYSQL_ROOT_PASSWORD: ${MYSQL_PASSWORD:-root}
         MYSQL_DATABASE: xisiyun_asr
         MYSQL_CHARSET: utf8mb4
       command: ["--character-set-server=utf8mb4", "--collation-server=utf8mb4_unicode_ci"]
       volumes: ["xisiyun_mysql_data:/var/lib/mysql"]
       restart: unless-stopped
   volumes:
     xisiyun_mysql_data:
   ```
   注释说明：本 compose 仅用于无本机 MySQL 场景；**FastAPI 不在容器内跑**，在本机 `uvicorn` 跑，这样改代码热重载方便（符合 Q5 b1 推荐）。
3. **scripts_tmp/README.md**：一句话说明这个目录是开发时的临时验证脚本，正式 pytest 集成测试是 tests/ 下的那 5 个；不要删这个目录，里面的脚本是面试时快速演示用的。
4. **检查一遍 git commit 历史**（此条也算任务项，spec R1）：从 T1 到 T14，每个 ticket 一笔 commit，共 14~15 笔，message 格式统一 `feat: xxx / test: xxx / docs: xxx` 这种 Conventional Commits 风格；**绝不允许最后还有「Initial commit」一笔全仓代码**，那样直接扣 R1 分。如果之前开发时混了太多小 commit，用 interactive rebase  squash 成 15 笔（但不要用 rebase -i 改已经推到远程的 commit，推了就多几笔无所谓，只要不是一笔就好）。

### 验收标准
1. **README 八节齐全**：按 1. 的八节标题逐一核对，一节不少（特别是「技术取舍」和「已知问题」诚实写比空着加分）。
2. **运行步骤可复制粘贴**：在一台全新 Windows 电脑（只有 Python 3.10 和 MySQL 8，其他没装）上，严格按 README Step 1 复制到 Step 5，复制粘贴不做任何修改 → 最后 curl health 返回 ok；api_test.http 第 0~9b 块全绿通过 ≤ 1 小时（含第一次 pip install 时间）。
3. **Mermaid 渲染**：GitHub/Gitee 上的 README.md 里，架构图和流程图正确渲染（因为 GitHub 已原生支持 ```mermaid 语法），不是显示代码块。
4. **加分项完成情况诚实**：明确写清楚 3（SSE）和 7（公网）未做，理由充分。面试官看到你会主动做「完成度对齐」，比藏着掖着被追问出来好很多。
5. **commit 历史检查**：`git log --oneline -n 20` 输出 ≥ 10 笔，没有任何 `commit all` / `init` 之类一大坨 message，全部 Conventional Commits 风格（feat/docs/test/chore/refactor）。
6. **docker-compose 备选**：没装本机 MySQL 的情况下，`docker compose up -d` → `docker ps` 看到 xisiyun_mysql 容器 running；改 .env MYSQL_PASSWORD=root → alembic upgrade head 能连上建表。

### 测试要求
本 ticket 是文档交付，验收标准 1~6 全人工核查；代码不产生任何业务改动，不需要 pytest。

### 依赖
**所有 T1~T14 全部完成并通过验收**。特别是 T13 pytest 5 个用例全绿、T14 api_test.http 全绿后再写 README 里的完成情况和测试方式。

### 不包含
1. 不写 Dockerfile 打 FastAPI 镜像（spec §6 不做容器化部署，除非公网部署但公网不做），只提供 MySQL 的 docker-compose 备选。
2. 不写 Postman Collection JSON（Q11 已选 a + c，README curl 示例 + api_test.http 足够）。
3. 不重新改业务代码，发现 bug 回到原 ticket 修，本 ticket 只写文档 + 整理 git。

---

## 需要确认的问题

> 以下为拆 tickets 过程中发现的 spec 小缺口，不需立即回答，可以在实现到对应 ticket 时再确认或自行按推荐值处理：
>
> 1. **T9 `_random_transcript()` 生成函数**：spec 写「固定或随机文本均可」，推荐是内置一段 2000 字固定的中文长文（避免引入 `python-lorem` 新依赖），无需确认，按此实现。如你想要随机句子拼接，在 T9 开发前告诉我一声，否则默认固定。
> 2. **T10 ConflictException code 字符串**：建议 TASK_NOT_RETRYABLE（比笼统的 CONFLICT 更语义化），默认按此实现。
> 3. **T15 commit 历史 squash**：如果开发中产生 30+ 笔细碎 commit（比如每跑一个脚本就 commit），是否允许在推远程前用 `git rebase -i` 压缩成约 15 笔（和 tickets 数对齐）？还是保留细碎 commit 原样推？**推荐 squash 成 15 笔 Conventional Commits 风格更清楚**。没回复就默认推荐。
> 4. **T6 上传接口响应码**：PDF 原文没写成功是 200 还是 201。按 REST 惯例 POST 创建资源一般是 201，但 PDF 示例 JSON 没标。**推荐 200**（和 PDF 原文示例的上下文一致，PDF 没说要改响应码）。如你偏好 201，在 T6 开始前告知。
> 5. **T11 DELETE 响应体**：PDF 原文没定，业界惯例 204 No Content（无 body） vs 200 OK + `{deleted:true}`。**推荐 204**（更标准，FastAPI 对 204 的 response_model 处理也自动忽略）。如偏好 200 告知。
