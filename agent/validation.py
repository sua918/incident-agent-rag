"""모델이 적은 관측값을 도구 조회 결과와 대조한다."""
import re

SYSTEM = r'[a-z][a-z0-9-]*-(?:service|\d+)'


def validate_evidence(draft, observations, sources, query=''):
    issues = []
    facts = {d['system_name']:d for d in observations if 'system_name' in d}
    # 질문을 인용한 문장은 관측 사실로 취급하지 않는다.
    body = draft.replace("'"+query+"'",'') if query else draft
    for match in re.finditer(r'\[([a-z0-9-]+:\d+)\]',body):
        if match[1] not in {s['id'] for s in sources}:
            issues.append(f'조회하지 않은 출처입니다: {match[1]}')
    for clause in re.split(r'[\n·;]|(?=\d\))',body):
        for match in re.finditer(r'(CPU|메모리)\s*(?:사용률\s*)?[은는가:=]*\s*(\d+(?:\.\d+)?)\s*%',clause,re.I):
            following = clause[match.end():match.end()+35]
            # 매뉴얼의 임계 조건과 현재 측정값을 구분한다.
            if re.match(r'\s*(?:를|을|이|가)?\s*(?:넘|이상|초과|미만|이하|기준|임계|일\s*때|이면)',following):
                continue
            names = list(re.finditer(SYSTEM,clause[:match.start()]))
            name = names[-1][0] if names else next(iter(facts)) if len(facts)==1 else None
            if name not in facts:
                issues.append(f'{match[1]} 수치의 대상 시스템을 확인할 수 없습니다.')
                continue
            field = 'cpu_percent' if match[1].lower()=='cpu' else 'memory_percent'
            if abs(float(match[2])-float(facts[name][field])) > .05:
                issues.append(f'{name}의 {match[1]}는 조회값 {facts[name][field]}%와 다릅니다.')
        pattern = '('+SYSTEM+r')\s*(?:의\s*)?상태\s*[은는가]?\s*[=:]?\s*(healthy|degraded|down|정상|저하|다운)'
        for match in re.finditer(pattern,clause,re.I):
            expected = facts.get(match[1])
            actual = {'정상':'healthy','저하':'degraded','다운':'down'}.get(match[2],match[2].lower())
            if not expected or expected['status'] != actual:
                issues.append(f'{match[1]}의 상태가 조회 결과와 다릅니다.')
    return list(dict.fromkeys(issues))
