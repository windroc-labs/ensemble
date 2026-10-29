# Ensemble

[English](../../README.md) · [Español](README.es.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Português](README.pt.md) · [简体中文](README.zh-CN.md)

Ensemble permet de travailler avec des agents de programmation en équipe. Discutez avec un commandant, déléguez une tâche à un rôle
ou laissez un tableau répartir le travail entre les rôles. Claude Code, Codex et OpenCode partagent les mêmes commandes de session.

<a id="what"></a>
## Ce que vous pouvez exécuter

![Cinq disciplines : Drive, Equip, Systematize, Orchestrate et Master](../images/levels.svg)

Choisissez le périmètre dont vous avez besoin. Un agent exécutant correspond à une conversation avec un client ; un rôle y ajoute une charte, une tâche et un résultat ;
un tableau planifie les exécutions des rôles et reporte leurs résultats dans les tickets. Le tableau peut être Linear ou du Markdown local.

<a id="why"></a>
## Pourquoi une équipe

![Un agent, plusieurs agents coordonnés manuellement et des équipes ensemble coordonnées par des commandants](../images/compare.svg)

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

Pour fournir le mot de passe `sudo` depuis Keychain, téléchargez d’abord le script ; stdin ne peut pas aussi contenir son texte. Remplacez `<item>` par le nom du service Keychain :

```bash
(f=$(mktemp) && trap 'rm -f "$f"' EXIT && curl -fsSL -o "$f" https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install.sh && security find-generic-password -s <item> -w | bash "$f" --password-stdin)
```

| Option | Lit |
|---|---|
| `--password-stdin` | Le mot de passe `sudo` depuis stdin, avant la clé du fournisseur |

L’installateur demande quels clients utiliser, si les agents exécutants doivent fonctionner en local ou dans Docker, et s’il faut configurer un
tableau. Il installe les dépendances et plugins sélectionnés, demande les connexions nécessaires et configure la commande `ensemble`.
Relancez-le pour terminer une installation interrompue. Pour les options, utilisez `bash scripts/install.sh --help` dans une copie du dépôt ;
pour une installation existante, lisez d’abord [Mise à niveau](../upgrading.md).

Utilisez le compte de modèles ou le fournisseur requis par le client choisi. La configuration du fournisseur et du réseau pour OpenCode figure
dans [Personnalisation des rôles et des tâches](../customizing-roles-and-tasks.md#opencode-sessions).
Le `gh` de l’hôte doit pouvoir pousser et gérer les PR dans les dépôts cibles : les jetons classiques nécessitent `repo`, `read:org` et `gist` ;
les jetons à permissions fines nécessitent aussi un accès en lecture à Actions pour les vérifications.

### GitHub App

Les agents exécutants Docker ayant déclaré un identifiant `github` utilisent une GitHub App. L’installateur ne crée pas l’App.
Dans GitHub Settings › Developer settings › GitHub Apps, créez une App privée installable uniquement sur votre compte,
avec les webhooks désactivés. Ses permissions de dépôt doivent couvrir la déclaration d’identifiants du rôle engineer,
écrite par l’installateur dans `[orchestrator.roles.engineer.credentials.github]`. Installez-la uniquement sur les dépôts
vers lesquels les agents exécutants peuvent pousser, y compris les dépôts de sortie des documents. Les exécutions Docker
sans déclaration ne reçoivent aucun jeton GitHub. Pour les agents lancés directement et l’accès de sortie du verifier, voir
[Identifiants GitHub explicites](../upgrading.md#broker-protocol-35-explicit-github-credentials).

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
ensemble work
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
