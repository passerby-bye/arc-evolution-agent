# arc-evolution-agent

Arc-Bench 黑客松决赛（hackathon-evolution）提交的演进 agent，最终版本 v4x。

决赛任务是在一个已经存在的 Web 应用上做"演进"：每个任务有 10 个目标功能，其中 5 个是修改已有功能，
5 个是新增功能。平台用 Playwright 私有测试评分，每个功能 3 个场景，共 30 个测试。
agent 要在保留原有功能的前提下完成这 10 个功能，并预置测试场景用到的种子数据。

## 流程

入口是 `main.py`。

1. **准备**：清理上次运行留下的数据文件，建立 git 基线。
2. **基线快照**：用浏览器打开原应用的页面，记录每页的控件，作为回归检查的依据（`snapshot.py`）。
3. **选目标功能**：带 "Original Feature Description" 的是修改功能，其余是新增功能。
4. **修复阶段**：原应用里如果有需求控件只存在于没被接入的模块中，先把这些模块接回去
   （`repair.py`、`graph.py`）。
5. **种子数据**：一次请求写入场景 GIVEN 里点名的预置记录，代码逐条核对是否存在。
6. **逐个功能**：新增功能先做。
   - 新增功能交给 Octos 内核的工具循环实现（`octos_feature.py`、`octos_stdio.py`）。
   - 修改功能用两步编辑：先按代码索引定位，再对编号的代码片段做按行编辑
     （`change.py`、`edits.py`、`code_index.py`）。
   - 每个功能生成场景测试并运行（`structured_tests.py`、`qa.py`），失败就带着失败步骤、
     页面文字和页面错误进入修复轮。改坏应用的功能会回退到上一个 git 提交。
7. **收尾**：清理遗留问题，再跑一轮 QA 审查（`wholeapp.py`、`audit_gen.py`）。

## 运行

依赖 Python 3.10+、Node.js、Playwright 浏览器，以及 `pip install -r requirements.txt`。

```bash
export OPENAI_BASE_URL=...   # OpenAI 兼容接口
export OPENAI_API_KEY=...
export MODEL=deepseek-v4-flash-vision-exp
python main.py <requirements 目录> --output-dir <原应用目录>
```

原应用目录下要有 `frontend/` 和 `backend/`。常用的可选环境变量：

| 变量 | 作用 |
|---|---|
| `EVO_OCTOS` | 设为 `0` 时新增功能也用两步编辑，不用 Octos |
| `EVO_OCTOS_TURN_SECONDS` | Octos 单轮时长上限，默认 1200 秒 |
| `EVO_ONLY` | 只做指定的功能，逗号分隔，用于本地实验 |
| `EVO_SKIP_QA` | 跳过收尾 QA |
| `OCTOS_BIN` | 使用本地的 Octos 可执行文件，不下载 |

## 平台成绩

| 任务 | 通过 |
|---|---|
| sheet | 21/30，另一次运行 14/30 |
| github | 0/30 |

github 0 分的原因已经查明：首页 `<main>` 里缺 "Sign in" 链接，页头缺 "Search" 搜索框。
这两项检查在修复阶段就报出来了，但之后被当作已知问题过滤掉，最后的清理阶段又因为没时间没有执行。
同一个包两次运行分数差别很大，主要因为每个功能耗时不固定，排在后面的功能会因为没时间被跳过。

## 许可

Apache License 2.0，第三方和模板代码见 `NOTICE`。
