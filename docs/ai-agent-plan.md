# AI Agent 接入方案 v2（对话式 · 版本矩阵驱动）

> **v2 修订说明**：v1 曾建议把 AI 做成「步骤8→9 之间的旁路分析」。经澄清，真实痛点不在审核效率，而在
> **版本信息的录入与核对**。因此方向调整为：
> 1. 形态从「定点旁路分析」改为「**任务级常驻对话式 Agent**」，全流程可用；
> 2. 核心场景锁定**版本配套表识别 → 真实环境版本识别 → 两者对比审核**这条主线。

---

## 一、业务主线：一份版本矩阵的三次流转

把用户真正要做的事抽象出来，会发现三个环节在围绕**同一份数据结构**打转：

| 环节 | 用户动作 | 产物 | 语义 |
|---|---|---|---|
| **3 版本配套** | 上传「版本配套表」 | 16 个版本字段写入 `step3.ext_data` | **计划值**（本次测试应当用什么版本） |
| **4 环境确认** | 上传「真实环境版本信息」 | 结构化矩阵写入 `step4.ext_data.version_matrix` | **实测值**（环境里实际装的什么版本） |
| **5 环境审核** | PL 看对比结果做决策 | 差异清单 + 风险解读 | **计划 vs 实测 的偏差** |

**关键抽象**：步骤3 和步骤4 是同一个「版本矩阵提取器」的两次调用，区别只在于结果写到哪个 step。
一套解析管线复用两次，而不是写两套逻辑。

```
版本配套表 ──AI提取──► 步骤3 ext_data（计划值）──┐
                                                  ├──► 步骤5 对比审核 ──► 差异清单 + 风险解读
真实环境版本 ──AI提取──► 步骤4 ext_data（实测值）─┘
```

---

## 二、形态：任务级常驻对话式 Agent

不是一个按钮、不是一次性分析，而是**任务详情页的常驻侧栏对话窗**。

### 2.1 为什么是对话式

版本表这东西没有标准格式——不同产品线、不同版本负责人导出的表头千差万别，「GDR 版本」可能被写成
`GDR`、`gdr_ver`、`GDR驱动版本`、`通信库GDR`。规则匹配必然漏，只有让模型理解语义才稳。

而语义理解这件事，**人需要在环**：AI 填错了要能说"GDR 那行填错了，应该是 1.5.2"，重新提取；
AI 看不懂表头要能追问"你这个表的第三列是什么"。这天然是对话，不是一次性 API 调用。

### 2.2 上下文自动感知

Agent 不是无状态聊天机器人，它始终知道三件事：

```
system prompt = 当前任务上下文（自动注入，用户无需描述）
  ├─ 任务基本信息：名称/编号/产品/项目/测试类型/周期
  ├─ 当前所处环节（step 1~10）与该环节的准出条件
  ├─ 当前用户在本任务中的角色（PL / TSE / 版本负责人 / 执行人）
  └─ 当前环节已有的 ext_data（比如步骤3 已填了哪些版本字段、哪些还空着）
```

所以版本负责人在步骤3 直接丢一张表说"帮我填一下"，Agent 就知道要填哪 16 个字段、
哪些已经填过、该往哪写。不需要用户解释任何背景。

### 2.3 模型可切换

支持同时配置多个 Provider，会话内可切换（用户要求：本地部署 + 厂商 API 都要能接）：

```python
# backend/app/agent/providers.yaml（或走环境变量注入）
providers:
  local-qwen:
    type: openai_compat          # vLLM / Ollama 新版 / Xinference 都兼容
    base_url: http://192.2.100.x:8000/v1
    api_key: ""                  # 内网可不鉴权
    models: ["Qwen2.5-72B-Instruct", "Qwen2.5-VL-72B-Instruct"]
    vision: true                 # 支持读图，用于截图类表格
    data_policy: internal        # 内网数据可全量送入

  vendor-deepseek:
    type: openai_compat
    base_url: https://api.deepseek.com
    api_key: ${DEEPSEEK_API_KEY}
    models: ["deepseek-chat"]
    vision: false
    data_policy: external        # 外发前必须脱敏

  vendor-doubao:
    type: openai_compat
    base_url: https://ark.cn-beijing.volces.com/api/v3
    api_key: ${ARK_API_KEY}
    models: ["doubao-pro-32k", "doubao-vision-pro-32k"]
    vision: true
    data_policy: external
default: local-qwen
```

