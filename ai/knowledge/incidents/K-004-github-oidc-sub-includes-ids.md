---
doc_type: incident
provider: any
title: GitHub OIDC 토큰의 sub 에 owner·repo 숫자 ID 가 들어가 이름 기준 신뢰 조건이 거부된다
card_id: K-004
related_rules: []
observed: 2026-10-01 / github-actions → aws sts, azure entra / provision.yml 첫 실행
---
## 증상
같은 워크플로에서 GCP 만 성공하고 AWS 는 "Not authorized to perform sts:AssumeRoleWithWebIdentity", Azure 는 AADSTS700213 "No matching federated identity record" 로 실패한다.

## 원인
토큰의 sub 가 repo:<owner>/<repo>:ref:... 가 아니라 repo:<owner>@<owner_id>/<repo>@<repo_id>:ref:... 형식이다. sub 문자열을 그대로 비교하는 AWS 신뢰 정책·Azure 페더레이션 자격 증명은 옛 형식과 일치하지 않는다. GCP 는 sub 가 아니라 assertion.repository 로 조건을 걸어 영향을 받지 않았다.

## 고치는 법
gh api repos/<owner>/<repo> 로 id·owner.id 를 읽어 새 형식으로 subject 를 적는다. Azure 오류 메시지가 실제 토큰의 subject 를 그대로 보여 주므로 그 값을 쓰면 된다.

## 확인
provision.yml 재실행에서 3개 클라우드 모두 성공.
