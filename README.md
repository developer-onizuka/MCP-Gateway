# 0. Goal & Overview

AIエージェントの普及に伴い、外部データソースや社内システムと連携するための **Model Context Protocol (MCP)** の利用が急拡大しています。しかし、実運用においては以下のような課題に直面します。

* **クライアント設定の肥大化:** 複数のMCPサーバー（例：GraphRAG、社内DB検索、ドキュメント参照など）を導入すると、LLMクライアント（Claude Desktopなど）側で個別の接続設定や管理が必要になり煩雑になる。
* **セキュリティとガバナンスの欠如:** バックエンドの各MCPサーバー個別に「プロンプトインジェクション対策（不正な指示のブロック）」や「出力の機密情報マスク」を実装するのは非効率であり、ポリシーの漏れや不統一を招く。

本リポジトリで提供する **`gateway-mcp`** は、こうしたエンタープライズ環境におけるMCPの運用課題を解決するため、「ツールのアグリゲーション（集約）」**と**「インライン・ガードレール（セキュリティ統制）」を軽量なPython/Starlette製アーキテクチャで実現することを目的としています。


# 1. Key Features

### 1-1. 複数バックエンドの動的ツール集約 (Tool Aggregation)
* 起動時に複数のMCPサーバー（SSEベース）へ接続し、提供されるすべてのツールを自動で集約（Aggregation）。
* LLMクライアントからは「たった1つのMCPサーバー」に接続しているように見せかけつつ、裏側で適切なバックエンドへ自動ルーティングします。


### 1-2. リアルタイム・ガードレール （入力・出力統制）
* **入力チェック:** 引数に含まれる文字列をスキャンし、禁止ワード（プロンプトインジェクションや不正な指示の兆候）が検知された場合は処理を即座にブロック。
* **出力チェック:** バックエンドからの応答テキストを監視し、機密キーワード（パスワードや秘匿情報）の露出を検知・マスク。


### 1-3. クラウドネイティブな設定管理 (Environment-driven)
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

# 3. Setup

### 3-1. Gateway機能を持つMCPサーバーのコンテナ化 & Yamlファイルの展開
コンテナをビルドし、DockerhubにPushします。
```
sudo docker build --no-cache . -t developeronizuka/gateway-mcp:1.0.1
sudo docker push developeronizuka/gateway-mcp:1.0.1
```
Yamlファイルを展開します。
```
kubectl apply - f gateway-mcp.yaml
```
なお、gateway-mcp では、バックエンドのルーティング先やセキュリティガードレールのルール（NGワード・機密ワード）を Python コードから完全に分離し、Kubernetes の ConfigMap を通じて外部から動的に管理できるようにしています。これにより、コードや Docker イメージを再ビルドすることなく、運用環境で柔軟にポリシーや接続先を変更できます。

| キー (Key) | 役割・説明 |
| :--- | :--- |
| **`backends.json`** | ゲートウェイが接続・集約するバックエンドの MCP サーバーのルーティング定義です。<br><br>JSON 形式でサーバー名と SSE エンドポイントの URL（例: クラスタ内の ClusterIP サービス）を指定します。 |
| **`GUARDRAIL_NG_WORDS`** | LLM クライアントからの入力（ツール引数）を監視・ブロックするキーワード一覧です。<br><br>カンマ区切りで指定します。公序良俗に反する単語のほか、プロンプトインジェクションを防ぐための定型句を登録し、不正な指示がバックエンドへ到達するのを防ぎます。 |
| **`GUARDRAIL_SECRET_WORDS`** | バックエンド（GraphRAG など）からの出力テキストを監視・マスクする機密キーワード一覧です。<br><br>検索結果やデータベースの応答に社外秘データやパスワードなどの機密情報が混入していないかをスキャンし、情報漏洩を未然にブロックします。 |

### 3-2. GraphRAGのKubernetes上への展開とサービス設定の変更 & Yamlファイルの展開

