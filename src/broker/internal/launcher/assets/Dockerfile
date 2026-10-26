ARG USER_IMAGE
FROM ${USER_IMAGE}
USER root
ARG AGENT_UID
ARG AGENT_GID
RUN test "$(id -u agent):$(id -g agent)" = "${AGENT_UID}:${AGENT_GID}"
ARG GH_VERSION=2.102.0
# ripgrep: OpenCode downloads an unchecked rg when none is on PATH.
# /usr/local/bin precedes /usr/bin on PATH: the gh wrapper runs this gh, not a gh the user image installs.
RUN apt-get update && apt-get install -y --no-install-recommends tini python3 python3-venv curl ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && case "$(dpkg --print-architecture)" in \
         amd64) arch=amd64 sum=bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386 ;; \
         arm64) arch=arm64 sum=7862c86c72f43df3a2d93ddde6f473285b4e2af61b494849846827e513ef6484 ;; \
         *) exit 1 ;; \
       esac \
    && name="gh_${GH_VERSION}_linux_${arch}" \
    && curl -fsSL -o "/tmp/${name}.tar.gz" "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${name}.tar.gz" \
    && echo "${sum}  /tmp/${name}.tar.gz" | sha256sum -c - \
    && tar -xzf "/tmp/${name}.tar.gz" -C /usr/local/bin --strip-components=2 "${name}/bin/gh" \
    && rm "/tmp/${name}.tar.gz" \
    && chown root:root /usr/local/bin/gh && chmod 755 /usr/local/bin/gh
COPY src/ensemble/session /opt/ensemble/src/ensemble/session
COPY src/ensemble/utils /opt/ensemble/src/ensemble/utils
COPY src/ensemble/delegate /opt/ensemble/src/ensemble/delegate
COPY plugin-sources.json /opt/ensemble/plugin-sources.json
COPY .claude-plugin/plugin.json /opt/ensemble/version.json
COPY bootstrap.py codex-probe.py opencode-install.py plugin-install.py hook-stub with-secrets workspace-check gh git-credential-ensemble /opt/ensemble/lib/
COPY claude.json /opt/ensemble/templates/.claude.json
RUN npm install --global --ignore-scripts --prefix /opt/ensemble/clients @openai/codex@0.163.0 \
    && python3 -I /opt/ensemble/lib/opencode-install.py "$(dpkg --print-architecture)" /opt/ensemble/clients \
    && PATH=/opt/ensemble/clients/bin:/home/agent/.npm-global/bin:/usr/local/bin:/usr/bin:/bin python3 -I /opt/ensemble/lib/plugin-install.py /opt/ensemble/plugin-sources.json /opt/ensemble \
    && python3 -I /opt/ensemble/lib/codex-probe.py /opt/ensemble/clients/bin/codex 0.163.0
# python3 on the agent's PATH is this venv's, importing ensemble; system site keeps pip --user working.
RUN python3 -m venv --system-site-packages /opt/ensemble/venv && mkdir -p /opt/ensemble/bin \
    && printf '%s\n' /opt/ensemble/src > "$(/opt/ensemble/venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/ensemble.pth" \
    && printf '%s\n' '#!/bin/sh' 'exec /opt/ensemble/venv/bin/python "$@"' > /opt/ensemble/bin/python3 \
    && ln -s /opt/ensemble/src/ensemble/delegate/team /opt/ensemble/team \
    && printf '%s\n' '#!/bin/sh' 'export PYTHONPATH=/opt/ensemble/src' 'exec /opt/ensemble/venv/bin/python -P -m ensemble.session.cli "$@"' > /opt/ensemble/bin/ensemble-session \
    && printf '%s\n' '#!/bin/sh' 'exec /opt/ensemble/venv/bin/python -I /opt/ensemble/src/ensemble/session/hosts/docker/relay.py "$@"' > /opt/ensemble/bin/relay \
    && ln -s /opt/ensemble/lib/with-secrets /opt/ensemble/bin/with-secrets \
    && ln -s /opt/ensemble/clients/bin/codex /opt/ensemble/bin/codex \
    && ln -s /opt/ensemble/clients/bin/opencode /opt/ensemble/bin/opencode \
    && ln -s /opt/ensemble/lib/gh /opt/ensemble/bin/gh \
    && ln -s /opt/ensemble/lib/git-credential-ensemble /opt/ensemble/bin/git-credential-ensemble \
    && printf '%s\n' '[credential]' 'helper = /opt/ensemble/bin/git-credential-ensemble' 'useHttpPath = true' >> /etc/gitconfig \
    && printf '%s\n' '[core]' 'hooksPath = .githooks' >> /etc/gitconfig \
    && printf '%s\n' 'PATH=/opt/ensemble/bin:$PATH' > /etc/profile.d/ensemble.sh \
    && chmod 755 /opt/ensemble/bin/ensemble-session /opt/ensemble/bin/relay /opt/ensemble/bin/python3 /opt/ensemble/lib/with-secrets /opt/ensemble/lib/hook-stub /opt/ensemble/lib/workspace-check /opt/ensemble/lib/gh /opt/ensemble/lib/git-credential-ensemble \
    && chmod -R a+rX /opt/ensemble/codex-plugins \
    && chmod -R a+rX /opt/ensemble/claude-plugins \
    && chmod -R a+rX /opt/ensemble/plugins \
    && chown -R root:root /opt/ensemble && chmod -R go-w /opt/ensemble \
    && install -d -o agent -g agent -m 0700 /run/ensemble /run/ensemble/relay-queue
ARG ENSEMBLE_VERSION
LABEL ensemble.version=${ENSEMBLE_VERSION} ensemble.agent-uid=${AGENT_UID} ensemble.agent-gid=${AGENT_GID}
ENV PATH=/opt/ensemble/bin:/home/agent/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
# Claude Code loads each plugin here as <name>@inline, keeping its /<name>: commands.
ARG ENSEMBLE_PLUGIN_DIRS
ENV CLAUDE_CODE_PLUGIN_DIRS=${ENSEMBLE_PLUGIN_DIRS}
USER agent
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
