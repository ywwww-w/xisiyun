# 录音转写服务 API — 可执行开发 Spec

> 版本：v1.0 | 更新：2026-09-10 | 状态：待用户确认后进入实现
>
> 本 Spec **仅描述要做什么、怎么做、怎么验证，不包含任何实现代码**。确认无误后再进入编码阶段。

---

## 1. 题目目标

用 **Python + FastAPI + MySQL** 在 4 个自然日内，独立交付一个可一键本地运行的「录音上传 → 异步 Mock 转写 → 真实 LLM 结构化摘要 → 任务状态/结果查询」后端服务。

**最终交付物**（PDF 明确要求，少一项就会扣分）：

1. GitHub/Gitee 仓库（**保留完整 commit 历史，禁止一次性提交**）
2. `README.md`：运行方式、架构/流程图（Mermaid）、表结构设计说明、技术取舍、已知问题/未完成项
3. **一键启动方式**（本机 MySQL 已装前提下，`uvicorn app.main:app --reload` + `alembic upgrade head` 即可跑）
4. 可导入的 API 调试文件：`api_test.http`（JetBrains / VS Code REST Client 格式）+ README 内核心接口 curl

**判分优先级**（PDF 明示）：

| 级别 | 内容 | 交付权重 |
|---|---|---|
| P0 必做（错一项无法通过） | 6 个接口全通 + 状态机正确流转 + 统一错误 + 日志 + 迁移脚本 + 一键启动 | 60% |
| P1 加分（做任意 1 项显著加分，最多 5 项） | 自动重试 + 重启恢复 + 上传幂等 + 并发控制 + 测试 | 30% |
| 工程规范 | 目录分层合理、README 清楚、commit 粒度合理 | 10% |
| 明确不考察 | 鉴权、前端、高并发性能优化（正确性优先） | 0%（花时间 = 浪费） |

---

## 2. 已知信息

> 本项目从零开始，**无任何已有代码/测试/入口文件**。以下信息均来自题目 PDF（`后端实习生笔试项目.pdf`）与 11 项共同决策确认。

### 2.1 已确认的技术栈（不可更改）

| 层 | 选型 | 决策编号 |
|---|---|---|
| 语言 / 框架 | Python 3.10 + FastAPI 0.110+ | Q1 |
| 数据库（服务） | 用户本机已安装 MySQL 8.x，端口 3306 | Q2 + 用户补充 |
| DB 访问层 | SQLAlchemy 2.0 async + asyncmy 异步驱动 + Alembic 迁移 | Q5a |
| LLM 供应商 / 模型 | DeepSeek `deepseek-chat`（API Key 用户已有） | Q3 / Q7 |
| LLM 输出约束 | `response_format=json_object` + 代码层 `json.loads` 兜底 | Q7c1 |
| LLM 超时 | **30 秒**，全部参数写入 `.env` 可随时改 | Q7 |
| 异步任务架构 | `asyncio.Queue`（内存队列）+ `asyncio.Semaphore(3)`（并发控制）+ DB 持久化状态 + startup hook 扫描恢复 | Q4 方案 B |
| 本地文件存储 | `./uploads/{recording_id}.{ext}`，不接对象存储 | Q9 |
| 调试文件格式 | `api_test.http`（JetBrains / REST Client） | Q11 |
| 目录分层 | 扁平风格 A（见下文 4.4） | Q10 |

### 2.2 P0 必做的接口契约（PDF 原文摘录，一字不改）

| 方法 | 路径 | 输入 | 成功返回（HTTP 2xx） | 关键约束 |
|---|---|---|---|---|
| POST | `/v1/recordings` | `multipart/form-data` 字段 `file` | `{"recording_id":"...","task_id":"...","status":"pending"}` | 立即返回，**不得同步等待处理** |
| GET | `/v1/tasks/{task_id}` | 路径参数 `task_id` | `{task_id, recording_id, status, ...}` | status 必须能区分 `transcribing` / `summarizing` |
| GET | `/v1/recordings` | query: `page`, `page_size`（分页） | 分页列表，`ORDER BY created_at DESC`，每条含最新任务状态 | 分页参数默认值：page=1, page_size=20 |
| GET | `/v1/recordings/{id}` | 路径参数 `id` | 录音详情；**若处理完成则含 transcript 与摘要** | 未完成时只返回元信息+状态 |
| POST | `/v1/tasks/{task_id}/retry` | 路径参数 `task_id` | `{task_id, status:"pending"}` | **仅 failed 状态允许**，重复请求不能重复执行（幂等） |
| DELETE | `/v1/recordings/{id}` | 路径参数 `id` | HTTP 204 或 200 空对象 | 同时删除本地文件 + DB 关联数据 |

### 2.3 P0 必做的业务规则（PDF 原文摘录）

1. **上传校验**：文件必须存在；大小 ≤50MB；扩展名 ∈ {`wav`, `mp3`, `m4a`, `aac`}。
2. **任务状态机（5 态）**：`pending → transcribing → summarizing → done`；**任一环节可失败进入 `failed`**。
3. **转写阶段（Mock）**：
   - 随机耗时 **5~15 秒**；
   - 约 **20% 概率失败**；
   - 成功后产出一段固定/随机 transcript 文本（可自行生成）。
4. **摘要阶段（真实 LLM）**：输出必须严格符合 JSON Schema：
   ```json
   {"summary":"一句话摘要","key_points":["要点1","要点2"],"todos":["待办1"]}
   ```
   - **必须处理**：LLM 调用超时；返回内容不符合预期格式（解析失败）。
5. **日志要求**：关键路径有日志，**能通过 grep 一个 task_id 还原完整生命周期**。
6. **工程要求**：统一错误响应结构，合理 HTTP 状态码（400/404/409/413/415/422/500 严格区分）；表结构通过 Alembic 迁移脚本提供。

### 2.4 P1 加分项已确认的实现范围（Q8，不做 3/7）

| # | 加分项 | 是否做 | 实现方式（摘要） |
|---|---|---|---|
| 1 | 失败自动重试（最多 3 次，指数退避） | ✅ | 每个阶段独立 `for attempt in range(3): try: ... except: sleep(2**attempt)` |
| 2 | 服务重启恢复（处理中/排队中任务继续） | ✅ | startup hook `SELECT WHERE status IN (pending,transcribing,summarizing)` 重新入队 |
| 3 | LLM 流式 SSE 输出 | ❌ 不做 | 复杂度高，收益低，留 README 说明作为「已知未完成项」 |
| 4 | 上传幂等（同文件不重复建任务） | ✅ | recordings 表 `file_hash`（MD5）唯一索引；上传命中直接返回旧 ID |
| 5 | 并发控制（最多 N 个任务同时跑） | ✅ | `asyncio.Semaphore(MAX_CONCURRENT_TASKS)`，默认值 3，读 `.env` |
| 6 | 单元/集成测试 | ✅ | `pytest + pytest-asyncio + httpx.AsyncClient`，SQLite 内存测试库，5 个核心用例 |
| 7 | 公网部署 | ❌ 不做 | 浪费时间，无收益 |

