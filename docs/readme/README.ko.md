# Ensemble

[English](../../README.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [Português](README.pt.md) · [简体中文](README.zh-CN.md)

Ensemble을 사용하면 코딩 에이전트와 팀으로 일할 수 있습니다. 커맨더와 대화하거나, 역할에 작업을 위임하거나,
보드가 역할 간에 작업을 배분하도록 할 수 있습니다. Claude Code, Codex, OpenCode는 같은 세션 제어 기능을 공유합니다.

<a id="what"></a>
## 실행할 수 있는 것

![다섯 가지 실천 단계: Drive, Equip, Systematize, Orchestrate, Master](../images/levels.svg)

필요한 범위를 선택하세요. 워커는 클라이언트와의 대화입니다. 역할은 여기에 역할 규정, 작업, 결과를 더합니다.
보드는 역할 실행을 예약하고 그 결과를 이슈에 기록합니다. 보드로는 Linear 또는 로컬 Markdown을 사용할 수 있습니다.

<a id="why"></a>
## 팀을 사용하는 이유

![단일 에이전트, 수동으로 조율하는 여러 에이전트, 커맨더가 조율하는 ensemble 팀](../images/compare.svg)

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

Keychain에서 `sudo` 비밀번호를 전달하려면 스크립트를 먼저 다운로드하세요. stdin으로 스크립트 본문까지 전달할 수는 없습니다. `<item>`을 Keychain 서비스 이름으로 바꾸세요.

```bash
(f=$(mktemp) && trap 'rm -f "$f"' EXIT && curl -fsSL -o "$f" https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install.sh && security find-generic-password -s <item> -w | bash "$f" --password-stdin)
```

| 플래그 | 읽는 값 |
|---|---|
| `--password-stdin` | stdin에서 `sudo` 비밀번호를 읽으며, 공급자 키보다 먼저 전달합니다 |

설치 프로그램은 사용할 클라이언트, 워커를 로컬 또는 Docker 중 어디에서 실행할지, 보드를 설정할지를 묻습니다.
선택한 의존성과 플러그인을 설치하고, 필요한 로그인을 요청하며, `ensemble` 명령을 설정합니다.
설치가 중단되었다면 다시 실행하여 마무리하세요. 옵션은 저장소 체크아웃에서 `bash scripts/install.sh --help`로 확인할 수 있습니다.
기존 설치가 있다면 먼저 [업그레이드](../upgrading.md)를 읽으세요.

선택한 클라이언트에 필요한 모델 계정 또는 제공자를 사용하세요. OpenCode 제공자 및 네트워크 설정은
[역할 및 작업 사용자 지정](../customizing-roles-and-tasks.md#opencode-sessions)에 있습니다.
호스트의 `gh`에는 대상 저장소에서 push 및 PR 작업을 수행할 권한이 필요합니다. 클래식 토큰에는 `repo`, `read:org`, `gist`가 필요하며,
세분화된 토큰에는 검사 결과를 확인하기 위한 Actions 읽기 권한도 필요합니다.

### GitHub App

`github` 자격 증명을 선언한 Docker 워커는 GitHub App을 사용합니다. 설치 프로그램은 App을 생성하지 않습니다.
GitHub Settings › Developer settings › GitHub Apps에서 자신의 계정에만 설치할 수 있는 비공개 App을 만들고
웹훅을 끄세요. 저장소 권한은 설치 프로그램이 작성하는 engineer 역할의 자격 증명 선언
`[orchestrator.roles.engineer.credentials.github]`를 충족해야 합니다. 문서 출력 저장소를 포함해 워커가 push할 수 있는 저장소에만 설치하세요.
선언이 없는 Docker run에는 GitHub 토큰이 제공되지 않습니다. 직접 시작하는 워커와 verifier의 출력 권한 설정은
[명시적 GitHub 자격 증명](../upgrading.md#broker-protocol-35-explicit-github-credentials)을 참조하세요.

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
ensemble work
claude
```

사용하는 클라이언트가 `codex --no-daemon` 또는 `opencode`라면 `claude` 대신 사용하세요. 해당 대화에서 다음을 호출하세요.

```text
/ensemble:manage
```

할 일과 원하는 결과물을 설명하세요. 커맨더는 Crew를 통해 워커와 역할 실행을 조율합니다.
나중에 `work`로 돌아오려면 터미널에서 `ensemble list`를 실행하고 고르세요. 그 세션과 대화로 돌아갑니다.
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
