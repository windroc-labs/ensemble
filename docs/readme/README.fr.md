# Ensemble

[English](../../README.md) · [Español](README.es.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Português](README.pt.md) · [简体中文](README.zh-CN.md)

Ensemble permet de travailler avec des agents de programmation en équipe. Discutez avec un commandant, déléguez une tâche à un rôle
ou laissez un tableau répartir le travail entre les rôles. Claude Code, Codex et OpenCode partagent les mêmes commandes de session.

<a id="what"></a>
## Ce que vous pouvez exécuter

![Trois points d’entrée : un agent exécutant seul, un rôle avec une tâche ou un flux de travail sur tableau](../images/levels.svg)

Choisissez le périmètre dont vous avez besoin. Un agent exécutant correspond à une conversation avec un client ; un rôle y ajoute une charte, une tâche et un résultat vérifié ;
un tableau planifie les exécutions des rôles et reporte leurs résultats dans les tickets. Le tableau peut être Linear ou du Markdown local.

<a id="why"></a>
## Pourquoi une équipe

![Coordination directe d’agents exécutants séparés, comparée à un commandant pilotant une équipe](../images/compare.svg)

Des exécutions d’agents indépendantes demandent à quelqu’un de répartir le travail et de suivre leurs réponses. Un commandant utilise Crew pour lancer,
inspecter et reprendre ces exécutions depuis une seule conversation. Vous décidez toujours du travail à autoriser et à vérifier.

<a id="how-it-works"></a>
## Fonctionnement

![Frontières des couches : commandes Manager, Orchestrator, Delegate, Session et Utils partagés](../images/layers.svg)

![Frontières d’exécution : harnais de session, gardes locaux et Docker, identifiants du broker et proxy de sortie](../images/system.svg)

Le contrôle, la transmission des identifiants et les chemins réseau sont distincts. La [présentation de l’architecture](../architecture.md)
explique les responsabilités et la confiance ; les détails d’implémentation se trouvent dans le code source lié.

<a id="install"></a>
## Installation

Sur un Mac avec puce Apple silicon, exécutez l’installateur de la version épinglée par le
[marketplace windroc](https://github.com/windroc-labs/agent-plugins-marketplace/blob/main/.claude-plugin/marketplace.json).
Remplacez `<tag>` par le `source.ref` associé à ensemble dans ce fichier :

```sh
curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install.sh | bash
```

L’installateur demande quels clients utiliser, si les agents exécutants doivent fonctionner en local ou dans Docker, et s’il faut configurer un
tableau. Il installe les dépendances et plugins sélectionnés, demande les connexions nécessaires et configure `ctmux`.
Relancez-le pour terminer une installation interrompue. Pour les options, utilisez `bash scripts/install.sh --help` dans une copie du dépôt ;
pour une installation existante, lisez d’abord [Mise à niveau](../upgrading.md).

Utilisez le compte de modèles ou le fournisseur requis par le client choisi. La configuration du fournisseur et du réseau pour OpenCode figure
dans [Personnalisation des rôles et des tâches](../customizing-roles-and-tasks.md#opencode-sessions).
Le `gh` de l’hôte doit pouvoir pousser et gérer les PR dans les dépôts cibles : les jetons classiques nécessitent `repo`, `read:org` et `gist` ;
les jetons à permissions fines nécessitent aussi un accès en lecture à Actions pour les vérifications.

### GitHub App

Les agents exécutants Docker utilisent une GitHub App plutôt que la connexion `gh` de l’hôte. L’installateur ne crée pas l’App.
Dans GitHub Settings › Developer settings › GitHub Apps, créez une App privée installable uniquement sur votre compte,
avec les webhooks désactivés. Ses permissions de dépôt doivent couvrir `github.permissions` dans la
[configuration de Session](../../src/ensemble/session/config.toml). Installez-la uniquement sur les dépôts vers lesquels les agents exécutants peuvent pousser,
y compris les dépôts de sortie des documents.

Générez une clé privée, notez l’App ID et importez-les :

```sh
/usr/local/libexec/ensemble/ensemble-broker login github ensemble-broker-bot \
  --key-file KEY.pem --app-id APP_ID
```

Supprimez le PEM après l’importation. La [rotation des identifiants](../upgrading.md#credential-rotation) explique leur remplacement et leur révocation.

<a id="quick-start"></a>
## Démarrage rapide

Pour une seule tâche, dans une conversation de votre client installé :

```text
/ensemble:act-as dummy-tester:echo hello
```

Pour une équipe, ouvrez un shell de commandant et démarrez votre client :

```sh
ctmux work
claude
```

Utilisez `codex` ou `opencode` à la place de `claude` si c’est votre client. Dans cette conversation, invoquez :

```text
/ensemble:manage
```

Décrivez le travail et le livrable souhaité. Le commandant coordonne les agents exécutants et les exécutions des rôles via Crew.
Pour travailler avec un tableau, `/ensemble:board` agit sur les tickets ; la [personnalisation du flux de travail](../customizing-roles-and-tasks.md#on-the-board)
explique comment choisir les étapes.

<a id="docs"></a>
## Documentation

| À lire quand vous avez besoin de… | Où |
|---|---|
| Comprendre les couches du système et les frontières d’exécution et de confiance | [Architecture](../architecture.md) |
| Configurer les modèles, fournisseurs et rôles, vos propres tâches ou le flux de travail | [Personnalisation des rôles et des tâches](../customizing-roles-and-tasks.md) |
| Connaître les changements nécessaires à une installation existante | [Mise à niveau](../upgrading.md) |
| Connaître les conventions de contribution et les points d’entrée des vérifications | [AGENTS.md](../../AGENTS.md) |