### 2.5 已有运行方式（规划，确认后落地）

```powershell
# ========== 本机首次运行（你本机已装 MySQL）==========
# 1. 手动建库（只做一次）
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS xisiyun_asr CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# 2. 填环境变量
copy .env.example .env
# 然后编辑 .env 填入 MYSQL_PASSWORD、DEEPSEEK_API_KEY

# 3. 装依赖 + 建表 + 启动（一条命令也可以拆）
pip install -r requirements.txt
alembic upgrade head       # Alembic 自动通过迁移脚本建 recordings/tasks 两张表
uvicorn app.main:app --reload --port 8000

# 4. 测试
# PyCharm 打开 api_test.http → 点绿色箭头
# 或 curl 走 README 里的示例
# 或跑测试：pytest -v
```

---

## 3. 需求拆解（功能点 × DoD）

> 每个功能点必须回答：**要做什么 → 影响哪里（文件/模块）→ 完成的判断标准（Definition of Done, DoD）**。
>
> 编码顺序**严格按 P0-1 → P0-2 → ... → P1 顺序**，不提前写加分项。

---

### P0-1. 项目骨架与基础设施

| 维度 | 内容 |
|---|---|
| **要做什么** | 搭完整的可运行项目骨架（无业务逻辑，但能启动能打 /health 返回 200）；装依赖；配 Alembic；配日志 |
| **影响模块（对应目录）** | `requirements.txt` / `.env.example` / `.gitignore` / `app/main.py` / `app/config.py` / `app/database.py` / `app/utils/logger.py` / `app/utils/errors.py` / `migrations/`（Alembic 全套）/ `app/models.py`（空壳，先有 Base） |
| **DoD** | ① `pip install -r requirements.txt` 无错；② `uvicorn app.main:app --port 8000` 启动无异常；③ `curl http://localhost:8000/health` 返回 `{"status":"ok"}`；④ `app/utils/logger.py` 能同时打带 `task_id` 字段的控制台日志 + `./logs/app.log` 文件轮转；⑤ `app/utils/errors.py` 注册的全局异常处理器对未捕获异常返回统一 JSON 格式而非 FastAPI 默认 HTML；⑥ `alembic current` 能正确连接 `.env` 里配置的 MySQL 并显示版本 |

---

### P0-2. 数据库表结构（Alembic 迁移）

| 维度 | 内容 |
|---|---|
| **要做什么** | 建 `recordings`、`tasks` 两张表，字段完全对齐第 5 章「表结构设计」；建必要索引 |
| **影响模块** | `app/models.py`（SQLAlchemy 两个 ORM 类）；Alembic 自动生成 `migrations/versions/xxxx_initial_tables.py` |
| **字段与约束**（必须逐条对齐） |见本 Spec 第 5 章「数据结构设计 — DB 表」 |
| **DoD** | ① `alembic upgrade head` 在空库 `xisiyun_asr` 上建两张表无报错；② `DESCRIBE recordings` 和 `DESCRIBE tasks` 的字段、类型、约束、索引与第 5 章逐条一致（尤其 `file_hash UNIQUE`、`tasks.status INDEX`、外键 `ON DELETE CASCADE`）；③ `alembic downgrade base` 能正确回滚删表 |

---

### P0-3. POST `/v1/recordings` 上传接口（不包含异步处理启动，先只存+入库）

| 维度 | 内容 |
|---|---|
| **要做什么** | 文件校验、落盘、MD5 去重（P1-4 的前置）、两张表各插一条初始记录、立即返回（**此点先不往队列塞**，队列在 P0-5 做） |
| **影响模块** | `app/api/v1/recordings.py` / `app/schemas.py`（`UploadResponse` schema） / `app/services/storage.py`（`save_uploaded_file()`、`compute_file_md5_streaming()`、`delete_file_by_recording()`） |
| **校验顺序（按性能从快到慢排，早失败早返回）** | ① 文件是否存在（无 file 字段 → 422 由 FastAPI 自动返回）→ ② 扩展名白名单 wav/mp3/m4a/aac（不区分大小写，错 → 415 Unsupported Media Type）→ ③ 大小 ≤50MB（在 FastAPI `UploadFile` 读取时判断，或在 `storage.py` 读完统计，超 → 413 Payload Too Large）→ ④ 流式算 MD5（边读边算，避免 50MB 一次性读入内存爆）→ ⑤ 查 `file_hash` 唯一索引：命中 → 不写文件不插新表，直接返回旧的 `recording_id` + 对应 `task_id` + 旧 status（P1-4 上传幂等） |
| **DoD** | ① 上传合法 wav 返回 200，body 严格是 PDF 要求的三字段 JSON（字段名不能拼错！）；② 上传非白名单扩展名返回正确 415；③ 上传 51MB 文件返回正确 413；④ 完全相同的同一个二进制文件上传两次 → `recording_id` 完全相同（幂等生效），`uploads/` 目录下只有一个物理文件；⑤ recordings 表和 tasks 表能各看到一条初始记录：tasks.status=`pending`、recordings.last_status=`pending` |

---

### P0-4. 异步任务执行引擎（asyncio Queue + Worker，不包含具体阶段逻辑，先空跑）

| 维度 | 内容 |
|---|---|
| **要做什么** | 在 `pipeline.py` 启动后台 worker 协程；startup hook 启 worker；队列塞 task_id 能被消费；Semaphore 包执行体；logging 贯穿；startup hook 扫 DB 把非终态任务重新入队（P1-2 前置） |
| **影响模块** | `app/services/pipeline.py`（`_queue: asyncio.Queue`、`_sem: asyncio.Semaphore`、`worker_loop()` 协程、`start_background_workers()`、`enqueue_task()`、`resume_pending_tasks_on_startup()`）；`app/main.py` startup hook 调 `start_background_workers()` + `resume_pending_tasks_on_startup()` |
| **DoD** | ① 启动后手动往 `_queue` put 一个假 task_id → worker 里的 `print`/日志能看到被取出；② `MAX_CONCURRENT_TASKS=3`，同时塞 10 个 → 日志时间戳能证明同时最多 3 个在跑；③ 服务正常启动后，手动在 MySQL 把一条 tasks.status 从 `done` 改成 `pending` 并重启 uvicorn → 启动日志能看到「Resuming X pending tasks...」并且那条 task_id 被重新入队；④ shutdown 时 worker 协程能 clean 退出（不强制要求优雅等当前任务跑完，但至少不能抛 `Task was destroyed but it is pending!` 警告；FastAPI 的 lifespan + `cancel` + `await task` 就行） |