**数据分流策略**：`data_policy: external` 的 Provider 在发送前强制走 `redact()`，
把环境 IP、主机序列号、MAC、疑似口令替换成占位符；`internal` 的不过滤。
这样"内网模型处理敏感数据、公有云模型处理通用问答"是自动生效的，不依赖用户自觉。

---

## 三、P0 场景一：版本配套表识别填充（步骤3）

### 3.1 端到端流程

```
① 用户在步骤3 的 AI 侧栏上传「版本配套表」或直接粘贴表格文本
      ↓
② 后端解析成结构化文本（保留表格行列关系）
      ↓
③ LLM 提取 → 调用 fill_version_fields 工具，输出 16 字段 JSON（带置信度）
      ↓
④ 后端不写库，返回 preview_id + 字段级 diff 预览
      ↓
⑤ 前端渲染对照表：字段名 | AI识别值 | 当前值 | 置信度 | 勾选框
      ↓
⑥ 用户勾选确认 → POST /apply → 写入 step3.ext_data → 表单实时刷新
```

### 3.2 文件解析（四种输入统一归一）

| 输入形式 | 处理方式 | 依赖 |
|---|---|---|
| `.xlsx` / `.xlsm` | openpyxl 逐 sheet 转 Markdown 表格 | 新增 `openpyxl==3.1.2` |
| `.csv` | 标准库 csv 转 Markdown 表格 | 无 |
| 图片 `.png/.jpg` | base64 直送视觉模型，跳过文本解析 | 无（需 Provider 支持 vision） |
| 粘贴文本 | 原样送入 | 无 |
| `.pdf` / `.docx`（P2） | pdfplumber / python-docx | 后置 |

统一转成 Markdown 表格再送模型——这个格式对 LLM 最友好，token 也最省。
解析结果存 `step_files.parsed_text`，避免重复解析。

### 3.3 字段映射：给模型的锚点

步骤3 的 16 个字段是提取的目标 schema。给模型的不是干巴巴的 key，而是 **key + 中文名 + 常见别名**：

```python
VERSION_FIELDS = [
  {"key": "target_version",       "label": "被测版本信息", "aliases": ["被测版本","主版本","测试版本","版本"]},
  {"key": "hdm_version",          "label": "HDM版本",      "aliases": ["HDM","HDM固件"]},
  {"key": "bios_version",         "label": "BIOS版本",     "aliases": ["BIOS","BIOS固件"]},
  {"key": "model_image_version",  "label": "模型镜像版本", "aliases": ["镜像版本","模型镜像","镜像"]},
  {"key": "comm_lib_version",     "label": "通信库版本",   "aliases": ["通信库","集合通信库","HCCL","NCCL"]},
  {"key": "gdr_version",          "label": "GDR版本",      "aliases": ["GDR","GDR驱动","gdr_ver"]},
  {"key": "switch_node_version",  "label": "交换节点版本", "aliases": ["交换节点","Switch节点"]},
  {"key": "os_version",           "label": "OS版本",       "aliases": ["操作系统","OS","内核版本"]},
  {"key": "gpu_driver_version",   "label": "GPU驱动版本",  "aliases": ["GPU驱动","显卡驱动","Driver"]},
  {"key": "gpu_firmware_version", "label": "GPU固件版本",  "aliases": ["GPU固件","显卡固件","FW"]},
  {"key": "nic_driver_version",   "label": "网卡驱动版本", "aliases": ["网卡驱动","NIC驱动"]},
  {"key": "nic_firmware_version", "label": "网卡固件版本", "aliases": ["网卡固件","NIC固件"]},
  {"key": "mb_cpld_version",      "label": "主板CPLD版本", "aliases": ["主板CPLD","MB CPLD","CPLD"]},
  {"key": "switch_cpld_version",  "label": "Switch CPLD版本","aliases":["Switch CPLD","交换CPLD"]},
  {"key": "retimer_version",      "label": "retimer版本",  "aliases": ["retimer","Retimer","重定时器"]},
  {"key": "stress_tool_version",  "label": "压测工具版本", "aliases": ["压测工具","测试工具版本"]},
]
```

