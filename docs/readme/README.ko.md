# Ensemble

[English](../../README.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [Português](README.pt.md) · [简体中文](README.zh-CN.md)

Ensemble을 사용하면 코딩 에이전트와 팀으로 일할 수 있습니다. 커맨더와 대화하거나, 역할에 작업을 위임하거나,
보드가 역할 간에 작업을 배분하도록 할 수 있습니다. Claude Code, Codex, OpenCode는 같은 세션 제어 기능을 공유합니다.

<a id="what"></a>
## 실행할 수 있는 것

![세 가지 진입점: 단독 워커, 역할과 작업, 보드 워크플로](../images/levels.svg)

필요한 범위를 선택하세요. 워커는 클라이언트와의 대화입니다. 역할은 여기에 역할 규정, 작업, 결과를 더합니다.
보드는 역할 실행을 예약하고 그 결과를 이슈에 기록합니다. 보드로는 Linear 또는 로컬 Markdown을 사용할 수 있습니다.

<a id="why"></a>
## 팀을 사용하는 이유

![개별 워커를 직접 조율하는 방식과 한 커맨더가 팀을 제어하는 방식의 비교](../images/compare.svg)

에이전트를 따로 실행하면 누군가 작업을 배분하고 응답을 추적해야 합니다. 커맨더는 Crew를 사용해 하나의 대화에서
이러한 실행을 시작하고, 살펴보고, 재개합니다. 어떤 작업을 승인하고 검토할지는 여전히 사용자가 결정합니다.

<a id="how-it-works"></a>
## 작동 방식

![계층 경계: Manager 명령, Orchestrator, Delegate, Session, 공유 Utils](../images/layers.svg)

![런타임 경계: 세션 하니스, 로컬 및 Docker 가드, broker 자격 증명, 외부 통신 프록시](../images/system.svg)

제어, 자격 증명 전달, 네트워크 경로는 서로 구분됩니다. [아키텍처 개요](../architecture.md)에서 각 부분의 책임과 신뢰 관계를 설명하며,
구현 세부 사항은 연결된 소스 코드에서 확인할 수 있습니다.

<a id="install"></a>
## 설치

Apple silicon Mac에서 [windroc 마켓플레이스](https://github.com/windroc-labs/agent-plugins-marketplace/blob/main/.claude-plugin/marketplace.json)에
고정된 릴리스의 설치 프로그램을 실행하세요.
`<tag>`를 해당 파일에 있는 ensemble의 `source.ref`로 바꾸세요.

```sh
curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install.sh | bash
```

설치 프로그램은 사용할 클라이언트, 워커를 로컬 또는 Docker 중 어디에서 실행할지, 보드를 설정할지를 묻습니다.
선택한 의존성과 플러그인을 설치하고, 필요한 로그인을 요청하며, `ctmux`를 설정합니다.
설치가 중단되었다면 다시 실행하여 마무리하세요. 옵션은 저장소 체크아웃에서 `bash scripts/install.sh --help`로 확인할 수 있습니다.
기존 설치가 있다면 먼저 [업그레이드](../upgrading.md)를 읽으세요.

선택한 클라이언트에 필요한 모델 계정 또는 제공자를 사용하세요. OpenCode 제공자 및 네트워크 설정은
[역할 및 작업 사용자 지정](../customizing-roles-and-tasks.md#opencode-sessions)에 있습니다.
호스트의 `gh`에는 대상 저장소에서 push 및 PR 작업을 수행할 권한이 필요합니다. 클래식 토큰에는 `repo`, `read:org`, `gist`가 필요하며,
세분화된 토큰에는 검사 결과를 확인하기 위한 Actions 읽기 권한도 필요합니다.

### GitHub App

Docker 워커는 호스트의 `gh` 로그인 대신 GitHub App을 사용합니다. 설치 프로그램은 App을 생성하지 않습니다.
GitHub Settings › Developer settings › GitHub Apps에서 자신의 계정에만 설치할 수 있는 비공개 App을 만들고
웹훅을 끄세요. 저장소 권한은 [Session 설정](../../src/ensemble/session/config.toml)의
`github.permissions`를 충족해야 합니다. 문서 출력 저장소를 포함해 워커가 push할 수 있는 저장소에만 설치하세요.

개인 키를 생성하고 App ID를 기록한 다음 가져오세요.

```sh
/usr/local/libexec/ensemble/ensemble-broker login github ensemble-broker-bot \
  --key-file KEY.pem --app-id APP_ID
```

가져온 뒤 PEM을 삭제하세요. [자격 증명 교체](../upgrading.md#credential-rotation)에서 교체와 폐기를 설명합니다.

<a id="quick-start"></a>
## 빠른 시작

단일 작업을 실행하려면 설치된 클라이언트의 대화에서 다음을 입력하세요.

```text
/ensemble:act-as dummy-tester:echo hello
```

팀을 사용하려면 커맨더 셸을 열고 클라이언트를 시작하세요.

```sh
ctmux work
claude
```

사용하는 클라이언트가 `codex` 또는 `opencode`라면 `claude` 대신 사용하세요. 해당 대화에서 다음을 호출하세요.

```text
/ensemble:manage
```

할 일과 원하는 결과물을 설명하세요. 커맨더는 Crew를 통해 워커와 역할 실행을 조율합니다.
보드 작업에서는 `/ensemble:board`가 이슈를 조작합니다. 단계 선택 방법은
[워크플로 사용자 지정](../customizing-roles-and-tasks.md#on-the-board)에서 설명합니다.

<a id="docs"></a>
## 문서

| 필요한 내용 | 위치 |
|---|---|
| 시스템 계층, 실행 및 신뢰 경계 | [아키텍처](../architecture.md) |
| 모델, 제공자, 역할 설정, 사용자 고유의 작업 또는 워크플로 | [역할 및 작업 사용자 지정](../customizing-roles-and-tasks.md) |
| 기존 설치에 필요한 변경 사항 | [업그레이드](../upgrading.md) |
| 기여자 규칙과 검사 실행 방법 | [AGENTS.md](../../AGENTS.md) |