---

### P0-5. 状态机阶段逻辑实现（Mock 转写 + 真实 LLM 摘要 + 自动重试）

| 维度 | 内容 |
|---|---|
| **要做什么** | 写 `run_pipeline(task_id)` 函数，完整实现 pending→transcribing→summarizing→done 四态流转+失败进入 failed；每个阶段独立 3 次自动重试（P1-1 前置）；阶段状态变更必须乐观锁写 DB（P0-6 retry 幂等的前置） |
| **影响模块** | `app/services/pipeline.py`（`run_pipeline()` 主函数 + `_stage_transcribe()` + `_stage_summarize()`）；`app/services/llm.py`（`summarize_transcript(transcript: str) -> dict` 封装 DeepSeek 调用，含超时/response_format=json_object/loads 兜底） |
| **阶段细节（逐条对齐 PDF）** | **Stage 1 转写（Mock）**：① 改 tasks.status=`transcribing`（必须 WHERE status='pending' 乐观锁，若 WHERE 不命中说明别人改了，直接 return 不重复跑）；② `t = random.uniform(5, 15); await asyncio.sleep(t)`；③ `if random.random() < 0.2: raise TranscriptionMockError("Mock 20% failure")`；④ 成功则生成 transcript（`lipsum()` 随机中文 500~2000 字，或固定一段长文本都行）；⑤ `UPDATE recordings SET transcript=?, last_status='summarizing' WHERE id=?`；⑥ 改 tasks.status=`summarizing`。**Stage 2 摘要（LLM）**：① 改 tasks.status=`summarizing`（WHERE status='transcribing' 乐观锁）；② 调 `llm.summarize_transcript()`，内部 httpx.AsyncClient timeout=30s，`response_format={"type":"json_object"}`，system prompt 里**贴出最终 JSON schema** 要求必须有 summary(str) / key_points(list[str]) / todos(list[str]) 三个字段；③ 拿到 LLM 返回的文本 → `json.loads()` try/except，解析失败抛 `LLMResponseFormatError`；④ 成功则 `UPDATE recordings SET summary_json=?, last_status='done'`；⑤ 改 tasks.status=`done`。**失败与自动重试（P1-1）**：每个阶段独立用 `for attempt_idx in range(MAX_AUTO_RETRIES=3): try: ... except Exception as e: tasks.current_stage_retry_count = attempt_idx+1; tasks.error_message = str(e); if attempt_idx == MAX_AUTO_RETRIES-1: 改 tasks.status='failed' + recordings.last_status='failed' + break; await asyncio.sleep(2 ** attempt_idx) # 1s/2s/4s` |
| **DoD** | ① 正常情况下（不触发 Mock 失败 + LLM 正常），一个任务 6~45 秒内走到 done → DB 里 tasks.status=done，recordings 有 transcript 文本且 summary_json 是合法的三字段 dict；② 故意让 Mock 失败率改成 100% 测试（测试时临时改常量）→ 3 次重试日志间隔为 1s/2s/4s → 最终进入 failed，tasks.error_message 非空，tasks.current_stage_retry_count=3；③ 故意把 DeepSeek Key 改错测试 → 3 次重试后 failed，error_message 里包含 httpx 401/鉴权失败信息；④ 故意在 LLM prompt 里去掉 response_format（测试时临时改）让模型偶尔返回非 JSON → 解析异常被捕获，计入重试次数；⑤ 每一步状态流转都有 INFO 日志，grep 一个 task_id 能看到：入队 → 开始转写 → 转写第 N 次失败/成功 → 开始摘要 → 摘要第 N 次失败/成功 → 终态（done/failed），共 6~10 条日志，时间线正确 |

---

### P0-6. 5 个查询/操作类接口（列表/详情/任务状态/重试/删除）

| 维度 | 内容 |
|---|---|
| **要做什么** | 实现 P0 必做的剩余 5 个接口（GET tasks、GET recordings list、GET recordings detail、POST retry、DELETE recordings）；POST retry 必须幂等（重复请求不重复入队，非 failed 返回 409）；DELETE 级联删 DB + 删本地文件 |
| **影响模块** | `app/api/v1/tasks.py`（GET status + POST retry）；`app/api/v1/recordings.py`（GET list + GET detail + DELETE）；`app/schemas.py`（`TaskOut` / `RecordingOut` / `RecordingListItem` / `PagedResponse[T]` 泛型分页 schema，严格按 FastAPI 惯例写） |
| **每条接口的硬约束（逐条检查）** | **GET /v1/tasks/{task_id}**：task_id 不存在 → 404 `{code:TASK_NOT_FOUND}`；status 字段必须是 5 态之一，processing 时能区分是在 transcribing 还是 summarizing（也就是 status 本身直接是字符串，不要用 processing + stage 分离的字段，PDF 要的是 status 体现阶段）。**GET /v1/recordings?page=&page_size=**：默认 page=1 / page_size=20；page_size 上限 100（防止查全表拖死 MySQL）；`ORDER BY created_at DESC`；返回字段含 `total / page / page_size / items[]`，每个 item 含 `last_status`（直接读 recordings.last_status 冗余字段，不用 JOIN，性能好）。**GET /v1/recordings/{id}**：不存在 → 404；status=done 时 response body 里必须含 `transcript` 字段(str)和 `summary` 字段(解析成对象的三字段，不要返回 DB 的 JSON 字符串)；status!=done 时 `transcript` 和 `summary` 设为 null 或干脆不返回（用 schema `model_dump(exclude_none=True)`）。**POST /v1/tasks/{task_id}/retry**：幂等核心逻辑 = `事务开始 → SELECT tasks WHERE id=? FOR UPDATE（行锁，防并发重入）→ IF status != 'failed'：回滚，return HTTP 409 {code: TASK_NOT_FAILED, message:"..."} → ELSE：UPDATE tasks SET status='pending', current_stage_retry_count=0, total_retry_count=total_retry_count+1, error_message=NULL；UPDATE recordings SET last_status='pending'；enqueue_task(task_id)；提交事务 → return 200`。409 场景：status 是 pending/transcribing/summarizing/done 都要返回 409，只有 failed 能过；同一个 failed task 连续 POST 两次 retry → 第二次由于 status 已经改成 pending 了所以 409，天然幂等。**DELETE /v1/recordings/{id}**：不存在 → 404；事务顺序（很重要！）：① `SELECT storage_path, file_ext FROM recordings WHERE id=?` 拿路径（或拼接 `uploads/{id}.{ext}` 也行）；② `DELETE FROM tasks WHERE recording_id=?`（外键先删子表，或用 ON DELETE CASCADE 自动删，两种都行但要在 Alembic 里配置 CASCADE）；③ `DELETE FROM recordings WHERE id=?`；④ 提交事务；⑤ **事务提交成功后**再删本地文件（避免删了文件但 DB 回滚不一致）；删文件用 `Path.unlink(missing_ok=True)` 静默失败，不能因为磁盘文件丢了就返回 500；⑥ 返回 204 No Content（无 body）或 200 + `{deleted:true}`（二者皆可，但要在 schema 里统一） |
| **DoD** | ① 用 `api_test.http` 的 6 个请求按 PDF 顺序全跑通（上传 → 立即查 tasks.status=pending → 等 30 秒 → 查 tasks.status ∈ {transcribing/summarizing/done} → done 后查 recordings detail 有 transcript 有 summary → 手动改成 failed 测 retry 返回 pending → 第二次 retry 返回 409 → DELETE 成功 → 再 GET 返回 404）；② 所有 P0 指定的错误码场景都返回**正确的 HTTP 状态码 + 统一 JSON 错误结构**：404（不存在）/409（retry 非 failed）/415（扩展名）/413（超限）/400（其他业务错）/422（FastAPI 自动参数校验）/500（未知）；③ `api_test.http` 末尾有一个「完整生命周期脚本块」，6 个请求用 `{% client.global %}` 变量串起来（第一个请求返回的 recording_id 自动塞到后面） |

