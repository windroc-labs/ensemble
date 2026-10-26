# Worker image CI's base image (docs/docker-host.md#base-images): the default worker definition
# (src/broker/worker/Dockerfile) with agent at 1000:1000, no python3, and one in-image plugin.
ARG BASE=ci-worker:definition
FROM ${BASE}
USER root
# The worker definition's PATH has no sbin directories.
RUN export PATH=/usr/sbin:/sbin:$PATH \
    && if id vscode >/dev/null 2>&1; then userdel --remove vscode; fi \
    && ! getent passwd 1000 && ! getent group 1000 \
    && groupmod --gid 1000 agent && usermod --uid 1000 --gid 1000 agent \
    && chown -R agent:agent /home/agent \
    && apt-get purge -y --auto-remove python3-minimal \
    && rm -rf /var/lib/apt/lists/* /opt/uv /usr/local/bin/uv \
    && test ! -e /usr/bin/python3 \
    && install -d -m 0755 /opt/windroc/plugins/dummy-plugin/.claude-plugin /opt/windroc/plugins/dummy-plugin/commands \
    && printf '%s\n' '{"name": "dummy-plugin", "version": "0.0.1"}' > /opt/windroc/plugins/dummy-plugin/.claude-plugin/plugin.json \
    && printf '%s\n' 'Reply with the word hello.' > /opt/windroc/plugins/dummy-plugin/commands/hello.md
USER agent
