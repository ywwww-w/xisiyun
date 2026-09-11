# scripts_tmp / 开发期临时验证脚本

本目录是开发过程中**逐 ticket 验证**用的临时脚本，**不是最终交付的自动化测试套件**。

## 正式测试在哪？

正式的 pytest 集成测试统一放在 [**`tests/`**](../tests/) 目录下：

| 脚本 | 说明 |
|------|------|
| [`tests/conftest.py`](../tests/conftest.py) | 测试基础设施：SQLite 内存库 / uploads 隔离 / LLM 与 pipeline 全 Mock / FastAPI lifespan 手动管理 |
| [`tests/test_smoke_fixtures.py`](../tests/test_smoke_fixtures.py) | 6 条冒烟用例：health / DB 隔离 / uploads 隔离 / pipeline mock 走完全流程 / retry 409 |
| [`tests/test_api.py`](../tests/test_api.py) | 5 条核心集成用例：上传 / MD5 幂等 / 全流水线成功 / Retry 4 态串行 / DELETE 级联三边界 |

## 本目录里的脚本各是啥（面试演示用，可以删吗？）

| 脚本 | 对应 Ticket | 做什么 | 保留建议 |
|------|------------|--------|---------|
| `test_storage_md5.py` | T4 | 流式 MD5 计算 + seek(0) 回绕正确性 | 演示用，建议保留 |
| `test_storage_size.py` | T4 | 413 超限文件自动清理残留 | 演示用，建议保留 |
| `test_storage_delete.py` | T4 | 文件不存在时 delete 幂等不抛 500 | 演示用，建议保留 |
| `test_schemas.py` | T5 | Pydantic v2 12 个模型校验（分页上限 / LLM 三键 / 泛型） | 演示用，建议保留 |
| `test_upload_api.py` | T6 | 上传路由 4 种场景（正常 / 幂等 / 413 / 415） | 演示用，建议保留 |
| `test_pipeline.py` | T7 | Queue / Semaphore / startup 入队恢复 结构验证 | 演示用，建议保留 |
| `test_llm.py` | T8 | LLM 三层校验 / 空 Key / 超时 / 401 / 各种 Markdown 围栏剥离 | 演示用，建议保留 |
| `test_pipeline_state_machine.py` | T9 | 状态机 5 验收：A1 成功流 / A2 stage1 3×退避 / A3 LLM 401×3 / A4 乐观锁 rowcount=0 / A5 Resume 仅 pending | 演示用，建议保留 |
| `test_tasks_api.py` | T10 | tasks 路由 5 验收：4 阶段 / failed→200 retry / 4 态 409 / 并发双 409 / 404 | 演示用，建议保留 |
| `test_recordings_api.py` | T11 | recordings 路由 5 验收：25 条分页 / 状态屏蔽 transcript / 级联删除 / missing_ok 204 / 404 | 演示用，建议保留 |
| `test_assets/` 子目录见 | T14 | `sample.wav`(1KB 假 WAV) / `wrong.txt` / `51mb_fake.mp3`(.gitignore) | **保留**（api_test.http 要用） |

> 面试时如果面试官问"为什么有两套测试"，直接回答：scripts_tmp 是开发过程中"每个 ticket 写完立刻验"的 TDD 沙箱，单跑快、易读；tests/ 是最终交付的统一 pytest 套件，严格 SQLite 内存隔离，全 mock，保证 CI 上零 flaky。两者功能 95% 重合，但粒度和使用场景不同。**不要删 scripts_tmp/**，面试官现场说"给我演示一下 LLM 三层校验错在哪" —— 直接 `python scripts_tmp/test_llm.py` 就能跑，比翻 tests/test_api.py 里的 5 条用例快得多。