---

### P0-7. 统一错误响应 + 日志贯穿（工程规范收尾）

| 维度 | 内容 |
|---|---|
| **要做什么** | 把 P0-6 里散落的 HTTPException 全部收敛成自定义异常 + `errors.py` 里的 handler；日志格式统一带上 task_id；README 写好 |
| **影响模块** | `app/utils/errors.py`（新增 `AppBaseException`/`NotFoundException`/`ConflictException`/`BadRequestException`/`UnsupportedMediaTypeException`/`PayloadTooLargeException` 等自定义类，每个类绑定一个 HTTP 状态码和业务 code 字符串）；`app/main.py`（@app.exception_handler 注册全部异常类 + 兜底 Exception handler → 统一返回 `{"error": {"code": str, "message": str, "details": dict\|null}}`） |
| **DoD** | ① 以上所有 4xx/5xx 场景用 curl 测一遍，返回体**顶级结构 100% 一样**（都是 `error.code / error.message / error.details` 三字段），不会出现 FastAPI 默认的 `detail` 字段；② 手动触发一个未捕获异常（比如在某个接口里写 `1/0`）→ 返回 HTTP 500 + `{error:{code:"INTERNAL_SERVER_ERROR", message:"..."}}`，日志里有完整 traceback，但响应体 message 是脱敏的 "Internal server error"，不会把堆栈泄露给客户端；③ README 里有「错误响应格式」小节，列了所有业务 code 字符串含义 |

---

### P1-1~P1-6. 加分项（P0 全通过后再做，共 5 项）

> 注意：**大部分加分项代码其实已经嵌在 P0 里了**（比如 P1-2 重启恢复 = P0-4 的 startup hook；P1-5 并发控制 = `Semaphore`；P1-1 自动重试 = P0-5 for 循环；P1-4 上传幂等 = P0-3 查 `file_hash` UNIQUE），这里列的是"收尾 + 测试 + 边界完善"。

| 功能点 | 要做什么/DoD | 影响模块 |
|---|---|---|
| **P1-1 自动重试边界** | 确认 `current_stage_retry_count` 在用户手动 POST retry 时**重置为 0**（P0-6 retry UPDATE 语句已包含，但要单独验证一下：自动 3 次→failed→POST retry→新的自动重试计数从 1 重新开始，而不是 4、5） | `app/api/v1/tasks.py` retry 接口的 UPDATE SQL |
| **P1-2 重启恢复边界** | 场景：服务在「转写 sleep 到第 10 秒」时被强杀重启 → 任务状态 transcribing → startup hook 扫出来重新入队 → 转写阶段重新跑（Mock 幂等）→ 正常继续。注意：startup 入队前先做「UPDATE tasks SET status='pending' WHERE status IN (transcribing, summarizing)」把正在处理中的状态重置为 pending 再入队，不然阶段乐观锁的 WHERE 条件会不命中导致任务永远挂在 transcribing 上 | `app/services/pipeline.py: resume_pending_tasks_on_startup()` |
| **P1-4 上传幂等等边界** | 同 MD5 但**原文件名不同**的两个文件上传 → 按设计仍视为重复（因为 hash 一样，物理内容相同），返回旧 ID；测试 case 要覆盖：把同一个 wav 改名为 a.wav / b.wav 各传一次 → recording_id 相同 | `app/services/storage.py` MD5 逻辑；测试用例 |
| **P1-5 并发控制边界** | 临时把 `.env` 的 `MAX_CONCURRENT_TASKS` 改成 1 → 同时传 3 个音频 → 日志时间戳能证明第一个开始时间 T，第二个 T+~10，第三个 T+~20，串行执行。改回 3 后能并行 | `app/config.py` 读 MAX_CONCURRENT_TASKS；测试用例（可选） |
| **P1-6 测试（独立重点功能点！）** | **5 个必测用例（全部集成测试，用 SQLite 内存库，不污染本机 MySQL）**：① `test_upload_success_returns_pending` → 上传成功，返回 pending，DB 有记录，uploads 有文件；② `test_upload_idempotent_by_md5` → 同文件传两次 recording_id 相同；③ `test_pipeline_runs_to_done` → mock `_stage_transcribe`（去掉 random 失败和 sleep）+ mock LLM（返回固定 JSON）→ 等 0.1 秒后查 status=done，summary_json 正确；④ `test_retry_only_failed` → 先手动插一条 status=done 的 task → POST retry → 断言返回 409；⑤ `test_delete_cascades_file_and_db` → 上传成功后 DELETE → 查 recordings 表 0 条 + tasks 表 0 条 + `uploads/` 下文件不存在。**DoD**：`pytest -v` 5/5 全绿，`coverage run -m pytest && coverage report` 能看到核心业务代码覆盖率 ≥70%（不强求具体数字但至少不能全是 mock） | `tests/conftest.py`（fixture：override get_session 用 SQLite 内存、override `_queue` 同步化、mock LLM 类）；`tests/test_api.py` 5 个用例 |

---

## 4. 实现思路（不写代码，写模块关系）

