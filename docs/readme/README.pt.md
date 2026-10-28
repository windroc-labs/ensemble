# Ensemble

[English](../../README.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [简体中文](README.zh-CN.md)

O Ensemble permite trabalhar com agentes de programação como uma equipe. Converse com um comandante, delegue uma tarefa a um papel
ou deixe um quadro distribuir o trabalho entre os papéis. Claude Code, Codex e OpenCode compartilham os mesmos controles de sessão.

<a id="what"></a>
## O que você pode executar

![Três pontos de entrada: um trabalhador sem papel, um papel com uma tarefa ou um fluxo de trabalho com quadro](../images/levels.svg)

Escolha o escopo de que precisa. Um trabalhador é uma conversa com um cliente; um papel acrescenta uma definição de responsabilidades, uma tarefa e um resultado verificado;
um quadro agenda execuções de papéis e registra seus resultados nas issues. O quadro pode ser Linear ou Markdown local.

<a id="why"></a>
## Por que uma equipe

![Coordenação direta de trabalhadores separados em comparação com um comandante que controla uma equipe](../images/compare.svg)

Execuções separadas de agentes precisam de alguém para distribuir o trabalho e acompanhar as respostas. Um comandante usa Crew para iniciar,
inspecionar e retomar essas execuções a partir de uma única conversa. Você continua decidindo qual trabalho autorizar e revisar.

<a id="how-it-works"></a>
## Como funciona

![Limites entre camadas: comandos de Manager, Orchestrator, Delegate, Session e Utils compartilhados](../images/layers.svg)

![Limites de execução: harness de sessão, guardas locais e do Docker, credenciais do broker e proxy de saída](../images/system.svg)

O controle, a entrega de credenciais e os caminhos de rede são distintos. A [visão geral da arquitetura](../architecture.md)
explica as responsabilidades e a confiança; os detalhes de implementação estão no código-fonte vinculado.

<a id="install"></a>
## Instalação

Em um Mac com Apple silicon, execute o instalador da versão fixada pelo
[marketplace windroc](https://github.com/windroc-labs/agent-plugins-marketplace/blob/main/.claude-plugin/marketplace.json).
Substitua `<tag>` pelo `source.ref` do ensemble nesse arquivo:

```sh
curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install.sh | bash
```

O instalador pergunta quais clientes usar, se os trabalhadores serão executados localmente ou no Docker e se você quer configurar um
quadro. Ele instala as dependências e os plugins selecionados, solicita os logins necessários e configura o `ctmux`.
Execute-o novamente para concluir uma instalação interrompida. Para ver as opções, use `bash scripts/install.sh --help` em um checkout do repositório;
para uma instalação existente, leia [Atualização](../upgrading.md) primeiro.

Use a conta de modelos ou o provedor exigido pelo cliente escolhido. A configuração de provedor e rede do OpenCode está
em [Personalização de papéis e tarefas](../customizing-roles-and-tasks.md#opencode-sessions).
O `gh` do host precisa de acesso para push e PR nos repositórios de destino: tokens clássicos precisam de `repo`, `read:org` e `gist`;
tokens com permissões granulares também precisam de acesso de leitura a Actions para as verificações.

### GitHub App

Os trabalhadores do Docker usam um GitHub App em vez do login do `gh` do host. O instalador não cria o App.
Em GitHub Settings › Developer settings › GitHub Apps, crie um App privado que só possa ser instalado na sua conta,
com os webhooks desativados. As permissões de repositório devem abranger `github.permissions` na
[configuração de Session](../../src/ensemble/session/config.toml). Instale-o apenas nos repositórios em que os trabalhadores possam fazer push,
incluindo os repositórios de saída de documentos.

Gere uma chave privada, anote o App ID e importe-os:

```sh
/usr/local/libexec/ensemble/ensemble-broker login github ensemble-broker-bot \
  --key-file KEY.pem --app-id APP_ID
```

Exclua o PEM após a importação. A [rotação de credenciais](../upgrading.md#credential-rotation) explica a substituição e a revogação.

<a id="quick-start"></a>
## Início rápido

Para uma única tarefa, na conversa do cliente instalado:

```text
/ensemble:act-as dummy-tester:echo hello
```

Para uma equipe, abra um shell de comandante e inicie seu cliente:

```sh
ctmux work
claude
```

Use `codex` ou `opencode` em vez de `claude` se esse for seu cliente. Nessa conversa, invoque:

```text
/ensemble:manage
```

Descreva o trabalho e a entrega desejada. O comandante coordena trabalhadores e execuções de papéis por meio de Crew.
Para trabalhar com um quadro, `/ensemble:board` opera sobre as issues; a [personalização do fluxo de trabalho](../customizing-roles-and-tasks.md#on-the-board)
explica como escolher as etapas.

<a id="docs"></a>
## Documentação

| Leia quando precisar de… | Onde |
|---|---|
| Camadas do sistema e limites de execução e confiança | [Arquitetura](../architecture.md) |
| Modelos, provedores, configurações de papéis, suas próprias tarefas ou fluxo de trabalho | [Personalização de papéis e tarefas](../customizing-roles-and-tasks.md) |
| Mudanças necessárias em uma instalação existente | [Atualização](../upgrading.md) |
| Convenções para colaboradores e pontos de entrada para as verificações | [AGENTS.md](../../AGENTS.md) |
