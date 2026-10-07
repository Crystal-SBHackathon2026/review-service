"""노드 에러 종류 — 제안서 4절.

- TransientError: 타임아웃·5xx·429·연결 실패. 파이프라인의 노드 RetryPolicy 가 재시도한다.
- 도메인 결과(근거 부족, 인용 검증 실패, 모델 거절)는 예외가 아니라 정상 반환 + 사유 코드다.
- 그 밖의 예외는 버그 — 그대로 올라가 파이프라인이 DLQ·status=error 로 처리한다.
"""


class TransientError(RuntimeError):
    pass
