# 把测试任务系统做成 MCP Server

> 思路：不做系统内的 AI 功能，而是把系统能力以 MCP 协议暴露出去，让外部 Agent 来调用。
> 「用户想怎么用他自己搞」。

---

## 一、为什么这个思路更对

前面讨论的三个方案都在试图**在系统内交付 AI 能力**，成本高在两处：AI 交互界面（侧栏/对话/流式，约 4 人日）
和模型适配（多 Provider、健康检查、故障切换，约 1 人日）。MCP 把这两块整体移出去了。

| 我之前的困境 | MCP 方案下 |
|---|---|
| 对话式还是批处理式？交互形态怎么选？ | 不用选，用户用自己的 Agent 客户端决定 |
| 本地模型还是公有云？要不要写适配层？ | 不用管，模型在 Agent 客户端那边 |
| 前端侧栏 + SSE 流式 + 会话持久化（≈4 人日） | 全部消失，零前端改动 |
| 框架要 3 个场景才回本 | 无框架沉没成本，加一个工具就是一份独立增量 |
| 会话/消息要存表 | Agent 客户端自己管，我一张新表都不用加 |

**成本对比**

| 项目 | 系统内对话式 Agent | MCP Server |
|---|---|---|
| AI 交互界面 | 2.0 | 0 |
| 多 Provider 适配 | 1.0 | 0 |
| Tool calling runtime | 1.5 | 0.3（官方 SDK 承担） |
| 会话 / 消息持久化 | 0.5 | 0 |
| 新表 | 0.5 | 0 |
| 业务工具实现 | 1.5 | 2.0 |
| 鉴权与权限对齐 | — | 1.0 |
| **合计** | **8.0 人日** | **3.3 人日** |

而且 MCP 这 3.3 人日里，2 人日是在实现**真正有价值的业务工具**。

**还有一个之前没意识到的优势：Agent 在系统外，能跨系统取证。**
系统内 Agent 只能看到我库里的数据；接了 MCP 的外部 Agent 可以同时连日志系统、设备管理系统、禅道——
查失败原因时能拿到我根本没有的信息。这个能力在系统内做不出来。

---

## 二、一个重要的分工原则：工具返回数据，不返回结论

比方说版本差异：

```
✅ 工具返回：  gdr_version  计划 1.5.2  实测 1.4.8  归一化后不等  步骤3来源：配套表第7行
❌ 工具返回：  GDR 版本不匹配，高风险，建议重刷 GDR 后重跑性能测试
```

理由有三：

1. **判断权属于 Agent。** 它可能同时连着设备管理系统，知道这台机器下周要重刷固件——那"建议重刷"就是错的。
2. **工具保持中立和可测试。** 返回纯数据，单测好写，行为可预测。
3. **避免我在系统内硬编码业务判断。** 那些判断换个产品线可能就不成立了。

同理，**版本号归一化和字段 diff 仍然是纯代码**（`normalize.py`），工具只返回确定性结果，
语义解读交给 Agent。这条原则在所有方案里都不变。

---

## 三、工具清单

### 第一批：只读工具（1.5 人日，默认全开）

| 工具 | 说明 | 复用 |
|---|---|---|
| `list_my_tasks` | 我的任务，支持 `pending` / `done` 过滤 | `TaskService.get_tasks` + 现有 pending 逻辑 |
| `get_task_detail` | 任务详情：当前环节、负责人、进度、周期 | `TaskService.get_task` |
| `get_step_data` | 指定环节的 `ext_data`（版本矩阵、报告链接等） | 直接读 `TaskStep` |
| `list_linked_cases` | 任务关联用例 + 结果概览 | `cases.list_linked_cases` |
| `get_case_results` | 用例执行明细，失败/阻塞的带步骤级 `step_results` | `CaseResult` |
| `get_version_matrix` | 步骤3 计划值 + 步骤4 实测值，一次拿全 | `TaskStep.ext_data` |
| `diff_version_matrix` | **纯代码**做版本一致性比对，返回差异表 | `normalize.py` + diff |
| `list_issues` | 问题记录列表 | `TaskIssue` |

只读工具零风险，先做完就能用起来。

### 第二批：写工具（1.0 人日，默认关闭，需环境变量显式开启）

| 工具 | 说明 | 安全设计 |
|---|---|---|
| `fill_version_fields` | 填版本字段 | `dry_run` **默认 true**，先返回 diff 预览 |
| `create_issue` | 创建问题单 | 校验环节权限 |
| `update_case_result` | 记录用例执行结果 | 校验执行人身份 |
| `advance_step` | 推进环节 | 校验 `assigned_to`，拒绝越权 |

**写工具默认全部不注册**，需要在环境变量里显式打开（`MCP_ENABLE_WRITE_TOOLS=true`）。
这样即使用户的 Agent 客户端设了"自动批准工具调用"，也不会误操作业务数据。

### 第三批：Resource（0.3 人日，可选）

- `testtask://task/{id}/summary` — 任务全貌的 Markdown 摘要
- `testtask://task/{id}/failures` — 失败用例聚合视图

Resource 是 MCP 里"只读数据"的一等公民，Agent 可以直接把它当上下文读进来，比调工具更省事。

---

## 四、技术选型

### SDK：用独立的 `fastmcp` 包，不要用 `mcp.server.fastmcp`

官方 `mcp` SDK 内置的 `mcp.server.fastmcp` 模块已趋于维护态，独立包 `fastmcp`（v3.x）是活跃维护的继任者。
避免版本冲突，直接从 `fastmcp` 导入：

```python
from fastmcp import FastMCP

mcp = FastMCP("testtask")

@mcp.tool()
async def diff_version_matrix(task_id: int) -> dict:
    """比对步骤3 计划版本与步骤4 实测版本，返回差异表（纯计算，不做业务判断）"""
    ...
```