1. **リポジトリの参照**
[developer-onizuka/RAG](https://github.com/developer-onizuka/RAG) の手順を参照し、GraphRAGをKubernetes上に展開します。
2. **Kubernetesマニフェスト（`graphrag-mcp.yaml`）の調整**
リポジトリ内で提供されている [graphrag-mcp.yaml](https://github.com/developer-onizuka/RAG/blob/main/graphrag-mcp.yaml) の Service 定義において、`type: LoadBalancer` の行をコメントアウト（または削除）してください。
> **変更の理由**
> 今回構築する **MCP Gateway** をフロントに挟み、クラスタ内のプライベートネットワーク（`ClusterIP`）経由で安全にルーティング・接続することを目的としているためです。外部へ直接公開する必要がないため、デフォルトの `ClusterIP` として動作させます。

### 修正後のYAML設定例

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
このYamlファイルを展開します。
```
kubectl apply - f graphrag-mcp.yaml
```

この構成により、GraphRAGはクラスタ内の `svc-graphrag-mcp`（ClusterIP）として安全に待機し、先ほど作成した `gateway-mcp` がフロントエンドとしてそのトラフィックとセキュリティ（ガードレール）を一元管理できるようになります。

# 4. Execution
今回は、MCP Gatewayを挟んで、GraphRAGによるナレッジグラフの登録になります。その際、NGワードに該当する文章を送信・登録しようとした場合でも、MCP Gatewayの入力ガードレール機能によって自動的に検知・ブロックされ、バックエンドのGraphRAGへ不正なデータや不適切な指示が到達するのを防ぐ挙動を確認します。

<img src="https://github.com/developer-onizuka/MCP-Gateway/blob/main/Guardrail1.png" width="720"><br>

<img src="https://github.com/developer-onizuka/MCP-Gateway/blob/main/Guardrail2.png" width="720"><br>

<img src="https://github.com/developer-onizuka/MCP-Gateway/blob/main/Guardrail3.png" width="720"><br>

# 付録1. AWS AgentCoreにおけるGuradrail機能の実装
ここまでの解説を踏まえ、AWS AgentCore環境におけるGatewayとGuardrailが連携したリクエスト制御の具体的な動作メカニズムを、補足資料として以下に整理します。
```
 [User / AI] 
       │ 
       │ (1) Tool Call Request
┌──────▼────────────────────────────────────────────────---─┐
│  [ AgentCore Gateway (Execution Infrastructure) ]         │
│      │                                                    │
│      │ (2) Send input data and ask: "Is this safe?"       │
│      │                                                    │
│   ┌──▼───────────────────────────────────────────────┐    │
│   │ [ AgentCore Guardrail (Rule Definition) ]        │    │
│   │   - Bedrock Guardrails (Content & PII Check)     │    │
│   │   - Cedar Policies (Authorization & Arg Check)   │    │
│   └──────────────────────────────────────────────────┘    │
│      │ (3) Judgment Result: "NG (Reason: Blocked Word)"   │
│      ▼                                                    │
│  [!! Block Communication Immediately !]                   │
│      │                                                    │
└──────│────────────────────────────────────────────────---─┘
       │
       ▼ (4) No communication reaches the backend (MCP Server);
             Gateway immediately returns a custom error to the user
```

このシーケンスは、ユーザーやAIエージェントからのツール呼び出しリクエストを **AgentCore Gateway（実行インフラ）** がインターセプト（横取り）し、**AgentCore Guardrail（ルール定義）** による多角的な安全・認可チェックを経て、危険な通信を即座に遮断する流れを表しています。

---
#### (1) Tool Call Request（ツール呼び出しリクエスト）
* ユーザーまたはAIエージェントが、外部ツール（MCPサーバーなど）を実行するためのリクエストを送信します。

#### (2) Send input data and ask: "Is this safe?"（Gatewayによる検査依頼）
* リクエストは直接バックエンドには向かわず、通信経路上にある **AgentCore Gateway** に捕捉されます。
* Gatewayは入力データ（プロンプトやツールの引数）を **AgentCore Guardrail** へ渡し、評価を仰ぎます。
* Guardrail側では、以下の2つのエンジンが連携して審査を行います。
* **Bedrock Guardrails**: 有害表現やプロンプトインジェクション、PII（個人情報）などのコンテンツ安全性をチェック。
* **Cedar Policies**: 誰がそのツールを実行してよいかの認可（Authorization）や、引数の値がルールに違反していないかをチェック。

#### (3) Judgment Result: "NG" & Block（判定と即座の遮断）
* チェックの結果、禁止ワードの検出やポリシー違反などにより「NG」と判定されると、その結果がGatewayへ返却されます。
* 判定を受けたGatewayは、**その場で通信を強制的に遮断**します。

#### (4) Backend Protection & Error Return（バックエンドの保護とエラー返却）
* 不正なリクエストはバックエンド（MCPサーバーなどの実体）に**到達しません**。
* Gatewayがユーザー側へカスタムエラーメッセージを即座に返し、システム全体の安全を守ります。


# 付録2. 単体機能としての Bedrock Guardrails
なお、以下がGuardrailを単体で使った時の実装例です。Gatewayがない環境で Bedrock Guardrails を使おうとすると、通常は次のようなプログラム（Pythonなど）を自前で書くことになります。
```
import boto3

client = boto3.client('bedrock-runtime')

# ユーザーからの入力を受け取る
user_input = "私のパスワードは secret123 です。"

# 【単体としてのGuardrailsを呼び出す】
response = client.apply_guardrail(
    guardrailIdentifier='your-guardrail-id',
    guardrailVersion='DRAFT',
    source='INPUT',
    text=[user_input]
)

# 判定結果を確認
action = response['action'] # 'NONE' (安全) または 'GUARDRAIL_INTERVENED' (ブロック)

if action == 'GUARDRAIL_INTERVENED':
    print("ガードレールに引っかかりました！処理を中断します。")
else:
    # 安全なのでLLMへ処理を続行
    pass
```
