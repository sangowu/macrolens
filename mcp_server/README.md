# MacroLens MCP Server

把 MacroLens 的金融研究能力暴露为 [MCP](https://modelcontextprotocol.io) tools，供 Claude Desktop 等 MCP 客户端直接调用。复用现有检索管道（`agent.per_loop` / `agent.executor`），不重复实现逻辑。

## Tools

| Tool | 用途 | 关键参数 |
|------|------|---------|
| `macrolens_ask` | 完整 PER Loop 检索增强问答（带 `[n]` 引用） | `question`, `max_iter` |
| `macrolens_search_sec` | MAG7 SEC filings 混合检索（语义+全文+reranker），返回原文片段 | `query`, `company?`, `fiscal_year?`, `top_k` |
| `macrolens_get_macro` | 美国宏观指标（FRED）时间序列 | `series_id?`, `query?`, `date_from?`, `date_to?`, `limit` |

全部为**只读**（`readOnlyHint: true`）。

## 前置：启动依赖服务

MCP server 连接本地服务，使用前必须先起：

```bash
# 1. PostgreSQL（pgvector）:5433
docker start macrolens-pg          # 或首次用 docker run（见项目 README）

# 2. 本地 embedding server :8081（必需）
llama-server -m Qwen3-Embedding-0.6B-f16.gguf --embedding --pooling last -ngl 99 --port 8081

# 3. reranker :6006（可选，未起时自动 fallback 到 RRF 排序）
docker run --gpus all -p 6006:8000 macrolens-model-server
```

依赖未就绪时，各 tool 会在数秒内返回**可操作的错误提示**（而非挂起），例如提示先 `docker start macrolens-pg`。

`.env` 需配 `GEMINI_API_KEY`（问答用）；配了 `LANGFUSE_*` 则调用自动进入 Langfuse 追踪。

## 接入 Claude Desktop

编辑 `claude_desktop_config.json`（macOS: `~/Library/Application Support/Claude/`；Windows: `%APPDATA%\Claude\`）：

```json
{
  "mcpServers": {
    "macrolens": {
      "command": "uv",
      "args": ["run", "--directory", "D:\\Python_Projects\\MarcoLens", "mcp_server/server.py"]
    }
  }
}
```

`--directory` 指向项目根，确保 `uv` 用对虚拟环境并能定位 `config.yaml` / `.env`。重启 Claude Desktop 后，工具栏应出现 3 个 `macrolens_*` 工具。

## 验证

在 Claude Desktop 中提问，例如：
- “用 macrolens 查 Google 2023 年的主要 AI 风险因素” → 触发 `macrolens_search_sec` / `macrolens_ask`
- “2022 年美国联邦基金利率走势” → 触发 `macrolens_get_macro`

也可用 MCP Inspector 本地调试：

```bash
npx @modelcontextprotocol/inspector uv run --directory . mcp_server/server.py
```

## 设计说明

- **stdio transport**：本地子进程，零网络暴露、零云成本。
- **惰性单例**：`cfg` / `embedder` / `llm` / `reranker` 首次调用初始化一次，跨 tool 复用；DB 连接每次 tool 单独建并带 `connect_timeout`。
- **同步栈 + `asyncio.to_thread`**：检索管道是同步的（psycopg / Gemini SDK），tool 用线程池执行以不阻塞事件循环。
- **扁平参数 schema**：用 `Annotated[type, Field(...)]` 而非嵌套 Pydantic model，客户端可直接传 `{"question": ...}`。
