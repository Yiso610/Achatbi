# ChatBI 架构说明

## 1. Agent 工作流的整体职责

Agent 部分由三个文件共同组成：

| 文件 | 职责 |
|---|---|
| `agents/state.py` | 定义节点之间传递的数据，以及这些数据的类型、默认值和校验规则 |
| `agents/nodes.py` | 定义每个节点具体执行什么工作 |
| `agents/graph.py` | 把状态模型、节点函数和节点之间的连接组合起来，形成并运行一个 LangGraph 工作流图 |

这三个文件的关系可以概括为：

```text
AgentState 提供共享状态
       ↓
各个 Node 读取状态并返回状态更新
       ↓
Graph 根据线性边和条件边决定下一步
       ↓
工作流持续运行，直到 Format Response 后结束
```

这里需要区分“状态”和“流程”：`state.py` 不负责决定流程，`nodes.py` 不负责定义节点之间的跳转，`graph.py` 才负责把两者编排起来。

## 2. State：节点之间传递的数据

`agents/state.py` 使用 Pydantic 数据模型定义 `AgentState`。一次用户提问从工作流入口进入后，所有节点都围绕同一个状态对象协作：节点从状态中读取自己需要的信息，并返回需要写回的字段。

`AgentState` 通过模型配置支持额外字段、赋值时校验、枚举值处理和字段名填充；其中 `question` 和 `database_path` 是初始化工作流时必须提供的输入字段，其余字段都有默认值。

### 2.1 状态字段

| 分组 | 字段 | 类型与默认值 | 含义 |
|---|---|---|---|
| 输入 | `question` | `str` | 用户输入的自然语言问题 |
| 输入 | `database_path` | `str` | 当前使用的数据库路径 |
| 路由 | `is_relevant` | `bool = False` | 问题是否与当前数据库相关 |
| SQL 生成 | `sql_query` | `str = ""` | 大模型生成的 SQL 查询语句 |
| SQL 生成 | `reasoning` | `str = ""` | SQL 生成时返回的思路说明 |
| SQL 校验 | `validation_passed` | `bool = False` | SQL 是否通过安全、表名、语法等检查 |
| SQL 校验 | `validation_error` | `str = ""` | SQL 校验失败时的错误信息 |
| 执行 | `query_result` | `List[Dict[str, Any]] = []` | SQL 执行后的查询结果 |
| 执行 | `error` | `str = ""` | SQL 执行过程中的错误信息 |
| 反思与重试 | `retry_count` | `int = 0` | SQL 反思和重试次数 |
| 可视化 | `visualization_spec` | `Optional[Dict[str, Any]] = None` | 图表类型、横轴、纵轴等可视化配置 |
| 可视化 | `analysis_summary` | `str = ""` | 基于实际查询结果生成的分析总结 |
| 最终输出 | `final_response` | `str = ""` | 最终展示给用户的格式化回答 |

### 2.2 状态中的错误与完成判断

`AgentState` 还提供了几个辅助方法：

- `has_error()`：判断 `error` 或 `validation_error` 是否有内容。
- `is_complete()`：判断 `final_response` 是否已经生成。
- `get_error_message()`：优先返回校验错误，否则返回执行错误。
- `reset_errors()`：在重试前清空错误字段。

状态模型还会校验 `retry_count` 不能为负数，并确认 `query_result` 必须是列表。这样可以把节点之间的共享数据约束在一个明确的数据结构中。

## 3. Nodes：每个节点具体做什么

`agents/nodes.py` 定义工作流中的节点函数。节点本身主要负责完成一个相对独立的步骤：读取当前状态、调用数据库管理器或大模型、然后返回状态更新字典。

节点不会直接决定下一个节点。节点完成后，LangGraph 会根据 `graph.py` 中配置的普通边或条件边继续执行。

### 3.1 数据库管理器获取

`get_database_manager()` 根据当前状态中的 `database_path` 创建 `DatabaseManager`。所有需要读取语义 Schema 或执行 SQL 的节点，都通过这个入口获得当前请求对应的数据库管理器。

### 3.2 七个工作节点

#### 1. `intent_router_node`

读取用户问题和数据库结构，调用路由模型判断问题是否可以由当前数据库回答，并返回 `is_relevant`。

- 相关：`is_relevant = True`，继续生成 SQL。
- 不相关：`is_relevant = False`，同时写入提示用户重新提问的 `final_response`。
- 路由模型调用发生异常时，当前实现默认把问题交给后续流程处理。

#### 2. `sql_generator_node`

读取用户问题和带有语义信息的数据库 Schema，调用 SQL 生成模型生成 SQLite SQL，并提取两部分结果：

- `sql_query`：生成的 SQL 查询。
- `reasoning`：SQL 生成前的查询思路说明。

生成失败时，节点会把错误写入 `validation_error`，让后续反思流程处理。

#### 3. `sql_validator_node`

读取当前 SQL，执行查询前的多重校验，并返回校验状态。主要检查包括：

- 禁止危险或写操作关键字，确保只进行只读查询；
- 检查 SQL 引用的表名是否存在；
- 使用数据库管理器进行 SQL 语法检查；
- 将查询结果行数限制在配置允许的最大值以内。

校验通过时返回 `validation_passed = True`；失败时返回 `validation_passed = False` 和 `validation_error`。

#### 4. `executor_node`

只读取已经通过校验的 SQL，并交给 `DatabaseManager` 执行。

- 执行成功：返回 `query_result`，并清空执行错误。
- 执行失败：返回空结果和 `error`，由工作流转入反思节点。

#### 5. `reflector_node`

读取当前问题、当前 SQL、数据库 Schema 以及校验错误或执行错误，调用反思模型修正 SQL，并增加 `retry_count`。

