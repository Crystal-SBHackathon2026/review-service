# 빌드 컨텍스트는 레포 루트다: docker build -f api/Dockerfile .
# review_ai 는 ./ai 를 일반 설치해 catalog/·knowledge/ 가 패키지 안(review_ai/_data/)에 들어간다.
# 검색기는 FileRetriever 라 [qdrant](fastembed·onnxruntime) 는 넣지 않는다 — Qdrant 를 쓰기로 하면 "./ai[qdrant]" 로 바꾼다.
# 코드 패치가 바꾼 package.json 의 package-lock.json 을 다시 만들 npm (review_api.lockfile — 스크립트 실행 없이 잠금 파일만)
FROM node:22-bookworm-slim AS node

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY ai ./ai
COPY common ./common
COPY api ./api
RUN pip install ./ai ./common ./api && rm -rf /build

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

RUN useradd --uid 10001 --no-create-home app
USER 10001
WORKDIR /app
EXPOSE 8080
CMD ["uvicorn", "review_api.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