### 4.1 核心运行时架构图（Mermaid，README 要同款）

```mermaid
flowchart TD
    Client[客户端] -->|1. POST multipart file| FastAPI[FastAPI Web 进程<br/>uvicorn 1 worker 多协程]
    FastAPI -->|1a. 校验扩展名/大小| FastAPI
    FastAPI -->|1b. 流式算 MD5| HashIdx[(MySQL recordings.file_hash UNIQUE)]
    HashIdx -->|命中| ReturnOld[返回旧 recording_id<br/>P1-4 幂等 不写文件不入队]
    HashIdx -->|未命中| Storage[本地磁盘<br/>./uploads&#47;{id}.{ext}]
    Storage -->|写成功| InsertTask[(MySQL 插 recordings + tasks<br/>status = pending)]
    InsertTask -->|立即返回 pending| Client
    InsertTask -->|2. asyncio.Queue.put_nowait(task_id)| Queue[内存队列<br/>asyncio.Queue]

    subgraph 后台处理线程 同一进程内 协程驱动
      Queue -->|Semaphore 3<br/>P1-5 并发控制| Workers[Worker 1 .. N 协程]
      Workers -->|3. UPDATE tasks SET status = transcribing| DbMain[(MySQL)]
      Workers -->|Mock random 5~15s, 20% 失败| MockAsr{Mock 转写结果}
      MockAsr -->|成功| Llm[LLM 调用 deepseek-chat<br/>timeout 30s response_format = json_object]
      MockAsr -->|失败 重试 3 次指数退避 P1-1| MockAsr
      Llm -->|成功| DbDone[(MySQL recordings.summary_json<br/>status = done)]
      Llm -->|超时&#47;JSON 格式错 重试 3 次 P1-1| Llm
      Llm -->|重试耗尽| DbFail[(tasks.status = failed<br/>error_message 堆栈)]
    end

    Client -->|4. GET &#47;v1&#47;tasks&#47;{id} 轮询| FastAPI --> DbMain
    Client -->|5. GET &#47;v1&#47;recordings&#47;{id}| FastAPI -->|status = done 才返回 transcript + summary| DbMain
    Client -->|6. POST &#47;v1&#47;tasks&#47;{id}&#47;retry| FastAPI -->|仅 failed 200 pending<br/>否则 409 TASK_NOT_FAILED| DbMain --> Queue
    Client -->|7. DELETE &#47;v1&#47;recordings&#47;{id}| FastAPI -->|删 DB CASCADE + 删文件| DbMain + Storage

    StartupHook[服务启动 startup hook] -->|扫 status != done&#47;failed 的 tasks| DbMain
    StartupHook -->|全部重新塞回 Queue P1-2| Queue
```

### 4.2 关键模块的职责边界（避免文件间循环 import）

| 模块 | 只允许依赖的上游模块 | 绝对禁止做什么 |
|---|---|---|
| `main.py` | 所有子模块 | 不写任何业务逻辑，只做「装配」：启 app、加 middleware、注册 router、hook 调 pipeline 启动 |
| `config.py` | 零依赖（pydantic-settings 除外） | 不 import 任何其他 app 模块 |
| `database.py` | `config.py` | 不 import models/schemas/api |
| `models.py`（ORM） | `database.py`（Base） | 不写业务方法，最多写 `__repr__` 方便调试 |
| `schemas.py`（Pydantic） | 零依赖 | 不 import ORM models，避免循环（用 from __future__  annotations 都不行的话就手写字段） |
| `api/*.py`（路由层） | `schemas.py` + `services/*` + `database.py`（get_session Depends） | 绝对不允许直接 `session.execute()` 写 SQL；所有 DB 操作必须经过 services 层！不然以后换 DB 你要改 100 个地方 |
| `services/pipeline.py`（核心） | `models.py` + `database.py`（session）+ `services/llm.py` + `services/storage.py` + `config.py` + `utils/logger.py` | 不 import router；不直接返回 HTTP 响应；所有异常只抛自定义异常（NotFoundException / PipelineError / ...）让上层 handler 转 |
| `services/llm.py` | `config.py` + `utils/logger.py` | 不碰 DB；不碰 queue |
| `services/storage.py` | `config.py` | 只碰磁盘 IO 和 MD5；不碰 DB |
| `utils/*.py` | 零或 `config.py` | 绝对不 import services 或 api |

### 4.3 接口 → 服务 → DB 调用链模板（每个接口照这个写）

```
POST /v1/recordings  [app/api/v1/recordings.py:create_recording()]
  └─ Depends(get_session) → async_session
  └─ Depends(get_pipeline) → 拿到 pipeline 对象（含 queue 和 sem）
  └─ storage.compute_file_md5_streaming(file) → str
  └─ storage.save_uploaded_file(file, recording_id, ext) → Path
  └─ recordings_service.create_with_task(session, {md5, ext, size, ...})
        └─ session.add(Recording + Task) → flush → 拿 id
        └─ session.commit()
  └─ pipeline.enqueue_task(task_id) → 非阻塞
  └─ return schemas.UploadResponse(...)
```

### 4.4 数据结构设计（DB 两张表，逐条字段对齐）

#### 4.4.1 `recordings` 表（录音元信息）

| 字段名 | SQLAlchemy 类型 | MySQL 类型 | 约束/索引 | 用途/备注 |
|---|---|---|---|---|
| id | `String(36)` | `VARCHAR(36)` | PK, PRIMARY KEY | UUID4，recording_id |
| original_filename | `String(255)` | `VARCHAR(255)` | NOT NULL | 仅展示给用户看，不参与任何路径拼接 |
| file_ext | `String(10)` | `VARCHAR(10)` | NOT NULL | 小写，wav/mp3/m4a/aac |
| file_size_bytes | `BigInteger()` | `BIGINT UNSIGNED` | NOT NULL | 校验用 |
| file_hash | `String(64)` | `VARCHAR(64)` | NOT NULL, **UNIQUE INDEX `uk_recordings_file_hash`** | 上传 MD5，P1-4 幂等核心。64 位预留以后换 SHA256 |
| storage_path | `String(512)` | `VARCHAR(512)` | NOT NULL | 相对路径，例 `uploads/xxxx-xxxx.wav`。冗余存，以后换对象存储不用改代码 |
| transcript | `Text()` | `LONGTEXT` | NULLABLE | Mock 转写结果，最长 4GB 足够 |
| summary_json | `JSON()` | `JSON` | NULLABLE | MySQL 原生 JSON 类型，存 LLM 返回的 `{summary,key_points,todos}` 三字段。读出来 SQLAlchemy 会自动反序列化成 dict |
| last_status | `Enum(TaskStatus)` | `VARCHAR(20)` | NOT NULL DEFAULT 'pending', INDEX `idx_recordings_last_status` | **冗余存 tasks 表最新状态**，列表接口不用 JOIN。枚举值同下 tasks.status |
| created_at | `DateTime(timezone=True), server_default=func.now()` | `DATETIME(6)` | NOT NULL, INDEX `idx_recordings_created_at` | 分页 ORDER BY 用，加索引加速 |
| updated_at | `DateTime(timezone=True), server_default=func.now(), onupdate=func.now()` | `DATETIME(6)` | NOT NULL | |

