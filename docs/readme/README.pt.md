<div align="center">

# ensemble

[English](../../README.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · **Português**

[![CI](https://github.com/windroc-labs/ensemble/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/windroc-labs/ensemble/actions/workflows/test.yml?query=branch%3Amain) [![Worker image](https://github.com/windroc-labs/ensemble/actions/workflows/worker-image.yml/badge.svg?branch=main)](https://github.com/windroc-labs/ensemble/actions/workflows/worker-image.yml?query=branch%3Amain) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../../LICENSE)

</div>

*Esta é uma tradução; a versão de referência é o README em inglês.*

Executa agentes de programação (Claude Code, Codex e OpenCode com os modelos Ollama do host) sem supervisão a partir de um quadro do Linear ou de um quadro local de arquivos Markdown de issues. Orchestrator lê e escreve issues por uma interface Tracker substituível; `[orchestrator] tracker` seleciona `local` (padrão) ou `linear`. Um arquivo workflow define as etapas e seus roles. Cada role trabalha em até `max_runs` issues ao mesmo tempo e devolve o resultado para você revisar. Depois que você aprova, o próximo role assume.

Desenvolvedores: [`AGENTS.md`](../../AGENTS.md).

```mermaid
flowchart LR
    R["researcher<br>deep-research, research, light-research"] -->|"report → Approve → promote"| P["pm<br>product-design"]
    P -->|"PRD → Approve → promote"| E["engineer<br>build, light-build"] -->|pull request| Y[you]
    T[ensemble-orchestrator router] -.->|claims ready issues| R & P & E
```

## Arquitetura em camadas

O ensemble é construído em camadas: cada uma faz um só trabalho e só chama a de baixo. As duas camadas de baixo, Session e Delegate, são blocos genéricos que você pode usar sozinhos ou como base da sua própria orquestração. No Orchestrator, altere o arquivo workflow para definir as etapas ou implemente a interface Tracker para conectar outro quadro. Para estendê-lo, um novo client de agente se conecta em Session, e novos roles e tasks entram em Delegate.

![Camadas do ensemble, de cima para baixo: Manager (a interface de usuário), Orchestrator (orquestração; trackers Linear e local), Delegate (a camada de roles), Session (o adaptador de clients)](../images/layers.svg)

| Camada | O que faz | Comece em |
|---|---|---|
| Manager | Um condutor para as camadas abaixo: um agente com quem você conversa, que lança workers, role runs e runs do quadro, acompanha-os e reporta. | [Demo](#demo-um-manager-vários-workers) |
| Orchestrator | Executa os roles conforme um arquivo workflow, com trackers substituíveis: Linear ou um quadro local de Markdown. | [Quadros](../architecture.md#board) |
| Delegate (núcleo) | Executa um role numa task por meio de uma session e devolve um resultado verificado. Um plugin do Claude Code e do Codex que você pode usar sozinho. | [Instalação](#instalação) |
| Session | Inicia, acompanha e retoma um agente de programação (Claude Code, Codex, OpenCode) no tmux ou num contêiner Docker, da mesma forma para todo client. | [Sessions](../architecture.md#session-settings) |

O workflow incluído reflete a minha filosofia de como os agentes devem trabalhar (pesquisa, depois um PRD, depois um build, com revisão humana entre cada etapa). `[orchestrator] workflow` seleciona outro arquivo workflow; um valor vazio usa o incluído. Os roles e as tasks também são seus para editar ([Personalizar roles e tasks](../customizing-roles-and-tasks.md)).

## Por que ensemble

![Trabalhar com agentes de programação: um agente, vários agentes à mão, ensemble](../images/compare.svg)

Um único agente deixa você esperando; vários agentes à mão fazem cada prompt, verificação e aprovação passar por você. Com o ensemble você conversa com um manager, que lança os workers, os acompanha e reporta.

## Cinco levels para dominar AI agents

Trabalhar com agents de programação é uma escada: cada level soma uma disciplina de engenharia às de baixo, e a habilidade está em escolher o level que o problema pede.

![Cinco levels para dominar AI agents: Drive, Equip, Systematize, Orchestrate, Master, com ensemble entre 4 e 5](../images/levels.svg)

| Level | Disciplina | O que significa |
|---|---|---|
| 1. Drive | prompt engineering e context engineering | Prompting conversacional, gestão do context, saber que modelo serve para cada trabalho. |
| 2. Equip | tools, skills, MCP | Dar ao agent ferramentas para alcançar e agir sobre o mundo (arquivos, APIs, um navegador, MCP servers), dentro de permissões e de um sandbox. |
| 3. Systematize | state engineering | Um workflow executado como máquina de estados: etapas definidas, novas tentativas, passos idempotentes, passagens de bastão. |
| 4. Orchestrate | loop engineering | Muitos agents: fan-out, planner e verifier, roteamento, limites de concorrência e de cota. |
| 5. Master | environment engineering | Guardrails e um ambiente agent-oriented: ferramentas, checks e verificação feitos para agents, não para humanos, para que o caminho mais fácil seja o certo (uma ideia de Lauren Tan). |

Ainda não chegou ao level 3? Experimente o [updraft](https://github.com/windroc-labs/updraft): pelo menos até lá ele te leva. As camadas do ensemble (a tabela do início) sobem mais: Session conduz a conversa do agent e lhe dá um host onde agir (levels 1–2), Delegate executa um role numa task como um workflow verificado (3), e o Orchestrator, com o manager por cima, coordena muitos agents (entre 4 e 5). Coloque você no topo, definindo o ambiente, os limites e os portões, e isso é Master, embora a verificação do ensemble ainda esteja um pouco fraca. A meta é o level 5 e além.

## Demo: um manager, vários workers

Sem o quadro, você pode usar o ensemble conversando com um único agente, o manager. Abra a session dele com `ctmux <name>`, inicie o Claude Code ou o Codex nela e invoque `/ensemble:manage`. Você passa o trabalho em palavras simples; ele lança um worker por task, acompanha os eventos, verifica os pull requests e reporta.

Comandos: [Início rápido](#início-rápido).

## Instalação

Cada client instala o plugin por conta própria. Antes de atualizar, veja [Upgrading](../upgrading.md).

| Client | Instalação |
|---|---|
| Claude Code | `/plugin marketplace add windroc-labs/agent-plugins-marketplace`, depois `/plugin install ensemble@windroc` |
| Codex 0.156.0+ | `codex plugin marketplace add windroc-labs/agent-plugins-marketplace` (para atualizar um já adicionado: `codex plugin marketplace upgrade windroc`), depois `codex plugin add ensemble@windroc`. `codex plugin list -m windroc` deve mostrá-lo instalado e habilitado. |

Nada disto é verificado. Se faltar algum, o run do agente falha.

| Requisito | Para |
|---|---|
| CLI `claude`, Python 3.11+ | todo run de agente |
| o plugin [`updraft`](https://github.com/windroc-labs/updraft), para workers no host: Claude Code `/plugin marketplace add windroc-labs/agent-plugins-marketplace`, depois `/plugin install updraft@windroc`; Codex `codex plugin marketplace add windroc-labs/agent-plugins-marketplace`, depois `codex plugin add updraft@windroc`. Os workers Docker instalam-no na imagem ([plugins](../architecture.md#plugins)). | as build tasks do engineer (`/updraft:build`, `/updraft:light-build`) e a revisão do PRD pelo PM (`/updraft:review`). Desnecessário fora isso. |
| tmux 3.3–3.8 | tui runs no Host `tmux` |
| [Ollama](https://ollama.com) só em `127.0.0.1`, com contexto de pelo menos 65536 e o modelo do tier baixado; no `tmux` também OpenCode 1.x (`npm install -g opencode-ai@1.19.3`) | só OpenCode ([Docker host › OpenCode](../architecture.md#opencode-on-docker), [OpenCode sessions](../customizing-roles-and-tasks.md#opencode-sessions)) |
| `git`, `gh` autenticado | programar (checkouts, pull requests) e destinos no GitHub. Desnecessário fora isso. |
| iTerm2 em execução | a divisão padrão do iTerm2 |
| Docker Desktop em execução; o broker `B` (`/usr/local/libexec/ensemble/ensemble-broker`), instalado com `sh <plugin dir>/src/broker/install.sh` (precisa de `sudo` e `gh` autenticado); um login de cada: `B login claude` (cole um `claude setup-token`), `B login github ensemble-broker-bot` e, para o Codex, `sudo B install-codex <version>` e depois `B login codex`; o OpenCode não precisa de nenhum | só o Docker Host ([Docker host](../architecture.md#docker-host)); o host não precisa de Node. O que cada credencial permite, onde fica e como revogá-la: [Credentials](../credentials.md). |

Como as sessions que o ensemble inicia carregam as suas configurações, e por que alguns comandos se recusam a rodar dentro delas: [Session settings](../architecture.md#session-settings).

| Comando | Faz |
|---|---|
| `ctmux <name>` | Abre a session tmux do commander: janelas nativas no iTerm2, um tmux attach simples em outros terminais, uma troca de session de dentro do tmux. `ctmux --install` coloca o ctmux e os quatro comandos `ensemble-crew`, `ensemble-session`, `ensemble-delegate` e `ensemble-orchestrator` em `~/.ensemble/bin/`: execute-os por esse caminho ou adicione-o ao `PATH`. Os comandos usam o plugin selecionado nesta instalação, mesmo com cópias de outros clientes presentes. Se uma atualização remover o diretório selecionado, o próximo comando registra e executa a cópia instalada mais recente do mesmo cliente e mostra o caminho em stderr, sem executar o instalador novamente. Se esse cliente não tiver mais nenhuma cópia instalada, o erro de seleção permanece. Para refs antigas, use o instalador. |
| `/ensemble:manage` | Lança e dirige workers, role runs e pipeline runs, com as diretrizes do commander. |
| `/ensemble:act-as <role>[:<task>] <input>` | Executa um role do núcleo nesta conversa. |
| `/ensemble:board` | Lê, encontra, cria, edita, move e arquiva issues do quadro com comandos fixos `ensemble-orchestrator issue`. |
| `ensemble-delegate run --list` | Lista todos os roles e tasks. |

## Início rápido

1. [Instale](#instalação) o plugin.
2. Numa nova session do Claude Code, verifique: `/ensemble:act-as dummy-tester:echo hello` devolve `hello`.
3. Execute um role: `/ensemble:act-as researcher <question>`. Sem configuração local, o relatório é salvo em `~/.ensemble/adhoc/<date>-researcher-<name>/agent/prompt.md`; para publicá-lo, defina o destination do role ([Personalizar roles e tasks](../customizing-roles-and-tasks.md#change-a-roles-run-settings)).
4. Para executar roles a partir de um quadro do Linear ou de um quadro local de Markdown: [Quadros](../architecture.md#board).

## Personalizar roles e tasks

- Configurações de run por role (nível do modelo, effort, client, destination, comandos): `[delegate.roles.<role>]` em `~/.ensemble/config.local.toml`.
- Os seus próprios roles, tasks e templates: um diretório de team listado em `[delegate] team_dirs`. Ele só adiciona; nunca altera um role embutido.
- Verificar: `ensemble-delegate run --list`, depois `ensemble-delegate run --role <role> --input <text> --dry-run`.

Formatos, regras e os limites do quadro: [Personalizar roles e tasks](../customizing-roles-and-tasks.md).

## Documentação

Estas páginas estão em inglês.

| Página | Cobre |
|---|---|
| [Customizing roles and tasks](../customizing-roles-and-tasks.md) | configurações de run, diretórios de team, arquivos de role e task, verificações, sessions do OpenCode |
| [Board](../architecture.md#board) | configuração, estados de issue e labels de task, operação do router, labels Agent (client por issue), quadro local (`tracker`, `ENSEMBLE_BOARD_DIR`, estrutura, formato de arquivo) |
| [Upgrading](../upgrading.md) | passos de atualização |
| [Core architecture](../architecture.md) | camadas e pacotes (desenvolvedores); configurações de session, `ensemble-session`, flags de client dos role runs |
| [Tests](../architecture.md#tests) | lint, suítes de testes, cobertura, testes ao vivo que um modelo local pode validar (desenvolvedores) |