别名表是**活的**——运行中发现新的表头写法，往 `ai/version_fields.py` 里加一条即可，不需要改 prompt。

### 3.4 输出契约

```json
{
  "fields": [
    {"key": "gdr_version", "value": "1.5.2", "confidence": 0.95,
     "source": "表格第 7 行 C 列「GDR驱动」"},
    {"key": "retimer_version", "value": null, "confidence": 0.0,
     "source": null, "note": "配套表中未找到 retimer 相关行"}
  ],
  "unmapped_rows": [{"row": "板卡序列号: SN12345", "reason": "非版本字段，已忽略"}],
  "warnings": ["表中存在两个 GPU 驱动版本号（第 3 行和第 11 行），已取第 3 行的值"]
}
```

三个字段是设计的关键：
- `confidence` — 前端按阈值染色（≥0.9 绿 / 0.6~0.9 黄 / <0.6 红且默认不勾选）
- `source` — 告诉用户这个值从表里哪来的，用户一眼能核对，**这是建立信任的核心**
- `unmapped_rows` / `warnings` — 让 AI 主动交代它忽略了什么、哪里有歧义，而不是闷头猜

### 3.5 安全红线：写操作必须 human-in-the-loop

`fill_version_fields` 工具**不直接写数据库**。它返回 preview，由用户勾选确认后才落库。
Agent 在本项目里的定位是「**有工具的手，没有笔的权**」——能读能算能建议，写库必须人点头。

---

## 四、P0 场景二：真实环境版本识别 + 对比审核（步骤4 / 步骤5）

### 4.1 步骤4：复用同一套提取器

版本负责人在步骤4 上传「真实环境版本信息」（自动化脚本采集输出 / 手工导出的环境清单），
走**与步骤3 完全相同的解析和提取管线**，结果写入 `step4.ext_data.version_matrix`，
同时在 `step_files` 存一份归档（`category='version_scan'`）便于追溯原始文件。

### 4.2 步骤5：对比审核 —— 代码算差异，AI 做解读

这里有一条重要架构原则：

> **能用代码确定性计算的，绝不交给模型。**

字段级 diff 是纯字符串比较，代码做 100% 准确、零成本、可复现。让模型去比对两个版本号
反而会漏、会幻觉。所以分工是：

```
【代码层】deterministic diff           【AI 层】语义归一 + 风险解读
  逐字段比较 step3 vs step4      →      把归一化后的差异交给模型
  输出：一致 / 不一致 / 单侧缺失        产出：
                                         · 版本号语义归一（"v1.5.2-rel" ≡ "1.5.2"）
                                         · 差异项的风险等级与影响面
                                         · 处置建议（重刷环境 / 更新配套表 / 可放行）
                                         · 给 PL 的审核结论建议
```

归一化规则（代码层，可测试）：去前缀 `v`/`V`、去 `-release`/`-rel`/`-ga` 后缀、
去除前后空格与全角字符、统一大小写。归一化后仍不一致的，才交给模型判断是否语义等价。

### 4.3 差异清单输出

```json
{
  "summary": "16 项版本字段中 13 项一致，2 项不一致，1 项实测缺失",
  "verdict": "not_ready",
  "diffs": [
    {"key": "gdr_version", "label": "GDR版本",
     "planned": "1.5.2", "actual": "1.4.8", "normalized_equal": false,
     "severity": "high",
     "impact": "GDR 版本低于配套要求，多机 allreduce 带宽可能不达标，直接影响性能测试结论有效性",
     "suggestion": "重刷 GDR 至 1.5.2 后重新执行性能测试",
     "source_plan": "配套表第 7 行", "source_actual": "环境采集 2026-08-30 15:20"}
  ],
  "missing_in_actual": [{"key": "retimer_version", "label": "retimer版本", "planned": "2.1.0"}],
  "review_checklist": [
    "确认 GDR 1.4.8 是否为环境刷写遗漏",
    "retimer 版本实测缺失，需补充采集或说明原因"
  ]
}
```

