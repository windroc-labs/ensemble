<div align="center">

# ensemble

[English](../../README.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · **Español** · [Français](README.fr.md) · [Português](README.pt.md)

[![CI](https://github.com/windroc-labs/ensemble/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/windroc-labs/ensemble/actions/workflows/test.yml?query=branch%3Amain) [![Worker image](https://github.com/windroc-labs/ensemble/actions/workflows/worker-image.yml/badge.svg?branch=main)](https://github.com/windroc-labs/ensemble/actions/workflows/worker-image.yml?query=branch%3Amain) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../../LICENSE)

</div>

*Esto es una traducción; la versión de referencia es el README en inglés.*

Ejecuta agentes de programación (Claude Code, Codex y OpenCode con los modelos de Ollama del host) sin supervisión desde un tablero de Linear o un tablero local de archivos Markdown de issues. Orchestrator lee y escribe issues mediante una interfaz Tracker intercambiable; `[orchestrator] tracker` selecciona `local` (por defecto) o `linear`. Un archivo workflow define las etapas y sus roles. Cada role trabaja hasta en `max_runs` issues a la vez y te devuelve su resultado para que lo revises. Cuando lo apruebas, lo toma el siguiente role.

Desarrolladores: [`AGENTS.md`](../../AGENTS.md).

```mermaid
flowchart LR
    R["researcher<br>deep-research, research, light-research"] -->|"report → Approve → promote"| P["pm<br>product-design"]
    P -->|"PRD → Approve → promote"| E["engineer<br>build, light-build"] -->|pull request| Y[you]
    T[ensemble-orchestrator router] -.->|claims ready issues| R & P & E
```

## Arquitectura en capas

ensemble está construido en capas: cada una hace un solo trabajo y solo llama a la de abajo. Las dos capas de abajo, Session y Delegate, son piezas genéricas que puedes usar por separado o como base de tu propia orquestación. En Orchestrator, cambia el archivo workflow para definir las etapas o implementa la interfaz Tracker para conectar otro tablero. Para ampliarlo, un nuevo client de agente se conecta en Session, y los nuevos roles y tasks se añaden en Delegate.

![Capas de ensemble, de arriba abajo: Manager (la interfaz de usuario), Orchestrator (orquestación; trackers Linear y local), Delegate (la capa de roles), Session (el adaptador de clients)](../images/layers.svg)

| Capa | Qué hace | Empieza en |
|---|---|---|
| Manager | Un conductor para las capas de abajo: un agente con el que hablas, que lanza workers, role runs y runs del tablero, los vigila e informa. | [Demo](#demo-un-manager-muchos-workers) |
| Orchestrator | Ejecuta roles según un archivo workflow, con trackers intercambiables: Linear o un tablero local de Markdown. | [Tableros](../board.md) |
| Delegate (núcleo) | Ejecuta un role sobre una task a través de una session y devuelve un resultado verificado. Un plugin de Claude Code y Codex que puedes usar por separado. | [Instalación](#instalación) |
| Session | Inicia, vigila y reanuda un agente de programación (Claude Code, Codex, OpenCode) en tmux o en un contenedor Docker, igual para todos los clients. | [Sessions](../sessions.md) |

El workflow incluido refleja mi filosofía de cómo deberían trabajar los agentes (investigación, luego un PRD, luego un build, con revisión humana entre cada etapa). `[orchestrator] workflow` selecciona otro archivo workflow; un valor vacío usa el incluido. Los roles y las tasks también son tuyos para editar ([Personalizar roles y tasks](../customizing-roles-and-tasks.md)).

## Por qué ensemble

![Trabajar con agentes de programación: un agente, muchos agentes a mano, ensemble](../images/compare.svg)

Un solo agente te hace esperar; con muchos agentes a mano, cada prompt, revisión y aprobación pasa por ti. Con ensemble hablas con un manager, que lanza, vigila e informa sobre los workers.

## Cinco levels para dominar los AI agents

Trabajar con agents de programación es una escalera: cada level suma una disciplina de ingeniería sobre las de abajo, y la habilidad está en elegir el level que pide el problema.

![Cinco levels para dominar los AI agents: Drive, Equip, Systematize, Orchestrate, Master, con ensemble entre 4 y 5](../images/levels.svg)

| Level | Disciplina | Qué significa |
|---|---|---|
| 1. Drive | prompt engineering y context engineering | Prompting conversacional, gestión del context, saber qué modelo sirve para cada trabajo. |
| 2. Equip | tools, skills, MCP | Dar al agent herramientas para alcanzar y actuar sobre el mundo (archivos, APIs, un navegador, MCP servers), dentro de permisos y un sandbox. |
| 3. Systematize | state engineering | Un workflow ejecutado como máquina de estados: etapas definidas, reintentos, pasos idempotentes, traspasos. |
| 4. Orchestrate | loop engineering | Muchos agents: fan-out, planner y verifier, enrutamiento, límites de concurrencia y de cuota. |
| 5. Master | environment engineering | Barreras de seguridad y un entorno agent-oriented: herramientas, checks y verificación hechos para agents, no para humanos, para que el camino más fácil sea el correcto (una idea de Lauren Tan). |

¿Aún no llegas al level 3? Prueba [updraft](https://github.com/windroc-labs/updraft): al menos te lleva hasta ahí. Las capas de ensemble (la tabla del principio) suben más: Session lleva la conversación del agent y le da un host donde actuar (levels 1–2), Delegate ejecuta un role sobre una task como un workflow verificado (3), y el Orchestrator, con el manager encima, coordina muchos agents (entre 4 y 5). Ponte tú encima, fijando el entorno, los límites y las puertas, y eso es Master, aunque la verificación de ensemble todavía se queda un poco corta. La meta es el level 5 y más allá.

## Demo: un manager, muchos workers

Sin el tablero, puedes usar ensemble hablando con un solo agente, el manager. Abre su session con `ctmux <name>`, inicia Claude Code o Codex en ella e invoca `/ensemble:manage`. Le das trabajo con palabras normales; lanza un worker por cada task, vigila sus eventos, revisa sus pull requests e informa.

Comandos: [Inicio rápido](#inicio-rápido).

## Instalación

Cada client instala el plugin por su cuenta. Antes de actualizar, consulta [Upgrading](../upgrading.md).

| Client | Instalación |
|---|---|
| Claude Code | `/plugin marketplace add windroc-labs/agent-plugins-marketplace`, luego `/plugin install ensemble@windroc` |
| Codex 0.156.0+ | `codex plugin marketplace add windroc-labs/agent-plugins-marketplace` (para refrescar uno ya añadido: `codex plugin marketplace upgrade windroc`), luego `codex plugin add ensemble@windroc`. `codex plugin list -m windroc` debe mostrarlo instalado y habilitado. |

Nada de esto se comprueba. Si falta algo, el run del agente falla.

| Requisito | Para |
|---|---|
| CLI `claude`, Python 3.11+ | cada run de agente |
| el plugin [`updraft`](https://github.com/windroc-labs/updraft), para workers en el host: Claude Code `/plugin marketplace add windroc-labs/agent-plugins-marketplace`, luego `/plugin install updraft@windroc`; Codex `codex plugin marketplace add windroc-labs/agent-plugins-marketplace`, luego `codex plugin add updraft@windroc`. Los workers Docker lo instalan en la imagen ([plugins](../docker-host.md#plugins)). | las build tasks del engineer (`/updraft:build`, `/updraft:light-build`) y la revisión del PRD del PM (`/updraft:review`). No hace falta en otro caso. |
| tmux 3.3+ | tui runs en el Host `tmux` |
| [Ollama](https://ollama.com) solo en `127.0.0.1`, con contexto de al menos 65536 y el modelo del tier descargado; en `tmux` también OpenCode 1.x (`npm install -g opencode-ai@1.19.3`) | solo OpenCode ([Docker host › OpenCode](../docker-host.md#opencode), [OpenCode sessions](../sessions.md#opencode-sessions)) |
| `git`, `gh` con sesión iniciada | programar (checkouts, pull requests) y destinos de GitHub. No hace falta en otro caso. |
| iTerm2 en ejecución | la división de iTerm2 por defecto |
| Docker Desktop en ejecución; el broker `B` (`/usr/local/libexec/ensemble/ensemble-broker`), instalado con `sh <plugin dir>/src/broker/install.sh` (necesita `sudo` y `gh` con sesión iniciada); un login de cada uno: `B login claude` (pega un `claude setup-token`), `B login github ensemble-bot` y, para Codex, `sudo B install-codex <version>` y luego `B login codex`; OpenCode no necesita ninguno | solo el Docker Host ([Docker host](../docker-host.md)); el host no necesita Node. Qué permite cada credencial, dónde se guarda y cómo revocarla: [Credentials](../credentials.md). |

Cómo cargan tu configuración las sessions que inicia ensemble, y por qué algunos comandos se niegan dentro de ellas: [Session settings](../sessions.md#session-settings).

| Comando | Hace |
|---|---|
| `ctmux <name>` | Abre la session de tmux del commander: ventanas nativas en iTerm2, un tmux attach normal en otros terminales, un cambio de session desde dentro de tmux. `ctmux --install` pone ctmux y los cuatro comandos `ensemble-crew`, `ensemble-session`, `ensemble-delegate` y `ensemble-orchestrator` en `~/.ensemble/bin/`: ejecútalos con esa ruta o añádela a `PATH`. Cada uno ejecuta ese comando desde el plugin instalado en el momento de ejecutarse (primero la instalación de Claude Code, luego la más reciente de Codex), así que una actualización no requiere reinstalar; vuelve a ejecutar `--install` para refrescarlos. |
| `/ensemble:manage` | Lanza y dirige workers, role runs y pipeline runs, con las pautas del commander. |
| `/ensemble:act-as <role>[:<task>] <input>` | Ejecuta un role del núcleo en esta conversación. |
| `/ensemble:board` | Lee, busca, crea, edita, mueve y archiva issues del tablero con los comandos fijos `ensemble-orchestrator issue`. |
| `ensemble-delegate run --list` | Lista todos los roles y tasks. |

## Inicio rápido

1. [Instala](#instalación) el plugin.
2. En una session nueva de Claude Code, compruébalo: `/ensemble:act-as dummy-tester:echo hello` devuelve `hello`.
3. Ejecuta un role: `/ensemble:act-as researcher <question>`. Sin configuración local, el informe se guarda en `~/.ensemble/adhoc/<date>-researcher-<name>/agent/prompt.md`; para publicarlo, configura el destination del role ([Personalizar roles y tasks](../customizing-roles-and-tasks.md#change-a-roles-run-settings)).
4. Para ejecutar roles desde un tablero de Linear o un tablero local de Markdown: [Tableros](../board.md).

## Personalizar roles y tasks

- Ajustes de run por role (nivel de modelo, effort, client, destination, comandos): `[delegate.roles.<role>]` en `~/.ensemble/config.local.toml`.
- Tus propios roles, tasks y plantillas: un directorio de team listado en `[delegate] team_dirs`. Solo añade; nunca cambia un role integrado.
- Comprobar: `ensemble-delegate run --list`, luego `ensemble-delegate run --role <role> --input <text> --dry-run`.

Formatos, reglas y los límites del tablero: [Personalizar roles y tasks](../customizing-roles-and-tasks.md).

## Documentación

Estas páginas están en inglés.

| Página | Cubre |
|---|---|
| [Customizing roles and tasks](../customizing-roles-and-tasks.md) | ajustes de run, directorios de team, archivos de role y task, comprobaciones |
| [Board](../board.md) | configuración, estados de issue y etiquetas de task, operar el router, etiquetas Agent (client por issue), tablero local (`tracker`, `ENSEMBLE_BOARD_DIR`, estructura, formato de archivo) |
| [Sessions and client policy](../sessions.md) | ajustes de session, `ensemble-session`, flags de client de los role runs, sessions de OpenCode |
| [Upgrading](../upgrading.md) | pasos de actualización |
| [Core architecture](../architecture.md) | capas y paquetes (desarrolladores) |
| [Tests](../tests.md) | lint, suites de tests, cobertura, tests en vivo que un modelo local puede validar (desarrolladores) |
