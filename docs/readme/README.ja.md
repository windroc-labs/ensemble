# Ensemble

[English](../../README.md) · [Español](README.es.md) · [Français](README.fr.md) · [한국어](README.ko.md) · [Português](README.pt.md) · [简体中文](README.zh-CN.md)

Ensemble を使うと、コーディングエージェントをチームとして扱えます。コマンダーと会話する、ロールにタスクを委任する、
ボードにロール間の作業振り分けを任せる、といった使い方ができます。Claude Code、Codex、OpenCode は共通のセッション操作を使います。

<a id="what"></a>
## 実行できるもの

![3 つの入り口：単独のワーカー、ロールとタスク、ボードのワークフロー](../images/levels.svg)

必要な範囲を選んでください。ワーカーはクライアントとの会話です。ロールはそこに役割の規定、タスク、結果を加えます。
ボードはロールの実行をスケジュールし、その結果を issue に書き戻します。ボードには Linear またはローカルの Markdown を使えます。

<a id="why"></a>
## チームを使う理由

![個別のワーカーを直接調整する場合と、1 人のコマンダーがチームを制御する場合の比較](../images/compare.svg)

エージェントを個別に実行すると、誰かが作業を振り分け、返答を追う必要があります。コマンダーは Crew を使い、
1 つの会話からこれらの実行を開始、確認、再開します。どの作業を許可し、レビューするかは引き続きあなたが決めます。

<a id="how-it-works"></a>
## 仕組み

![レイヤーの境界：Manager のコマンド、Orchestrator、Delegate、Session、共通の Utils](../images/layers.svg)

![実行時の境界：セッションハーネス、ローカルと Docker のガード、broker の認証情報、外向き通信のプロキシ](../images/system.svg)

制御、認証情報の配布、ネットワーク経路はそれぞれ別のものです。[アーキテクチャ概要](../architecture.md)では責任の分担と信頼関係を説明しています。
実装の詳細は、リンク先のソースコードを参照してください。

<a id="install"></a>
## インストール

Apple silicon Mac で、[windroc マーケットプレイス](https://github.com/windroc-labs/agent-plugins-marketplace/blob/main/.claude-plugin/marketplace.json)に
固定されているリリースのインストーラーを実行します。
`<tag>` を、そのファイルにある ensemble の `source.ref` に置き換えてください。

```sh
curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install.sh | bash
```

インストーラーでは、使用するクライアント、ワーカーをローカルと Docker のどちらで実行するか、ボードを設定するかを尋ねます。
選択した依存関係とプラグインをインストールし、必要なログインを求め、`ctmux` を設定します。
インストールが中断した場合は、再実行すれば完了できます。オプションを確認するには、リポジトリのチェックアウトで `bash scripts/install.sh --help` を使ってください。
既存のインストールがある場合は、先に[アップグレード](../upgrading.md)を読んでください。

選択したクライアントに必要なモデルのアカウントまたはプロバイダーを使ってください。OpenCode のプロバイダーとネットワークの設定は、
[ロールとタスクのカスタマイズ](../customizing-roles-and-tasks.md#opencode-sessions)にあります。
ホストの `gh` には、対象リポジトリへの push と PR 操作の権限が必要です。classic token には `repo`、`read:org`、`gist` が必要です。
fine-grained token には、チェックのために Actions の読み取り権限も必要です。

### GitHub App

Docker のワーカーは、ホストの `gh` のログインではなく GitHub App を使います。インストーラーは App を作成しません。
GitHub Settings › Developer settings › GitHub Apps で、自分のアカウントにだけインストールできる非公開 App を作成し、
webhook を無効にしてください。リポジトリの権限は、[Session の設定](../../src/ensemble/session/config.toml)にある
`github.permissions` を満たす必要があります。ワーカーに push を許可するリポジトリだけにインストールしてください。
これにはドキュメントの出力先リポジトリも含まれます。

秘密鍵を生成し、App ID を控えて、次のコマンドで取り込みます。

```sh
/usr/local/libexec/ensemble/ensemble-broker login github ensemble-broker-bot \
  --key-file KEY.pem --app-id APP_ID
```

取り込み後は PEM を削除してください。交換と失効については、[認証情報のローテーション](../upgrading.md#credential-rotation)を参照してください。

<a id="quick-start"></a>
## クイックスタート

1 つのタスクを実行するには、インストールしたクライアントの会話で次を入力します。

```text
/ensemble:act-as dummy-tester:echo hello
```

チームを使うには、コマンダーのシェルを開き、クライアントを起動します。

```sh
ctmux work
claude
```

使用するクライアントが `codex` または `opencode` の場合は、`claude` を置き換えてください。その会話で次を呼び出します。

```text
/ensemble:manage
```

作業内容と希望する成果物を伝えてください。コマンダーは Crew を通じて、ワーカーとロールの実行を調整します。
ボードで作業する場合、`/ensemble:board` で issue を操作します。ステージの選び方は、
[ワークフローのカスタマイズ](../customizing-roles-and-tasks.md#on-the-board)で説明しています。

<a id="docs"></a>
## ドキュメント

| 知りたいこと | 参照先 |
|---|---|
| システムのレイヤー、実行と信頼の境界 | [アーキテクチャ](../architecture.md) |
| モデル、プロバイダー、ロールの設定、独自のタスクやワークフロー | [ロールとタスクのカスタマイズ](../customizing-roles-and-tasks.md) |
| 既存のインストールに必要な変更 | [アップグレード](../upgrading.md) |
| コントリビューター向けの規約とチェックの実行方法 | [AGENTS.md](../../AGENTS.md) |
