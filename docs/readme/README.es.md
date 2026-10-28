# Ensemble

[English](../../README.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Português](README.pt.md) · [简体中文](README.zh-CN.md)

Ensemble te permite trabajar con agentes de programación como un equipo. Habla con un comandante, delega una tarea a un rol
o deja que un tablero distribuya el trabajo entre roles. Claude Code, Codex y OpenCode comparten los mismos controles de sesión.

<a id="what"></a>
## Qué puedes ejecutar

![Cinco disciplinas: Drive, Equip, Systematize, Orchestrate y Master](../images/levels.svg)

Elige el alcance que necesitas. Un trabajador es una conversación con un cliente; un rol añade un mandato, una tarea y un resultado;
un tablero programa ejecuciones de roles y registra sus resultados en las incidencias. El tablero puede ser Linear o Markdown local.

<a id="why"></a>
## Por qué trabajar en equipo

![Un agente, varios agentes coordinados a mano y equipos de ensemble coordinados por comandantes](../images/compare.svg)

Las ejecuciones independientes de agentes necesitan a alguien que distribuya el trabajo y siga sus respuestas. Un comandante usa Crew para iniciar,
inspeccionar y reanudar esas ejecuciones desde una sola conversación. Tú sigues decidiendo qué trabajo autorizar y revisar.

<a id="how-it-works"></a>
## Cómo funciona

![Límites entre capas: comandos de Manager, Orchestrator, Delegate, Session y Utils compartidos](../images/layers.svg)

![Límites de ejecución: arnés de sesión, guardas locales y de Docker, credenciales del broker y proxy de salida](../images/system.svg)

El control, la entrega de credenciales y las rutas de red son aspectos distintos. La [visión general de la arquitectura](../architecture.md)
explica las responsabilidades y la confianza; los detalles de implementación están en el código fuente enlazado.

<a id="install"></a>
## Instalación

En un Mac con Apple silicon, ejecuta el instalador de la versión fijada en el
[marketplace windroc](https://github.com/windroc-labs/agent-plugins-marketplace/blob/main/.claude-plugin/marketplace.json).
Sustituye `<tag>` por el `source.ref` de ensemble en ese archivo:

```sh
curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install.sh | bash
```

Para proporcionar la contraseña de `sudo` desde Keychain, descarga primero el script; stdin no puede transportar también el texto del script. Sustituye `<item>` por su servicio de Keychain:

```bash
(f=$(mktemp) && trap 'rm -f "$f"' EXIT && curl -fsSL -o "$f" https://raw.githubusercontent.com/windroc-labs/ensemble/<tag>/scripts/install.sh && security find-generic-password -s <item> -w | bash "$f" --password-stdin)
```

| Opción | Lee |
|---|---|
| `--password-stdin` | La contraseña de `sudo` desde stdin, antes de la clave del proveedor |

El instalador pregunta qué clientes usar, si los trabajadores se ejecutan localmente o en Docker y si quieres configurar un
tablero. Instala las dependencias y los plugins seleccionados, solicita los inicios de sesión necesarios y configura `ctmux`.
Vuelve a ejecutarlo para completar una instalación interrumpida. Para ver las opciones, usa `bash scripts/install.sh --help` en una copia del repositorio;
si ya tienes una instalación, lee primero [Actualización](../upgrading.md).

Usa la cuenta de modelos o el proveedor que requiera el cliente elegido. La configuración del proveedor y de la red de OpenCode se describe
en [Personalización de roles y tareas](../customizing-roles-and-tasks.md#opencode-sessions).
El `gh` del host necesita acceso para hacer push y gestionar PR en los repositorios de destino: los tokens clásicos necesitan `repo`, `read:org` y `gist`;
los tokens de permisos detallados también necesitan acceso de lectura a Actions para las comprobaciones.

### GitHub App

Los trabajadores de Docker usan una GitHub App en lugar del inicio de sesión de `gh` del host. El instalador no crea la App.
En GitHub Settings › Developer settings › GitHub Apps, crea una App privada que solo se pueda instalar en tu cuenta,
con los webhooks desactivados. Sus permisos de repositorio deben cubrir `github.permissions` en la
[configuración de Session](../../src/ensemble/session/config.toml). Instálala solo en los repositorios a los que los trabajadores puedan hacer push,
incluidos los repositorios de salida de documentos.

Genera una clave privada, anota el App ID e impórtalos:

```sh
/usr/local/libexec/ensemble/ensemble-broker login github ensemble-broker-bot \
  --key-file KEY.pem --app-id APP_ID
```

Elimina el PEM después de importarlo. [Rotación de credenciales](../upgrading.md#credential-rotation) explica la sustitución y la revocación.

<a id="quick-start"></a>
## Inicio rápido

Para una sola tarea, en una conversación del cliente instalado:

```text
/ensemble:act-as dummy-tester:echo hello
```

Para un equipo, abre un shell de comandante e inicia tu cliente:

```sh
ctmux work
claude
```

Usa `codex` u `opencode` en lugar de `claude` si ese es tu cliente. En esa conversación, ejecuta:

```text
/ensemble:manage
```

Describe el trabajo y el entregable deseado. El comandante coordina los trabajadores y las ejecuciones de roles mediante Crew.
Para trabajar con un tablero, `/ensemble:board` opera sobre las incidencias; la [personalización del flujo de trabajo](../customizing-roles-and-tasks.md#on-the-board)
explica cómo elegir las etapas.

<a id="docs"></a>
## Documentación

| Consulta lo que necesites… | Dónde |
|---|---|
| Las capas del sistema y los límites de ejecución y confianza | [Arquitectura](../architecture.md) |
| Modelos, proveedores, ajustes de roles, tus propias tareas o el flujo de trabajo | [Personalización de roles y tareas](../customizing-roles-and-tasks.md) |
| Cambios necesarios en una instalación existente | [Actualización](../upgrading.md) |
| Convenciones para colaboradores y puntos de entrada para las comprobaciones | [AGENTS.md](../../AGENTS.md) |
