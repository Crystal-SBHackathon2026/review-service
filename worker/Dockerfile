# 빌드 컨텍스트는 레포 루트다: docker build -f worker/Dockerfile .
# review_ai 는 ./ai 를 일반 설치해 catalog/·knowledge/ 가 패키지 안(review_ai/_data/)에 들어간다.
# [qdrant](qdrant-client·fastembed) 를 넣는다 — QDRANT_URL 이 있으면 의미 검색을 덧붙이고, 같은 이미지가
# Qdrant 파드의 색인 컨테이너(python -m review_ai.retrieval.indexer)로도 돈다.
# 임베딩 모델은 빌드 때 내려받아 이미지에 넣는다 — 파드가 뜰 때 Hugging Face 를 부르지 않는다(HF_HUB_OFFLINE).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY ai ./ai
COPY common ./common
COPY worker ./worker
RUN pip install "./ai[qdrant]" ./common ./worker && rm -rf /build

ENV FASTEMBED_CACHE_PATH=/opt/fastembed
RUN python -c "from review_ai.retrieval.embedding import FastEmbedder; FastEmbedder()" \
    && chmod -R a+rX /opt/fastembed
ENV HF_HUB_OFFLINE=1

RUN useradd --uid 10001 --no-create-home app
USER 10001
WORKDIR /app

CMD ["python", "-m", "review_worker.main"]
