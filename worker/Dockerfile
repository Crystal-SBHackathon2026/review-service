# 빌드 컨텍스트는 레포 루트다: docker build -f worker/Dockerfile .
# review_ai 는 ./ai 를 일반 설치해 catalog/·knowledge/ 가 패키지 안(review_ai/_data/)에 들어간다.
# 검색기는 FileRetriever 라 [qdrant](fastembed·onnxruntime) 는 넣지 않는다 — Qdrant 를 쓰기로 하면 "./ai[qdrant]" 로 바꾼다.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY ai ./ai
COPY common ./common
COPY worker ./worker
RUN pip install ./ai ./common ./worker && rm -rf /build

RUN useradd --uid 10001 --no-create-home app
USER 10001
WORKDIR /app

CMD ["python", "-m", "review_worker.main"]