#### 4.4.2 `tasks` 表（任务状态机）

| 字段名 | SQLAlchemy 类型 | MySQL 类型 | 约束/索引 | 用途/备注 |
|---|---|---|---|---|
| id | `String(36)` | `VARCHAR(36)` | PK | UUID4，task_id |
| recording_id | `String(36)` | `VARCHAR(36)` | **FK → recordings.id ON DELETE CASCADE**, NOT NULL, INDEX `idx_tasks_recording_id` | 删 recording 时自动级联删 task，不用手写 DELETE 子表 |
| status | `Enum(TaskStatus)` | `VARCHAR(20)` | NOT NULL DEFAULT 'pending', **INDEX `idx_tasks_status`** | 枚举值 5 态：pending/transcribing/summarizing/done/failed。startup 扫表核心索引 |
| current_stage_retry_count | `SmallInteger()` | `TINYINT UNSIGNED` | NOT NULL DEFAULT 0 | 当前阶段的自动重试次数，P1-1 用，上限 3 |
| total_retry_count | `Integer()` | `INT UNSIGNED` | NOT NULL DEFAULT 0 | 用户手动 POST /retry 累加的**总次数**，仅统计用，不影响业务逻辑 |
| error_message | `Text()` | `TEXT` | NULLABLE | 失败时的异常 message（不是堆栈，堆栈只打日志），用户能通过 GET /tasks/{id} 看到 |
| created_at | `DateTime(timezone=True), server_default=func.now()` | `DATETIME(6)` | NOT NULL | |
| updated_at | `DateTime(timezone=True), server_default=func.now(), onupdate=func.now()` | `DATETIME(6)` | NOT NULL, INDEX `idx_tasks_updated_at` | |

#### 4.4.3 Pydantic Schemas（只列关键几个，其他按需加）

```python
# 伪代码，仅描述结构，不是要写的代码
UploadResponse = { recording_id: str, task_id: str, status: TaskStatusEnum }
TaskOut = { id: str, recording_id: str, status: TaskStatusEnum, current_stage_retry_count: int, total_retry_count: int, error_message: str|None, created_at: datetime, updated_at: datetime }
SummaryOut = { summary: str, key_points: list[str], todos: list[str] }
RecordingListItem = { id: str, original_filename: str, file_ext: str, file_size_bytes: int, last_status: TaskStatusEnum, created_at: datetime, updated_at: datetime }
RecordingOut = RecordingListItem + { transcript: str|None, summary: SummaryOut|None }  # status==done 才有后两个字段
PagedResponse[T] = { total: int, page: int, page_size: int, items: list[T] }
ErrorResponse = { error: { code: str, message: str, details: dict|None } }
```

### 4.5 必须处理的边界/异常场景（不要漏，面试会重点问）

| 场景 | 发生概率 | 应该怎么处理（不能崩） | 对应代码位置 |
|---|---|---|---|
| 用户上传 `.WAV`（大写） | 常见 | 扩展名判断前 `.lower()`，415 不能误杀 | `storage.py` + `recordings.py` 校验 |
| 用户上传 `file.mp3.exe` 双扩展名 | 恶意 | **用最后一个点的扩展名**就会被拦（exe 不在白名单）；但如果有人传 `.aac` 实际是 exe —— 没关系，我们不执行文件，只是存，风险为 0，照样存 | 同上 |
| 上传 50MB 大文件 | 常见 | 必须**流式读**算 MD5 + 存磁盘，不能一次性 `await file.read()` 读到内存，不然 10 个并发直接 OOM。FastAPI 的 UploadFile 本身就是类文件对象，支持 `chunk = await file.read(1024*1024)` 分块读 | `storage.py` 的 `compute_file_md5_streaming()` 和 `save_uploaded_file()` |
| DB 插入 recordings 成功，但写磁盘失败（磁盘满 / 权限） | 低 | 抛异常让事务回滚 → DB 也不会有脏数据，然后返回 500 + 错误信息「磁盘写入失败」给客户端。**顺序：先写 DB（事务内）→ 再写磁盘？还是反过来？** → 推荐**先写磁盘成功 → 再写 DB**。原因：写磁盘失败事务还没开，无回滚成本；写 DB 失败只要把磁盘文件删掉即可（try/except finally） | `recordings.py` create 接口的顺序 |
| 队列塞 task_id 时，worker 协程还没启动 | 极低概率 | startup hook 里**先 start_background_workers()，再 resume_pending_tasks_on_startup()，再开 http 端口**。顺序要对，不然 queue 里的 task 没人消费会 pending 到天荒地老 | `main.py` lifespan 顺序 |
| LLM 返回合法 JSON 但字段缺一个（比如缺 todos 返回 `{}`） | 中 | `json.loads` 之后**再加一层 Pydantic 校验**：`schemas.LLMSummaryResponse.model_validate(parsed_json)`，不通过抛 `LLMResponseFormatError` 计入重试。这比只靠 loads 更严格，PDF 说"不符合预期格式"也算失败 | `services/llm.py` summarize_transcript() 末尾 |
| DELETE 接口时，本地文件被用户手动删了 | 中 | `Path.unlink(missing_ok=True)` 静默跳过，DB 仍然照删，返回 200。**不要因为文件不存在就返回 500/404**，对用户来说"删除"的语义已经满足了 | `recordings.py` DELETE 接口 |
| 分页接口 page=0 / page_size=10000 | 恶意测试 | `schemas.py` 里用 `Field(ge=1)` / `Field(ge=1, le=100)` 让 FastAPI 自动返回 422，参数校验不通过根本进不了业务逻辑 | `schemas.py` PagedQuery schema |
| 并发两个请求同时 POST retry 同一个 failed task | 面试经典题 | 必须用 `SELECT ... FOR UPDATE`（行级锁）+ 事务。两个并发请求：第一个拿到行锁，改 status 为 pending，commit；第二个请求等锁释放后再 SELECT → status 已经是 pending → 返回 409。如果不加 FOR UPDATE，两个请求读到的都是 failed → 都改成功 → 任务被 enqueue 两次，跑两遍！ | `tasks.py` retry 接口 |
| 服务在 LLM 调用的第 29 秒重启 | P1-2 边界 | status = summarizing → startup hook 先 UPDATE 成 pending → 重新入队 → 乐观锁阶段判断过 → 重新跑转写+摘要。幂等没问题，转写和摘要都是"覆盖写"，不会有脏数据 | `pipeline.py` resume 函数 |