### 4.4 步骤5 前端呈现

PL 打开步骤5，无需点任何按钮，AI 侧栏自动给出：

```
┌─ 版本一致性审核 ─────────────────────────┐
│  ● 未通过  ·  13 一致 / 2 不一致 / 1 缺失  │
├──────────────────────────────────────────┤
│  ✕ GDR版本        计划 1.5.2 → 实测 1.4.8 │
│    高风险 · 建议重刷 GDR 后重跑性能测试    │
│  ✕ 网卡驱动版本    计划 5.9 → 实测 5.8     │
│    中风险 · 建议确认驱动兼容性矩阵        │
│  ? retimer版本    计划 2.1.0 → 实测 缺失   │
│    需补充采集                            │
├──────────────────────────────────────────┤
│  🤖 AI 生成，仅供参考，结论以人工审核为准  │
└──────────────────────────────────────────┘
```

差异项可直接跳到步骤3/步骤4 对应字段，也支持「按建议更新配套表」的一键回填（仍需确认）。

---

## 五、架构设计

### 5.1 模块结构

```
backend/app/
├── agent/                          # AI Agent 能力层（新增）
│   ├── __init__.py
│   ├── config.py                   # 多 Provider 配置 + 启动时校验
│   ├── providers/
│   │   ├── base.py                 # LLMProvider 抽象：chat / vision / tool_call
│   │   ├── openai_compat.py        # OpenAI 兼容（vLLM/Ollama/DeepSeek/豆包/通义…）
│   │   └── registry.py             # 按名取 Provider，含健康检查与故障切换
│   ├── tools/
│   │   ├── base.py                 # Tool 注册：name / json_schema / handler / 权限
│   │   ├── context_tools.py        # 只读：get_task_context / get_step_data / get_case_stats
│   │   ├── version_tools.py        # 提取：extract_version_matrix（输出 preview，不写库）
│   │   └── compare_tools.py        # 比对：diff_version_matrices（代码层确定性计算）
│   ├── version_fields.py           # 16 字段定义 + 别名表（活的映射字典）
│   ├── normalize.py                # 版本号归一化（纯函数，单测覆盖）
│   ├── redact.py                   # 外发脱敏
│   ├── prompts.py                  # Prompt 模板 + 版本号
│   ├── runtime.py                  # Agent 主循环：LLM ↔ Tool，最多 5 轮
│   └── schemas.py                  # 输入输出的 Pydantic 契约
├── services/
│   └── agent_service.py            # 会话/消息编排、预览态、落库
├── api/
│   └── agent.py                    # 路由（SSE 流式）
└── models/
    └── models.py                   # 追加 4 个模型
```

### 5.2 数据模型

```python
class AgentSession(Base):
    """一次对话，绑定任务与环节"""
    __tablename__ = "agent_sessions"
    id, task_id, step, user_id
    provider, model                  # 记录实际使用的，便于复盘
    title                            # 首条消息自动摘要
    created_at, updated_at

class AgentMessage(Base):
    __tablename__ = "agent_messages"
    id, session_id, role             # user / assistant / tool
    content(Text)
    tool_calls(JSON)                 # 模型发起的工具调用
    tool_call_id, name               # 工具结果回填用
    token_usage(JSON)
    created_at

class AgentAttachment(Base):
    """会话内上传的文件（版本配套表 / 环境版本清单）"""
    __tablename__ = "agent_attachments"
    id, session_id, task_id, step
    original_name, stored_name, file_size, mime_type
    parsed_text(Text)                # 解析后的 Markdown 表格，避免重复解析
    parse_method                     # openpyxl / csv / vision / plain
    uploaded_by, uploaded_at

class VersionExtraction(Base):
    """版本矩阵提取与比对的历史记录（可追溯 + 可重放）"""
    __tablename__ = "version_extractions"
    id, task_id, step                # 3=配套表 / 4=环境实测
    source_file_id                   # → step_files.id 或 agent_attachments.id
    provider, model, prompt_version
    matrix(JSON)                     # 提取结果（含置信度与来源）
    applied_fields(JSON)             # 用户实际确认写入了哪些字段
    applied_by, applied_at
    created_at
```

