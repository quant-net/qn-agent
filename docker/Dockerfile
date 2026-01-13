FROM python:3.11-slim AS build

RUN apt-get update && \
    apt-get install --yes --no-install-recommends \
    openssh-client \ 
    git \
    && apt-get clean

# Update pip
RUN pip install --upgrade pip setuptools wheel

# Declare ARG without default - REQUIRED even without default value
ARG BRANCH

RUN echo "Dockerfile BRANCH value is: ${BRANCH}"

RUN git clone --single-branch --branch ${BRANCH} https://github.com/quant-net/qn-mq qn-mq && \
    cd qn-mq && pip install --no-cache-dir .

RUN git clone --single-branch --branch ${BRANCH} https://github.com/quant-net/qn-plugins qn-plugins

RUN  git clone --single-branch --branch ${BRANCH} https://github.com/quant-net/qn-agent qn-agent && \
    cd qn-agent && pip install --no-cache-dir .


COPY docker/docker-entrypoint.sh /
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["quantnet_agent"]