依赖只需加一行：`fastmcp>=3.0`。项目是 Python 3.11（`backend/Dockerfile`），满足要求。

### 传输方式：以 Streamable HTTP 为主

**系统是服务端部署的（192.2.100.30），不是本地应用，所以 stdio 模式并不合适。**

- **Streamable HTTP（推荐，主用）**：挂到现有 FastAPI 上，`/mcp` 路径，零新增容器
  ```python
  # main.py
  @contextlib.asynccontextmanager
  async def lifespan(app: FastAPI):
      async with contextlib.AsyncExitStack() as stack:
          await stack.enter_async_context(mcp.session_manager.run())
          yield

  app.mount("/mcp", mcp.streamable_http_app())
  ```
- **stdio（仅本地调试用）**：`python -m app.mcp_stdio`，用 MCP Inspector 测

> **已知坑**：Claude Desktop 的 `claude_desktop_config.json` **只接受 stdio 条目**，
> 直接填 `url` 会导致整个 `mcpServers` 块被静默丢弃。远程 MCP 要走 Connectors UI，
> 或用 `mcp-remote` 做 stdio 桥接。文档里要给足这块的示例，否则用户第一个坑就卡死。

老的 HTTP+SSE 传输（`/sse` + `/messages` 双端点）已在 2025-03-26 规范中废弃，2026 年各厂商陆续下线，**不要用**。

### 鉴权

HTTP 模式下复用现有 JWT：

```
Authorization: Bearer <现有登录 JWT>
```

Token 解析出 `current_user`，**所有工具调用都带上 user 上下文**，走 `TaskService` 同一套权限校验。
不开任何后门——这是硬要求，否则 MCP 就成了绕过角色权限的通道。

### 数据脱敏分级

Agent 可能把数据送到任何地方（包括公网模型），所以工具返回值默认脱敏：

| 字段 | 默认 | 开启条件 |
|---|---|---|
| 环境 IP、主机名 | `192.168.*.*` | `include_sensitive=true` |
| 序列号 SN、MAC | 部分掩码 | `include_sensitive=true` |
| 日志路径、账号 | 保留 | — |

`include_sensitive=true` 的调用会写审计日志。

---

## 五、用户怎么接

服务起来后，用户在自己的 Agent 客户端里配一段即可。以支持远程 MCP 的客户端为例：

```json
{
  "mcpServers": {
    "testtask": {
      "url": "http://192.2.100.30:8888/mcp",
      "headers": { "Authorization": "Bearer <你的登录 Token>" }
    }
  }
}
```

配好之后就能直接用自然语言问：

- 「我有哪些待办测试任务」
- 「任务 48 现在卡在哪个环节，谁负责」
- 「这个任务的版本配套和实测环境对得上吗」
- 「把失败的用例列出来，按症状归个类」
- 「帮我给 TST-202609-007 建个问题单，标题是…」

不需要我写任何交互代码——**这些都在 Agent 客户端那边发生**。

---

## 六、诚实说风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| **用户门槛** | 配 MCP 对非开发者不友好，可能只有 20% 的人用得起来 | 写详细的配置文档和示例；给重度用户预配置好。**但这是最现实的风险** |
| **写操作失控** | Agent 客户端可能自动批准工具调用，用户没看清就执行了 | 写工具默认不注册；`dry_run` 默认为 true；关键操作强制校验权限 |
| **协议仍在演进** | 2025-11-25 是当前稳定版，2026-07-28 RC 又移除了 `Mcp-Session-Id` 会话机制 | 用官方 SDK，跟进成本很低 |
| **数据外流** | Agent 能把任务数据送到任何地方 | 默认脱敏；`include_sensitive` 审计 |
| **不等于「给全员做了 AI」** | MCP 服务的是"已经有 Agent 工作流的人" | 见下一节：MCP 和轻版应该并行 |

最后一条要讲清楚：如果目标是**让所有测试人员都用上 AI**，MCP 解决不了，最终还是要回到系统内做 UI。
但作为**第一步**，MCP 是最优解——成本极低、验证真实需求、不锁定架构。

---

## 七、建议：MCP 与「轻版」并行，而不是二选一

这两件事覆盖的是不同人群，而且**共用同一套底层逻辑**：

| | 轻版（系统内按钮） | MCP Server |
|---|---|---|
| 服务对象 | 全体测试人员 | 有 Agent 工作流的重度用户 |
| 覆盖场景 | 步骤3/4 版本导入 + 步骤5 版本比对 | 全流程任意查询与操作 |
| 工作量 | 3 人日 | 3.3 人日 |
| 门槛 | 点一下就用 | 需要配置客户端 |

**共用部分**：版本号归一化 `normalize.py`、字段 diff、版本表解析——
轻版写一次，MCP 的 `diff_version_matrix` 工具直接调同一个函数，不重复实现。

合计 **6.3 人日**，比原对话式方案的 8 人日还少，但覆盖面大得多：
全员能用上版本导入，重度用户能用上完整 AI 能力。

---

## 八、落地步骤

1. **第 1 天**：`fastmcp` 接入 + 8 个只读工具 + 挂载到 FastAPI `/mcp`
   → 用 MCP Inspector 自测通过
2. **第 2 天**：JWT 鉴权 + 权限对齐 + 脱敏分级 + 审计
3. **第 3 天**：4 个写工具（默认关闭）+ dry_run 机制
4. **同步进行**：轻版三个按钮，共用 `normalize.py` 与解析逻辑

第 1 天结束就能在 Agent 里问出「我有哪些待办任务」——**一天见到效果，这是这套方案最舒服的地方**。
