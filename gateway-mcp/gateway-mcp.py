import asyncio
import os
import json
import logging
from contextlib import asynccontextmanager, AsyncExitStack

from mcp.server import Server
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server.sse import SseServerTransport
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession

from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
import uvicorn

# ========================================================
# ログ設定
# ========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("gateway-mcp")

# ========================================================
# 環境設定
# ========================================================
PORT = int(os.getenv("PORT", 5001))

BACKEND_SERVERS_JSON = os.getenv("BACKEND_SERVERS", "{}")
try:
    backend_configs: dict[str, str] = json.loads(BACKEND_SERVERS_JSON)
except json.JSONDecodeError:
    logger.error(f"❌ BACKEND_SERVERS is not a valid JSON string. Value: {BACKEND_SERVERS_JSON}")
    backend_configs = {}

if not backend_configs:
    logger.warning("⚠️ No backend servers configured. Gateway will start without routing any tools.")

# 🔻 ConfigMap / 環境変数から Input 用 NG ワードをロード
NG_WORDS_ENV = os.getenv("GUARDRAIL_NG_WORDS", "ignore previous instructions,これまでの指示を無視")
NG_WORDS = [word.strip() for word in NG_WORDS_ENV.split(",") if word.strip()]
logger.info(f"🛡️ Loaded Input NG Words: {NG_WORDS}")

# 🔻 ConfigMap / 環境変数から Output 用機密ワードをロード
SECRET_WORDS_ENV = os.getenv("GUARDRAIL_SECRET_WORDS", "SECRET_PASSWORD")
SECRET_WORDS = [word.strip() for word in SECRET_WORDS_ENV.split(",") if word.strip()]
logger.info(f"🛡️ Loaded Output Secret Words: {SECRET_WORDS}")

# ========================================================
# アプリケーション状態 (ルーティング管理)
# ========================================================
active_sessions: dict[str, ClientSession] = {}
tool_router: dict[str, ClientSession] = {}

# ========================================================
# ガードレール定義（ConfigMapからロードしたリストを使用）
# ========================================================
async def check_input_guardrail(tool_name: str, arguments: dict) -> tuple[bool, str]:
    logger.info(f"🔍 [Guardrail Input Check] Tool: {tool_name}, Args: {arguments}")
    
    for key, val in arguments.items():
        if isinstance(val, str):
            text_lower = val.lower()
            for ng in NG_WORDS:
                if ng.lower() in text_lower:
                    logger.warning(f"🚨 [Guardrail Blocked] NG word '{ng}' detected in argument '{key}' for tool {tool_name}")
                    return False, f"禁止ワード '{ng}' が検知されたため、入力をブロックしました。"
                    
    return True, ""

async def check_output_guardrail(tool_name: str, result_text: str) -> tuple[bool, str]:
    logger.info(f"🔍 [Guardrail Output Check] Tool: {tool_name}")
    
    for secret in SECRET_WORDS:
        if secret in result_text:
            logger.warning(f"🚨 [Guardrail Blocked] Secret word '{secret}' detected in output for tool {tool_name}")
            return False, f"機密情報（'{secret}'）が含まれていたためマスクしました。"
            
    return True, ""

# ========================================================
# MCP ツールハンドラ (Aggregation & Proxy)
# ========================================================
async def handle_list_tools(*args, **kwargs) -> types.ListToolsResult:
    """全バックエンドにツール一覧を問い合わせ、集約して返す"""
    logger.info("📋 [handle_list_tools] Executing tools aggregation...")
    aggregated_tools = []
    tool_router.clear()
    
    for server_name, session in active_sessions.items():
        try:
            result = await session.list_tools()
            for tool in result.tools:
                aggregated_tools.append(tool)
                tool_router[tool.name] = session
                logger.info(f"   -> Mapped tool '{tool.name}' to backend '{server_name}'")
        except Exception as e:
            logger.error(f"⚠️ Failed to fetch tools from {server_name}: {e}")
            
    logger.info(f"✅ [handle_list_tools] Total aggregated tools: {len(aggregated_tools)}")
    return types.ListToolsResult(tools=aggregated_tools)

