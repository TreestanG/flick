# Where Claude Code writes gesture functions. The engine starts one container
# from this image when it starts and removes it when it quits. Nothing secret
# is built in: the token from `claude setup-token` is handed over per call.
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-numpy ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code

# Claude Code refuses to skip its permission prompts as root.
RUN mkdir /work && chown node:node /work
USER node
WORKDIR /work
ENV DISABLE_AUTOUPDATER=1
CMD ["sleep", "infinity"]
