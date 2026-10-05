### 0. Goal & Overview

AIエージェントの普及に伴い、外部データソースや社内システムと連携するための **Model Context Protocol (MCP)** の利用が急拡大しています。しかし、実運用においては以下のような課題に直面します。

* **クライアント設定の肥大化:** 複数のMCPサーバー（例：GraphRAG、社内DB検索、ドキュメント参照など）を導入すると、LLMクライアント（Claude Desktopなど）側で個別の接続設定や管理が必要になり煩雑になる。
* **セキュリティとガバナンスの欠如:** バックエンドの各MCPサーバー個別に「プロンプトインジェクション対策（不正な指示のブロック）」や「出力の機密情報マスク」を実装するのは非効率であり、ポリシーの漏れや不統一を招く。

本リポジトリで提供する **`gateway-mcp`** は、こうしたエンタープライズ環境におけるMCPの運用課題を解決するため、「ツールのアグリゲーション（集約）」**と**「インライン・ガードレール（セキュリティ統制）」を軽量なPython/Starlette製アーキテクチャで実現することを目的としています。


### 1. Key Features

#### 1-1. 複数バックエンドの動的ツール集約 (Tool Aggregation)
* 起動時に複数のMCPサーバー（SSEベース）へ接続し、提供されるすべてのツールを自動で集約（Aggregation）。
* LLMクライアントからは「たった1つのMCPサーバー」に接続しているように見せかけつつ、裏側で適切なバックエンドへ自動ルーティングします。


#### 1-2. リアルタイム・ガードレール （入力・出力統制）
* **入力チェック:** 引数に含まれる文字列をスキャンし、禁止ワード（プロンプトインジェクションや不正な指示の兆候）が検知された場合は処理を即座にブロック。
* **出力チェック:** バックエンドからの応答テキストを監視し、機密キーワード（パスワードや秘匿情報）の露出を検知・マスク。


#### 1-3. クラウドネイティブな設定管理 (Environment-driven)
* Kubernetesの `ConfigMap` や環境変数（`BACKEND_SERVERS`, `GUARDRAIL_NG_WORDS` 等）から動的に設定を読み込めるため、コードを改修することなく柔軟にセキュリティポリシーやバックエンド構成を変更可能。

```
+-----------------------+
|      LLM Client       |
| (e.g., Claude Desktop)|
+-----------------------+
           |
           | SSE / JSON-RPC (Port 5001)
           v
+-----------------------------------------------------------------------+
|                         gateway-mcp (Gateway)                         |
|                                                                       |
|   +---------------------------------------------------------------+   |
|   |                     Starlette Web Server                      |   |
|   |        - GET /sse (Connection)                                |   |
|   |        - POST /messages/ (Message Handling)                   |   |
|   +---------------------------------------------------------------+   |
|                                   |                                   |
|                                   v                                   |
|   +---------------------------------------------------------------+   |
|   |                   Security Guardrail Layer                    |   |
|   |        - Input Check  : NG Word / Prompt Injection Block      |   |
|   |        - Output Check : Secret Word / Data Masking            |   |
|   +---------------------------------------------------------------+   |
|                                   |                                   |
|                                   v                                   |
|   +---------------------------------------------------------------+   |
|   |                   Tool Aggregator & Router                    |   |
|   |        - list_tools() : Aggregate all backend tools           |   |
|   |        - call_tool()  : Route execution to target backend     |   |
|   +---------------------------------------------------------------+   |
+-----------------------------------------------------------------------+
           |                                           |
           | SSE Client Session                        | SSE Client Session
           v                                           v
+-------------------------------+           +-------------------------------+
|     Backend MCP Server A      |           |     Backend MCP Server B      |
|      (e.g., GraphRAG)         |           |     (e.g., Internal Tools)    |
+-------------------------------+           +-------------------------------+
```

### 3. Setup

#### 3-1. Gateway機能を持つMCPサーバーのコンテナ化
```
sudo docker build --no-cache . -t developeronizuka/gateway-mcp:1.0.1
sudo docker push developeronizuka/gateway-mcp:1.0.1
```

#### 3-2. GraphRAGのKubernetes上への展開とサービス設定の変更

1. **リポジトリの参照**
[developer-onizuka/RAG](https://github.com/developer-onizuka/RAG) の手順を参照し、GraphRAGをKubernetes上に展開します。
2. **Kubernetesマニフェスト（`graphrag-mcp.yaml`）の調整**
リポジトリ内で提供されている [graphrag-mcp.yaml](https://github.com/developer-onizuka/RAG/blob/main/graphrag-mcp.yaml) の Service 定義において、`type: LoadBalancer` の行をコメントアウト（または削除）してください。
> **変更の理由**
> 今回構築する **MCP Gateway** をフロントに挟み、クラスタ内のプライベートネットワーク（`ClusterIP`）経由で安全にルーティング・接続することを目的としているためです。外部へ直接公開する必要がないため、デフォルトの `ClusterIP` として動作させます。

#### 修正後のYAML設定例

```yaml
apiVersion: v1
kind: Service
metadata:
  name: svc-graphrag-mcp
spec:
  # type: LoadBalancer  # 外部公開用ではなく内部ルーティング（ClusterIP）にするためコメントアウト
  selector:
    app: graphrag-mcp
  ports:
    - name: mcp-sse
      port: 5001
      targetPort: 5001
    - name: web-ui
      port: 8080
      targetPort: 8080

```

この構成により、GraphRAGはクラスタ内の `svc-graphrag-mcp`（ClusterIP）として安全に待機し、先ほど作成した `gateway-mcp` がフロントエンドとしてそのトラフィックとセキュリティ（ガードレール）を一元管理できるようになります。



<img src="https://github.com/developer-onizuka/MCP-Gateway/blob/main/Guardrail1.png" width="720"><br>

<img src="https://github.com/developer-onizuka/MCP-Gateway/blob/main/Guardrail2.png" width="720"><br>

<img src="https://github.com/developer-onizuka/MCP-Gateway/blob/main/Guardrail3.png" width="720"><br>

