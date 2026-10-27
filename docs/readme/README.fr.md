<div align="center">

# ensemble

[English](../../README.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · **Français** · [Português](README.pt.md)

[![CI](https://github.com/windroc-labs/ensemble/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/windroc-labs/ensemble/actions/workflows/test.yml?query=branch%3Amain) [![Worker image](https://github.com/windroc-labs/ensemble/actions/workflows/worker-image.yml/badge.svg?branch=main)](https://github.com/windroc-labs/ensemble/actions/workflows/worker-image.yml?query=branch%3Amain) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../../LICENSE)

</div>

*Ceci est une traduction ; la version de référence est le README en anglais.*

Fait tourner des agents de code (Claude Code, Codex, OpenCode sur les modèles Ollama de l'hôte) sans surveillance depuis un tableau Linear ou un tableau local de fichiers Markdown d'issues. Orchestrator lit et écrit les issues via une interface Tracker interchangeable ; `[orchestrator] tracker` sélectionne `local` (par défaut) ou `linear`. Un fichier workflow définit les étapes et leurs roles. Chaque role traite jusqu'à `max_runs` issues à la fois et vous rend son résultat pour relecture. Une fois que vous approuvez, le role suivant prend le relais.

Développeurs : [`AGENTS.md`](../../AGENTS.md).

```mermaid
flowchart LR
    R["researcher<br>deep-research, research, light-research"] -->|"report → Approve → promote"| P["pm<br>product-design"]
    P -->|"PRD → Approve → promote"| E["engineer<br>build, light-build"] -->|pull request| Y[you]
    T[ensemble-orchestrator router] -.->|claims ready issues| R & P & E
```

## Architecture en couches

ensemble est construit en couches : chacune fait un seul travail et n'appelle que celle du dessous. Les deux couches du bas, Session et Delegate, sont des briques génériques utilisables seules ou comme base de votre propre orchestration. Dans Orchestrator, changez le fichier workflow pour définir les étapes, ou implémentez l'interface Tracker pour connecter un autre tableau. Pour l'étendre, un nouveau client d'agent se branche sur Session, et de nouveaux roles et tasks s'ajoutent dans Delegate.

![Couches d'ensemble, de haut en bas : Manager (l'interface utilisateur), Orchestrator (l'orchestration ; trackers Linear et local), Delegate (la couche des roles), Session (l'adaptateur de clients)](../images/layers.svg)

| Couche | Ce qu'elle fait | Commencer par |
|---|---|---|
| Manager | Un pilote pour les couches du dessous : un agent à qui vous parlez, qui lance des workers, des role runs et des runs du tableau, les surveille et vous fait un rapport. | [Démo](#démo--un-manager-plusieurs-workers) |
| Orchestrator | Fait tourner les roles selon un fichier workflow, avec des trackers interchangeables : Linear ou un tableau local de Markdown. | [Tableaux](../architecture.md#board) |
| Delegate (cœur) | Fait exécuter une task à un role via une session et renvoie un résultat vérifié. Un plugin Claude Code et Codex utilisable seul. | [Installation](#installation) |
| Session | Démarre, surveille et reprend un agent de code (Claude Code, Codex, OpenCode) dans tmux ou un conteneur Docker, de la même façon pour chaque client. | [Sessions](../architecture.md#session-settings) |

Le workflow fourni reflète ma philosophie de la façon dont les agents devraient travailler (recherche, puis un PRD, puis un build, avec une relecture humaine entre chaque étape). `[orchestrator] workflow` sélectionne un autre fichier workflow ; une valeur vide utilise celui fourni. Les roles et les tasks sont aussi à vous de modifier ([Personnaliser les roles et les tasks](../customizing-roles-and-tasks.md)).

## Pourquoi ensemble

![Travailler avec des agents de code : un agent, plusieurs agents à la main, ensemble](../images/compare.svg)

Un seul agent vous fait attendre ; plusieurs agents à la main font passer chaque prompt, vérification et approbation par vous. Avec ensemble, vous parlez à un manager, qui lance les workers, les surveille et vous en rend compte.

## Cinq levels pour maîtriser les AI agents

Travailler avec des agents de code, c'est une échelle : chaque level ajoute une discipline d'ingénierie au-dessus des précédentes, et tout l'art consiste à choisir le level adapté au problème.

![Cinq levels pour maîtriser les AI agents : Drive, Equip, Systematize, Orchestrate, Master, avec ensemble entre 4 et 5](../images/levels.svg)

| Level | Discipline | Ce que cela veut dire |
|---|---|---|
| 1. Drive | prompt engineering et context engineering | Prompting conversationnel, gestion du context, savoir quel modèle convient à quelle tâche. |
| 2. Equip | tools, skills, MCP | Donner à l'agent des outils pour atteindre le monde et agir dessus (fichiers, APIs, navigateur, MCP servers), dans les limites des permissions et d'un sandbox. |
| 3. Systematize | state engineering | Un workflow exécuté comme une machine à états : étapes définies, relances, étapes idempotentes, passages de relais. |
| 4. Orchestrate | loop engineering | Plusieurs agents : fan-out, planner et verifier, routage, limites de concurrence et de quota. |
| 5. Master | environment engineering | Garde-fous et environnement agent-oriented : outils, checks et vérification pensés pour les agents, pas pour les humains, pour que le chemin le plus facile soit le bon (une idée de Lauren Tan). |

Pas encore au level 3 ? Essayez [updraft](https://github.com/windroc-labs/updraft) : il vous y mène, au moins. Les couches d'ensemble (le tableau du début) montent plus haut : Session porte la conversation de l'agent et lui donne un host où agir (levels 1–2), Delegate exécute un role sur une task comme un workflow vérifié (3), et le Orchestrator, avec le manager au-dessus, coordonne plusieurs agents (entre 4 et 5). Placez-vous au sommet, à fixer l'environnement, les limites et les points de contrôle, et c'est Master, même si la vérification d'ensemble reste un peu légère. L'objectif : le level 5 et au-delà.

## Démo : un manager, plusieurs workers

Sans le tableau, vous pouvez utiliser ensemble en parlant à un seul agent, le manager. Ouvrez sa session avec `ctmux <name>`, lancez-y Claude Code ou Codex et invoquez `/ensemble:manage`. Vous lui confiez le travail avec des mots simples ; il lance un worker par task, surveille leurs événements, vérifie leurs pull requests et vous fait un rapport.

Commandes : [Démarrage rapide](#démarrage-rapide).

## Installation

Chaque client installe le plugin lui-même. Avant une mise à jour, voir [Upgrading](../upgrading.md).

| Client | Installation |
|---|---|
| Claude Code | `/plugin marketplace add windroc-labs/agent-plugins-marketplace`, puis `/plugin install ensemble@windroc` |
| Codex 0.156.0+ | `codex plugin marketplace add windroc-labs/agent-plugins-marketplace` (pour rafraîchir un marketplace déjà ajouté : `codex plugin marketplace upgrade windroc`), puis `codex plugin add ensemble@windroc`. `codex plugin list -m windroc` doit l'afficher installé et activé. |

Rien de tout cela n'est vérifié. S'il en manque un, le run de l'agent échoue.

| Prérequis | Pour |
|---|---|
| CLI `claude`, Python 3.11+ | chaque run d'agent |
| le plugin [`updraft`](https://github.com/windroc-labs/updraft), pour les workers sur le host : Claude Code `/plugin marketplace add windroc-labs/agent-plugins-marketplace`, puis `/plugin install updraft@windroc` ; Codex `codex plugin marketplace add windroc-labs/agent-plugins-marketplace`, puis `codex plugin add updraft@windroc`. Les workers Docker l’installent dans l’image ([plugins](../architecture.md#plugins)). | les build tasks de l'engineer (`/updraft:build`, `/updraft:light-build`) et la revue du PRD par le PM (`/updraft:review`). Inutile sinon. |
| tmux 3.3–3.8 | les tui runs sur le Host `tmux` |
| [Ollama](https://ollama.com) sur `127.0.0.1` seulement, contexte d'au moins 65536, modèle du tier téléchargé ; sur `tmux` aussi OpenCode 1.x (`npm install -g opencode-ai@1.19.3`) | OpenCode seulement ([Docker host › OpenCode](../architecture.md#opencode-on-docker), [OpenCode sessions](../customizing-roles-and-tasks.md#opencode-sessions)) |
| `git`, `gh` connecté | coder (checkouts, pull requests) et destinations GitHub. Inutile sinon. |
| iTerm2 lancé | le split iTerm2 par défaut |
| Docker Desktop lancé ; le broker `B` (`/usr/local/libexec/ensemble/ensemble-broker`), installé par `sh <plugin dir>/src/broker/install.sh` (demande `sudo` et `gh` connecté) ; une connexion chacun : `B login claude` (collez un `claude setup-token`), `B login github ensemble-broker-bot` et, pour Codex, `sudo B install-codex <version>` puis `B login codex` ; OpenCode n'en demande aucune | le Docker Host seulement ([Docker host](../architecture.md#docker-host)) ; pas de Node sur l'hôte. Ce que permet chaque identifiant, où il est stocké et comment le révoquer : [Credentials](../credentials.md). |

Comment les sessions lancées par ensemble chargent vos réglages, et pourquoi certaines commandes y refusent de s'exécuter : [Session settings](../architecture.md#session-settings).

| Commande | Rôle |
|---|---|
| `ctmux <name>` | Ouvre la session tmux du commander : fenêtres natives dans iTerm2, un simple tmux attach dans les autres terminaux, un basculement depuis l'intérieur de tmux. `ctmux --install` place ctmux et les quatre commandes `ensemble-crew`, `ensemble-session`, `ensemble-delegate` et `ensemble-orchestrator` dans `~/.ensemble/bin/` : lancez-les par ce chemin, ou ajoutez-le au `PATH`. Les commandes utilisent le plugin sélectionné lors de cette installation, même si des copies d’autres clients restent présentes. Si une mise à jour supprime le répertoire sélectionné, la commande suivante enregistre et lance la copie installée la plus récente du même client et affiche son chemin sur stderr, sans relancer l’installateur. Si ce client n’a plus de copie installée, l’erreur de sélection reste inchangée. Pour les anciennes refs, relancez l’installateur. |
| `/ensemble:manage` | Lance et dirige des workers, des role runs et des pipeline runs, selon les consignes du commander. |
| `/ensemble:act-as <role>[:<task>] <input>` | Exécute un role du cœur dans cette conversation. |
| `/ensemble:board` | Lit, trouve, crée, modifie, déplace et archive les issues du tableau avec les commandes fixes `ensemble-orchestrator issue`. |
| `ensemble-delegate run --list` | Liste tous les roles et toutes les tasks. |

## Démarrage rapide

1. [Installez](#installation) le plugin.
2. Dans une nouvelle session Claude Code, vérifiez-le : `/ensemble:act-as dummy-tester:echo hello` renvoie `hello`.
3. Lancez un role : `/ensemble:act-as researcher <question>`. Sans configuration locale, le rapport est enregistré dans `~/.ensemble/adhoc/<date>-researcher-<name>/agent/prompt.md` ; pour le publier, définissez la destination du role ([Personnaliser les roles et les tasks](../customizing-roles-and-tasks.md#change-a-roles-run-settings)).
4. Pour faire tourner les roles depuis un tableau Linear ou un tableau local de Markdown : [Tableaux](../architecture.md#board).

## Personnaliser les roles et les tasks

- Réglages de run par role (niveau de modèle, effort, client, destination, commandes) : `[delegate.roles.<role>]` dans `~/.ensemble/config.local.toml`.
- Vos propres roles, tasks et modèles : un répertoire de team listé dans `[delegate] team_dirs`. Il ne fait qu'ajouter ; il ne modifie jamais un role intégré.
- Vérifier : `ensemble-delegate run --list`, puis `ensemble-delegate run --role <role> --input <text> --dry-run`.

Formats, règles et limites du tableau : [Personnaliser les roles et les tasks](../customizing-roles-and-tasks.md).

## Documentation

Ces pages sont en anglais.

| Page | Contenu |
|---|---|
| [Customizing roles and tasks](../customizing-roles-and-tasks.md) | réglages de run, répertoires de team, fichiers de role et de task, vérifications, sessions OpenCode |
| [Board](../architecture.md#board) | mise en place, états d'issue et labels de task, exploitation du router, labels Agent (client par issue), tableau local (`tracker`, `ENSEMBLE_BOARD_DIR`, arborescence, format des fichiers) |
| [Upgrading](../upgrading.md) | étapes de mise à jour |
| [Core architecture](../architecture.md) | couches et paquets (développeurs) ; réglages de session, `ensemble-session`, flags de client des role runs |
| [Tests](../architecture.md#tests) | lint, suites de tests, couverture, tests réels qu'un modèle local peut valider (développeurs) |
