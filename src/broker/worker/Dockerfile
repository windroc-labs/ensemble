FROM mcr.microsoft.com/devcontainers/base:bookworm@sha256:9a5942e64b35c5497c591b9bf177f4a3002d43440bf0a5e1a14fc34667b1a5c1
USER root
ARG NODE_VERSION=24.22.0
RUN apt-get update && apt-get install -y --no-install-recommends tmux git gh python3 python3-venv curl ca-certificates xz-utils \
    && python3 -m venv /opt/uv && /opt/uv/bin/pip install --no-cache-dir uv==0.13.1 \
    && ln -s /opt/uv/bin/uv /usr/local/bin/uv \
    && case "$(dpkg --print-architecture)" in \
         arm64) arch=arm64 sum=fef00bb4d36e1c50104a9775a10359beee98f162b4104dd12472c75065c6a143 ;; \
         amd64) arch=x64 sum=cc2f601cee82d9a5a233302fe307abc600727035fd7d2c3a3571755989e95008 ;; \
         *) exit 1 ;; \
       esac \
    && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz" \
    && echo "${sum}  node-v${NODE_VERSION}-linux-${arch}.tar.xz" | sha256sum -c - \
    && tar -xJf "node-v${NODE_VERSION}-linux-${arch}.tar.xz" -C /usr/local --strip-components=1 \
    && rm "node-v${NODE_VERSION}-linux-${arch}.tar.xz" \
    && useradd --create-home --shell /bin/bash agent \
    && SUDO_FORCE_REMOVE=yes apt-get purge -y sudo && rm -rf /etc/sudoers.d /var/lib/apt/lists/*
USER agent
ENV NPM_CONFIG_PREFIX=/home/agent/.npm-global
ENV PATH=/home/agent/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
WORKDIR /home/agent