---

## 5. 测试与验收（分 3 个层级，必须逐层通过才能算完成）

### 5.1 L1：本地手动冒烟（`api_test.http`，你写代码时自己要反复跑）

**命令**：PyCharm 打开 `d:\code\xisiyun\api_test.http` → 每个请求左方绿色 ▶ 依次点；或 VS Code 装 REST Client 插件。

**必按顺序点的 9 个测试块（对应 P0 所有场景）**：

1. `### 0. Health Check` → 200 `{"status":"ok"}`
2. `### 1. 上传合法 mp3` → 200，保存 `{{recording_id}}` / `{{task_id}}` 到变量
3. `### 2. 上传超大文件 (51MB 占位)` → 413
4. `### 3. 上传 txt（非白名单）` → 415
5. `### 4. 同 MD5 文件第二次上传` → 200，recording_id == 第 2 步的（P1-4 幂等验证）
6. `### 5. 查询 task 状态（前 5 秒）` → status ∈ {pending, transcribing}
7. `### 6. 查询任务 30 秒后再次查询` → status ∈ {summarizing, done, failed}，不是 pending/transcribing 就行（证明会流转）
8. `### 7. GET 录音详情` → 如果是 done：body 有 transcript(str) + summary(obj)；否则：这两个字段 null / 不存在
9. `### 8. 模拟失败场景：手动 SQL 改成 failed → POST retry` → 200，status=pending；**紧接着同一块内再 POST 一次** → 409
10. `### 9. DELETE recording` → 204；紧接着 GET detail → 404

### 5.2 L2：自动化测试（pytest，加分项 P1-6 的核心）

**命令**：

```powershell
cd d:\code\xisiyun
pip install -r requirements.txt      # 含 pytest pytest-asyncio pytest-httpx
pytest -v -s                         # -s 看日志，-v 看用例名
```

**5 个必过用例（每个用例 = 1 个 `def test_xxx` 函数）**：

| 用例名 | 做什么 | 断言 |
|---|---|---|
| `test_upload_success` | AsyncClient 上传一个临时生成的 1KB wav（空 header + 假数据也行） | ① HTTP 200；② response json 有三个字段；③ DB recordings 表 COUNT=1；④ `Path(uploads/...).exists()` 为 True |
| `test_upload_idempotent_md5` | 同一个 bytes 上传两次 | 两次返回的 recording_id **完全相等**；DB 还是 COUNT=1；磁盘只有一个文件 |
| `test_pipeline_full_success` | monkeypatch 掉：① `_stage_transcribe` 的 asyncio.sleep → 不 sleep，random 失败 → 永远成功；② `llm.summarize_transcript` → 返回固定 dict；上传后 `await asyncio.sleep(0.2)` 后 GET /recordings/{id} | status == 'done'；summary.key_points 类型 == list；summary.summary == mock 返回的固定值 |
| `test_retry_409_on_non_failed` | 手动插一条 status='done' 的 task → POST /retry | HTTP 409；error.code == 'TASK_NOT_FAILED' |
| `test_delete_cascades_everything` | 上传成功拿到 recording_id → DELETE → GET → 查 DB → 查文件 | DELETE 204；GET 404；DB 两表 COUNT 都=0；磁盘文件不存在 |

### 5.3 L3：最终交付验收（对照 PDF 提交项 1~4 一条一条勾）

| 提交项 | 验收标准 | 是否通过 |
|---|---|---|
| 1. 仓库 commit 历史 | GitHub/Gitee 上至少有 **5~10 个 commit**，每个 commit message 清晰（如 `feat: upload endpoint with md5 idempotency` / `fix: retry 409 on concurrent requests`），**禁止只有一个 Initial commit 把所有代码塞进去** | 待查 |
| 2. README.md | 包含 6 节：①运行方式（含建库命令 + 环境变量 + 启动命令）；②Mermaid 架构图（和 Spec 4.1 同款）；③表结构说明（两张表字段逐一列）；④技术取舍说明（为什么选 asyncio Queue 不选 Celery？为什么选 MySQL 本地服务不选 Docker？）；⑤已知问题/未完成项（明确列 SSE 流式没做、公网没做）；⑥API 示例（3 个核心 curl） | 待查 |
| 3. 一键启动 | 空白 Windows 电脑（只装 Python 3.10 + MySQL 8）按 README 步骤**复制粘贴 3 条命令内能跑起来**，并且 `api_test.http` 第 0 条通 | 待试 |
| 4. API 调试文件 | 根目录有 `api_test.http`，README 说明怎么用；核心接口 curl 至少有上传/查状态/删除三条（各一个命令行直接能跑） | 待查 |

---

## 6. 不做什么（严格遵守，别花时间别加戏）

1. **不做用户/鉴权**：JWT、Session、OAuth、登录注册接口一律不写。PDF 明确说不考察。
2. **不做前端页面**：连个 Swagger UI 的 favicon 都不要改，FastAPI 自带的 `/docs` 够用。
3. **不做高并发性能优化**：不搞连接池调大（SQLAlchemy 默认就行）、不搞多 worker（uvicorn 默认 1 worker 够用）、不搞 Redis 缓存（列表页才几行数据）。正确性 + 代码清晰最重要。
4. **不做对象存储**：OSS/S3/MinIO 一律不接。就用本地 `./uploads`，PDF 说"不要求对接对象存储"。
5. **不做 LLM SSE 流式**：加分项 3 明确放弃，不要写 `StreamingResponse` / `text/event-stream` 相关代码，不要写 `summary/stream` 路由。
6. **不做公网部署**：不写 Dockerfile（docker-compose.yml 可以留 MySQL 备选，但不要打 FastAPI 的镜像）；不搞 Nginx 配置；不买云服务器。
7. **不搞复杂的微服务/分层**：不要加 `domain/`、`repository/`、`use_cases/` 等 DDD 风格目录。扁平风格 A 刚刚好。面试答辩时被问到再解释"这个项目体量用 DDD 是过度设计"。
8. **不写业务无关的"炫技"代码**：比如装饰器重试（直接写 for 循环更易读，面试你解释 for 比装饰器清楚）、元编程自动注册路由（别搞，阅读成本高）。
9. **不修改题目要求的接口路径**：必须严格是 `/v1/recordings`、`/v1/tasks/{task_id}/retry`。不要自己加 `/api` 前缀（如 `/api/v1/recordings`），不要把 recordings 改成 audios。**PDF 怎么写就怎么命名，1:1 对齐**。
10. **不造轮子**：日志用标准库 logging + `RotatingFileHandler` 就行，不要上 loguru；配置用 pydantic-settings 不要自己写 ConfigParser；HTTP 客户端用 httpx 不要自己封装 requests。