建表无需迁移脚本——项目启动时 `Base.metadata.create_all` 自动建表（`backend/app/db/database.py:36`），且不影响存量表。

### 5.3 接口约定

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/agent/providers` | 可用模型列表（含 vision 能力标记） |
| `POST` | `/api/tasks/{id}/agent/sessions` | 建会话，body `{step}` |
| `GET` | `/api/tasks/{id}/agent/sessions` | 会话列表 |
| `GET` | `/api/agent/sessions/{sid}/messages` | 消息历史 |
| `POST` | `/api/agent/sessions/{sid}/messages` | 发消息，**SSE 流式**返回 token 与工具调用过程 |
| `POST` | `/api/agent/sessions/{sid}/attachments` | 上传版本表/截图 |
| `POST` | `/api/agent/extractions/{eid}/apply` | **确认写入** `ext_data`（human-in-the-loop 的落点） |
| `GET` | `/api/tasks/{id}/agent/version-diff` | 步骤5 用的版本对比结果（步骤3 vs 步骤4） |
| `DELETE` | `/api/agent/sessions/{sid}` | 删除会话 |

**流式用 SSE 而非 WebSocket**：Agent 交互是「一问一答」，SSE 单向流足够，实现和运维都轻得多。
现有前端鉴权是 `Authorization` header（`api()` 封装），`EventSource` 不支持自定义 header，
所以用 `fetch` + `ReadableStream` 手动读 SSE，复用现有鉴权逻辑，不把 token 放 query string。

### 5.4 前端交互

任务详情页改造为**左右分栏**：左侧原有内容（任务信息 + 步骤导航 + 步骤表单），右侧 320px 常驻 AI 侧栏，可折叠。

侧栏组成：
- 顶部：模型选择器（本地 Qwen / DeepSeek / 豆包…）+ 新建会话
- 中部：消息流。AI 消息支持富文本块——**版本字段预览表**（带勾选框）、**差异清单**、**来源引用**
- 底部：输入框 + 上传按钮 + 快捷指令（根据当前步骤动态变化）

快捷指令按步骤动态渲染，这是「全流程可用」又不显臃肿的关键：

| 当前环节 | 侧栏快捷指令 |
|---|---|
| 步骤3 版本配套 | 「上传版本配套表并填写」「帮我检查哪些字段还没填」 |
| 步骤4 环境确认 | 「上传真实环境版本信息」「与配套表比对」 |
| 步骤5 环境审核 | 「生成版本一致性审核结论」「这项差异要不要打回」 |
| 步骤7 执行测试 | 「分析失败用例」「这个报错是什么意思」 |
| 步骤8 测试完成 | 「检查材料是否齐全」「生成结论初稿」 |
| 其他环节 | 「这个任务现在到哪一步了」「下一步我该做什么」 |

### 5.5 工程硬约束

1. **写操作 human-in-the-loop** — 所有写库动作走「preview → 用户确认 → apply」两段式。Agent 永不直接改业务数据。
2. **代码能算的不交给模型** — 版本号归一化、字段 diff 都是纯函数，单测覆盖；模型只做语义判断和风险解读。
3. **外发脱敏** — `data_policy: external` 的 Provider 强制过 `redact()`：环境 IP、SN、MAC、疑似口令、绝对路径。
4. **密钥不入库** — Provider 的 `api_key` 走环境变量引用（`${DEEPSEEK_API_KEY}`），`.env` 已在 `.gitignore`。
5. **上下文预算** — 表格解析文本上限 8000 字符（超出按 sheet 截断并提示）；整表 token 超限时改为「分块提取 + 合并」。
6. **超时与降级** — 单次模型调用 60s；工具调用单个 10s；Provider 失败自动切备用 Provider 并提示用户。
7. **全程可追溯** — 记录 provider / model / prompt_version / 提取矩阵 / 实际写入字段。出问题能精确重放。
8. **权限对齐** — Agent 能读的数据 = 该用户在本任务中能看到的数据；写操作额外校验环节权限（复用 `TaskService` 的权限逻辑）。
9. **不阻塞主流程** — 侧栏是旁路，任何 AI 故障都不影响任务步骤的正常推进。

---

## 六、开发计划

### P0 — 对话框架 + 版本配套表识别（约 8 人日）

| # | 任务 | 产出 |
|---|---|---|
| 1 | `agent/providers/` 多 Provider 适配层 | 本地 + 厂商，可切换，含健康检查 |
| 2 | 4 张表 + 启动建表 | `agent_sessions` / `agent_messages` / `agent_attachments` / `version_extractions` |
| 3 | `tools/base.py` + `runtime.py` | Tool 注册与 Agent 主循环 |
| 4 | 文件解析（xlsx / csv / 图片 / 文本） | 统一转 Markdown 表格 |
| 5 | `version_fields.py` + `extract_version_matrix` | 16 字段提取，带置信度与来源 |
| 6 | 预览 → apply 两段式落库 | human-in-the-loop 闭环 |
| 7 | `api/agent.py` + SSE 流式 | 接口层 |
| 8 | 前端常驻侧栏 + 版本预览表 | 勾选、染色、来源展示 |
| 9 | docker-compose 注入 Provider 环境变量 | 部署闭环 |

**验收**：步骤3 上传一份真实版本配套表 → 侧栏 10 秒内给出 16 字段预览和来源标注 → 勾选确认后表单字段被正确填充 → 刷新页面数据仍在。

### P1 — 环境版本识别 + 对比审核（约 5 人日）

- 步骤4 上传环境实测版本，复用提取器写入 `step4.ext_data.version_matrix`
- `normalize.py` 版本号归一化 + 单测
- `diff_version_matrices` 确定性比对（代码层）
- AI 语义归一 + 风险解读 + 处置建议
- 步骤5 审核卡片与差异高亮

### P2 — 全场景扩展（约 6 人日）

- 步骤7 失败用例归因 + 一键生成 `task_issues` 草稿
- 步骤8 材料完整性检查
- 步骤2 用例推荐（基于方案链接与用例库）
- PDF / Word 文档解析支持

---

## 七、待确认的三个点

1. **真实环境版本信息是怎么收集的？** 自动化脚本产出固定格式文件（那我按格式写解析器，最准），
   还是人工整理后上传（那就仍走 AI 提取）？**给一份真实样本我就能定死解析规则。**
2. **版本配套表是否有模板？** 如果有标准模板，提取准确率能到 95%+ 且几乎不需要别名表；
   如果各产品线自由发挥，就靠别名表 + 模型语义理解兜底。
3. **本地模型部署在哪？** 需要 `base_url` 和模型名，以及是否支持 vision（决定截图类表格能不能处理）。
   另外确认一下从 `testtask-backend` 容器到模型服务的网络连通性。

---

## 八、风险与兜底

| 风险 | 影响 | 兜底 |
|---|---|---|
| 表头写法千奇百怪，提取错 | 版本信息填错，影响测试有效性 | 置信度染色 + 来源标注 + 强制人工确认；别名表持续补充 |
| 模型把版本号看串行 | 张冠李戴 | `source` 字段强制模型交代取值位置，用户一眼可核 |
| 敏感信息外发 | 安全事件 | `data_policy` 自动分流 + `redact()` 强制脱敏；默认走内网模型 |
| 本地模型不可用 | Agent 不可用 | Provider 故障自动切换；AI 是旁路，流程照常推进 |
| 用户过度信任 AI | 错误决策 | 每条 AI 输出强制标注「AI 生成，仅供参考」；写操作必须人工确认 |
| 幻觉版本号 | 编造不存在的版本 | 提取结果必须在原表文本中能检索到（代码层校验），检索不到则标为低置信度 |