async def handle_call_tool(*args, **kwargs) -> types.CallToolResult:
    """呼ばれたツールを担当する適切なバックエンドへ転送する（完全な引数抽出対応）"""
    name = None
    arguments = {}

    for arg in args:
        if hasattr(arg, "params"):
            params = arg.params
            if hasattr(params, "name") and params.name:
                name = params.name
            if hasattr(params, "arguments") and params.arguments is not None:
                arguments = params.arguments
        
        if isinstance(arg, dict):
            if "name" in arg and not name:
                name = arg["name"]
            if "arguments" in arg and arg["arguments"]:
                arguments = arg["arguments"]
            if "params" in arg and isinstance(arg["params"], dict):
                p = arg["params"]
                if "name" in p and not name:
                    name = p["name"]
                if "arguments" in p and p["arguments"]:
                    arguments = p["arguments"]

        if hasattr(arg, "name") and not name:
            name = getattr(arg, "name")
        if hasattr(arg, "arguments") and not arguments:
            arg_val = getattr(arg, "arguments")
            if arg_val:
                arguments = arg_val

    if not name and "name" in kwargs:
        name = kwargs["name"]
    if not arguments and "arguments" in kwargs and kwargs["arguments"]:
        arguments = kwargs["arguments"]

    if not name and len(args) > 0 and isinstance(args[0], str):
        name = args[0]
    if not arguments and len(args) > 1 and isinstance(args[1], dict):
        arguments = args[1]

    logger.info(f"🚀 [handle_call_tool] Resolved tool: '{name}' with args: {arguments}")

    if not name or not isinstance(name, str):
        logger.error(f"❌ [handle_call_tool] Failed to resolve tool name. Raw args: {args}, kwargs: {kwargs}")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="【Gateway Error】 ツール名を取得できませんでした。")],
            isError=True
        )

    session = tool_router.get(name)
    if not session:
        logger.error(f"❌ [handle_call_tool] Tool '{name}' not found in router.")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"【Gateway Error】 ツール '{name}' を提供するバックエンドが見つかりません。")],
            isError=True
        )

    # 1. 入力チェック
    is_safe, reason = await check_input_guardrail(name, arguments)
    if not is_safe:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"【Security Guardrail Blocked】 入力が拒否されました: {reason}")],
            isError=True
        )

    # 2. 適切なバックエンドへ転送
    try:
        logger.info(f"📡 Forwarding call_tool '{name}' to target backend with args: {arguments}...")
        backend_response: types.CallToolResult = await session.call_tool(name, arguments)
        logger.info(f"📥 Received response from backend for tool '{name}'")
    except Exception as e:
        logger.error(f"❌ Backend communication error during tool execution: {e}")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"【Gateway Error】 バックエンド通信エラー: {str(e)}")],
            isError=True
        )

    # 3. 出力チェック
    final_contents = []
    for content in backend_response.content:
        if content.type == "text":
            is_out_safe, out_reason = await check_output_guardrail(name, content.text)
            if not is_out_safe:
                final_contents.append(types.TextContent(type="text", text=f"【Security Guardrail Blocked】 出力拒否: {out_reason}"))
            else:
                final_contents.append(content)
        else:
            final_contents.append(content)

    # 4. バックエンドからの応答を安全に構築
    kwargs_result = {
        "content": final_contents,
        "isError": getattr(backend_response, "isError", False)
    }
    
    structured_content = None
    if hasattr(backend_response, "structuredContent") and backend_response.structuredContent is not None:
        sc = backend_response.structuredContent
        if isinstance(sc, dict) and "result" in sc:
            structured_content = sc
        elif isinstance(sc, dict):
            structured_content = {"result": sc}
        else:
            structured_content = {"result": sc}
    else:
        for content in final_contents:
            if content.type == "text":
                try:
                    parsed_json = json.loads(content.text)
                    if isinstance(parsed_json, dict) and "result" in parsed_json:
                        structured_content = parsed_json
                    else:
                        structured_content = {"result": parsed_json}
                    break
                except Exception:
                    pass
        
        if structured_content is None:
            text_vals = [c.text for c in final_contents if c.type == "text"]
            structured_content = {"result": text_vals[0] if text_vals else ""}

    kwargs_result["structuredContent"] = structured_content
    return types.CallToolResult(**kwargs_result)

# ========================================================
# 低レベル Server インスタンスの生成
# ========================================================
app_mcp = Server(
    "gateway-mcp",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool
)

# ========================================================
# Starlette Webサーバー設定 (SSE公開用)
# ========================================================
sse = SseServerTransport("/messages/")

async def endpoint_sse(request: Request):
    logger.info("🔌 New SSE connection initialized")
    async with sse.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
        options = InitializationOptions(
            server_name="gateway-mcp",
            server_version="1.3.0",
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(listChanged=False)
            )
        )
        await app_mcp.run(read_stream, write_stream, options)

@asynccontextmanager
async def lifespan(app: Starlette):
    """起動時に設定された全てのバックエンドMCPサーバーへ接続を保持する"""
    async with AsyncExitStack() as stack:
        for name, url in backend_configs.items():
            try:
                ctx_sse = sse_client(url)
                read_stream, write_stream = await stack.enter_async_context(ctx_sse)
                
                ctx_session = ClientSession(read_stream, write_stream)
                session = await stack.enter_async_context(ctx_session)
                
                await session.initialize()
                active_sessions[name] = session
                
                result = await session.list_tools()
                tool_names = [t.name for t in result.tools]
                logger.info(f"✅ Connected to backend '{name}' at {url} (Tools: {tool_names})")
                
            except Exception as e:
                logger.error(f"❌ Failed to connect to backend '{name}' at {url}: {e}")
        
        yield
        active_sessions.clear()

app = Starlette(
    routes=[
        Route("/sse", endpoint=endpoint_sse, methods=["GET"]),
        Mount("/messages/", app=sse.handle_post_message),
    ],
    lifespan=lifespan,
)

if __name__ == "__main__":
    logger.info(f"🚀 Starting Gateway on port {PORT}...")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