---

## 7. 风险点（最容易挂/扣分的 10 个地方，编码时反复对照检查）

### 🔴 R1：commit 历史只有一笔 → 直接扣分项

> 很多人笔试到最后才想起来要 git commit，然后 `git add . && git commit -m "finish"` 一次性全交。**PDF 明文要求「必须保留完整 commit 历史，禁止一次性提交全部代码」**，这是硬扣分点。

**规避方式**：Spec 里的 3.1~3.7 每个 P0 功能点 + 每个 P1 加分点 = 一个独立 commit。推荐 commit 节奏：

```
1. chore: init project skeleton, add requirements + health endpoint         （P0-1）
2. feat: add recordings/tasks alembic migration and SQLAlchemy models      （P0-2）
3. feat: POST /v1/recordings upload endpoint with md5 idempotent           （P0-3 + P1-4）
4. feat: asyncio queue worker and startup resume logic                     （P0-4 + P1-2 + P1-5）
5. feat: implement pipeline state machine with mock ASR and LLM summary    （P0-5 + P1-1）
6. feat: add GET list/detail tasks endpoints + retry + delete              （P0-6）
7. refactor: unified error response format and logging with task_id        （P0-7）
8. test: add 5 integration tests with pytest + httpx                       （P1-6）
9. docs: add README with architecture diagram and run steps                （交付项 2）
10. docs: add api_test.http and example curl commands                      （交付项 4）
```

一共 10 个 commit，面试官一眼看你 git log 就知道你是循序渐进写的，不是抄的。

### 🔴 R2：POST /v1/recordings 不是立即返回 → P0 必错项

> PDF 说「立即返回，不得同步等待处理完成」。如果你的实现是「先调 LLM 拿到摘要再返回 200」，**P0 直接不通过**，面试官 1 分钟就能测出来。

**规避方式**：上传接口里只做 `pipeline.enqueue_task(task_id)`（就是个 `queue.put_nowait`，毫秒级），然后立刻 return。处理是后台 worker 协程慢慢跑。

### 🔴 R3：状态机 status 只有 pending/processing/done 三态 → 扣分项

> PDF 明确说「processing 中需能体现当前阶段」。说明 status 必须是 5 态（pending/transcribing/summarizing/done/failed），不能偷懒只写三个。

**规避方式**：把 `TaskStatus` 枚举写死 5 个值，每个阶段进和出都 UPDATE tasks.status 对应值，GET /tasks/{id} 直接返回这个字段。

### 🔴 R4：DELETE 只删 DB 不删文件 / 删文件失败抛 500 → 边界没考虑

**规避方式**：Spec 4.5 已经写了：①先事务内删 DB（级联）→ ②再删文件（`missing_ok=True` 静默）。顺序 + 静默都要。

### 🔴 R5：RETRY 没加行锁 → 并发重复入队（面试 100% 会问并发测试）

**规避方式**：`SELECT ... FOR UPDATE` + 事务 + WHERE status='failed' 乐观判断。三个都要有，缺一不可。Spec 4.5 最后一条有。

### 🔴 R6：上传文件一次性 `file.read()` 读内存 → 50MB × 并发 = OOM

**规避方式**：`while chunk := await file.read(1MB): md5.update(chunk); f.write(chunk)` 分块读。Pydantic 先配置最大请求体 `MAX_UPLOAD_SIZE_MB`（FastAPI `app.add_middleware(TrustedHostMiddleware)` 或者直接在 uvicorn 启动命令加 `--limit-max-request-size 52428800` 也行，双保险）。

### 🔴 R7：LLM 只靠 prompt 没靠 `response_format` + `model_validate` 两层校验 → 遇到模型吐非 JSON 直接挂

**规避方式**：三层保险：① `response_format={"type":"json_object"}`；② `json.loads` try/except；③ loads 后 `LLMSummaryResponse.model_validate()` 校验字段存在和类型。任何一层失败都抛 LLMResponseFormatError 进入自动重试。

### 🔴 R8：启动恢复时，transcribing/summarizing 状态没重置 → 任务永远卡住

> startup 重新入队后，worker 跑阶段 1 转写时会执行 `UPDATE tasks SET status='transcribing' WHERE status='pending'` —— 如果 status 还是 transcribing（上次强杀没改回来），WHERE 条件不命中 → rowcount=0 → 任务就挂在那永远不前进。

**规避方式**：resume 函数里**先**统一 `UPDATE tasks SET status='pending' WHERE status IN ('transcribing','summarizing')`，**再**全部塞回 Queue。顺序不能反。Spec 4.5 最后一条也提了。

### 🔴 R9：错误响应结构不统一 → 有的返回 `{"detail":"..."}` 有的返回 `{"message":"..."}`

**规避方式**：所有自定义异常抛 `NotFoundException` / `ConflictException` 等（继承 `AppBaseException`），`main.py` 里**每个异常类都加一个 `@app.exception_handler`**，还有一个兜底 `@app.exception_handler(Exception)`；另外 FastAPI 默认的 `RequestValidationError`（参数 422）也要 override 成自定义结构，不然它还是返回默认的 `{"detail":[{"loc":...,"msg":...,"type":...}]}`。

### 🟡 R10：Alembic 迁移里外键没加 `ON DELETE CASCADE` → DELETE recording 时 tasks 子表删不掉报错

> SQLAlchemy 的 `ForeignKey(..., ondelete="CASCADE")` 要**在 model 里就写好**，不然 Alembic autogenerate 生成的 DDL 是 `ON DELETE RESTRICT`，删 recording 会抛子表有数据的 IntegrityError。

**规避方式**：models.py 的 `Task.recording_id = Column(String(36), ForeignKey("recordings.id", ondelete="CASCADE"), ...)`。`alembic upgrade head` 后 `SHOW CREATE TABLE tasks` 检查一下外键 CONSTRAINT 里确实有 `ON DELETE CASCADE` 才算过。

---

## 下一步

> 本 Spec 确认无误后，按第 3 节的功能点顺序 P0-1 → P0-2 → ... → P0-7 → P1-* → 文档收尾，严格按第 7 节 R1 的 commit 节奏提交。编码开始前不再做额外需求讨论，除非遇到和 Spec 冲突的实际代码问题。