- 未超过最大重试次数：返回修正后的 `sql_query`，清理错误并回到 SQL 校验节点。
- 超过最大重试次数：写入 `final_response`，结束本次工作流。
- 反思模型调用失败：写入失败提示，结束本次工作流。

因此，`reflector_node` 不是只处理 SQL 校验错误；SQL 校验失败和 SQL 执行失败都会进入这个节点。

#### 6. `visualizer_node`

只在 SQL 执行成功后运行。它读取实际的 `query_result`、用户问题和 SQL，调用可视化模型生成：

- `visualization_spec`：图表类型、横轴、纵轴等配置；
- `analysis_summary`：根据实际查询结果生成的中文分析总结。

如果没有查询结果，节点会直接返回“没有找到数据”的响应。如果可视化模型返回内容无法解析，则使用默认柱状图配置作为兜底。

#### 7. `format_response_node`

读取当前最终状态，把工作流结果整理成面向用户的 `final_response`。

- 有错误：根据错误类型生成失败说明和调整建议；
- 查询成功但没有数据：生成空结果提示；
- 查询成功：生成成功提示和结果数量说明。

这个节点是所有结束分支的统一出口，完成后工作流进入 `END`。

## 4. Graph：节点如何连接和跳转

`agents/graph.py` 负责把 `AgentState`、各个节点函数和节点之间的连接组合起来，形成一个 LangGraph 工作流图。

严格来说，不是“连线自己读取状态”，而是“条件判断函数读取当前状态并决定走哪条连线”：

1. 条件判断函数读取当前 `AgentState`；
2. 条件判断函数返回一个路由结果，例如 `"generate_sql"`；
3. LangGraph 根据预先配置的条件边，把这个路由结果映射到对应节点。

### 4.1 四个条件判断函数

#### `should_continue_after_routing(state)`

读取 `is_relevant`：

- `True` → 返回 `"generate_sql"`；
- `False` → 返回 `"end"`，实际跳转到 `format_response`。

#### `should_continue_after_validation(state)`

读取 `validation_passed`：

- `True` → 返回 `"execute_query"`；
- `False` → 返回 `"reflect"`。

#### `should_continue_after_execution(state)`

读取 `error`：

- 有错误 → 返回 `"reflect"`；
- 没有错误 → 返回 `"visualize"`。

#### `should_continue_after_reflection(state)`

读取 `final_response`：

- 已经有最终响应 → 返回 `"end"`，跳转到 `format_response`；
- 没有最终响应 → 返回 `"validate_sql"`，重新校验修正后的 SQL。

### 4.2 工作流结构

```text
Intent Router
    ├─ 不相关 → Format Response → END
    └─ 相关 → SQL Generator
                    ↓
              SQL Validator
                ├─ 失败 → Reflector
                │           ↓
                │       回到 Validator
                └─ 通过 → Executor
                            ├─ 执行失败 → Reflector
                            └─ 执行成功 → Visualizer
                                            ↓
                                      Format Response
                                            ↓
                                           END
```

因此，整体流程不是始终完全直线，而是带有条件分支和重试回路的工作流：

- Intent Router 判断不相关时，直接跳过 SQL 生成、校验、执行和可视化；
- SQL 校验失败会进入 Reflector；
- SQL 执行失败也会进入 Reflector；
- Reflector 修正后可能重新回到 `validate_sql`；
- 达到最大重试次数后才结束；
- 只有 SQL 执行成功后才进入 Visualizer。

### 4.3 `build_graph()` 的构建步骤

`build_graph()` 完成工作流图的组装和编译，主要步骤是：

1. 创建 `StateGraph(AgentState)` 工作流对象；
2. 注册所有节点：路由、SQL 生成、校验、执行、反思、可视化和响应格式化；
3. 设置入口节点 `route_intent`；
4. 添加节点之间的线性边；
5. 添加由条件判断函数驱动的条件边；
6. 把 `format_response` 连接到 `END`；
7. 编译整个工作流图并返回。

其中，线性边用于表示确定的下一步，例如 `generate_sql → validate_sql`；条件边用于表示需要读取当前状态后再做决定的分支。

### 4.4 工作流的创建、缓存和运行

`graph.py` 中 `build_graph()` 后面的函数分别承担运行时管理职责：

- `get_compiled_graph()`：获取已经编译的工作流图；如果尚未创建，则创建并缓存一份。异步锁用于避免并发初始化出多份图。
- `clear_graph_cache()`：清除工作流图缓存，使下一次调用重新建图。
- `run_agent()`：以同步方式对外运行工作流，内部调用异步版本。
- `arun_agent()`：创建初始 `AgentState`，解析数据库路径，异步调用编译后的工作流，并把最终结果重新转换为 `AgentState`。
- `get_graph_visualization()`：导出工作流图的 Mermaid 文本，用于可视化或调试。

## 5. 三个文件如何协作

一次请求可以按下面的方式理解：

```text
arun_agent()
  │
  ├─ 创建 AgentState(question, database_path)
  │
  └─ 调用编译后的 Graph
       │
       ├─ Graph 调用 intent_router_node
       │    └─ Node 读取 State，返回 is_relevant
       │
       ├─ 条件函数读取 State，返回路由结果
       │    └─ Graph 根据条件边跳转
       │
       ├─ 后续 Node 继续读取并更新 State
       │
       └─ format_response_node 写入 final_response
            └─ Graph 进入 END
```

可以用一句话区分三者：

> `state.py` 定义“传什么数据”，`nodes.py` 定义“每一步做什么”，`graph.py` 定义“这些步骤如何连接、何时分支以及如何重试”。
