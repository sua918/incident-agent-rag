"""고정 질문으로 원본 규칙과 의미 검색을 비교한다."""
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from agent.rag import RunbookSearch,keyword_type
from agent.validation import validate_evidence


async def run():
    cases=json.loads((ROOT/'eval/cases.json').read_text(encoding='utf-8'))
    rows=[]
    with tempfile.TemporaryDirectory(prefix='incident-eval-') as folder:
        search=RunbookSearch(Path(folder)/'index.db')
        started=time.perf_counter()
        index=await search.build_index()
        index_ms=round((time.perf_counter()-started)*1000,2)
        for case in cases:
            started=time.perf_counter()
            baseline=keyword_type(case['observations']) or 'db-down'
            baseline_ms=(time.perf_counter()-started)*1000
            started=time.perf_counter()
            result=await search.search(case['query'],case['observations'])
            elapsed=(time.perf_counter()-started)*1000
            if result['mode']!='rag':raise RuntimeError('실제 임베딩 검색을 확인하지 못했습니다.')
            documents=[hit['incident_type'] for hit in result['hits']]
            rows.append({'id':case['id'],'condition':case['condition'],'expected':case['expected'],
                         'original':baseline,'rag_documents':documents,'original_hit':baseline==case['expected'],
                         'rag_top1':bool(documents) and documents[0]==case['expected'],'rag_top3':case['expected'] in documents,
                         'original_ms':round(baseline_ms,4),'rag_ms':round(elapsed,2)})
    def summary(items):
        return {'count':len(items),'original_hits':sum(r['original_hit'] for r in items),
                'rag_top1_hits':sum(r['rag_top1'] for r in items),'rag_top3_hits':sum(r['rag_top3'] for r in items),
                'original_median_ms':round(statistics.median(r['original_ms'] for r in items),4),
                'rag_median_ms':round(statistics.median(r['rag_ms'] for r in items),2)}
    facts=[{'system_name':'auth-service','status':'degraded','cpu_percent':78.2,'memory_percent':61.5}]
    bodies=[('correct','1) auth-service 상태=degraded, CPU 78.2%, 메모리 61.5%로 조회되었습니다. 이후 대응 절차를 확인하세요.'),
            ('wrong_cpu','1) auth-service 상태=degraded, CPU 99%, 메모리 61.5%로 조회되었습니다. 이후 대응 절차를 확인하세요.'),
            ('wrong_memory','1) auth-service 상태=degraded, CPU 78.2%, 메모리 10%로 조회되었습니다. 이후 대응 절차를 확인하세요.'),
            ('wrong_status','1) auth-service 상태=healthy, CPU 78.2%, 메모리 61.5%로 조회되었습니다. 이후 대응 절차를 확인하세요.'),
            ('wrong_system','1) billing-service 상태=healthy, CPU 78.2%, 메모리 61.5%로 조회되었습니다. 이후 대응 절차를 확인하세요.'),
            ('wrong_source','1) auth-service 상태=degraded, CPU 78.2%로 조회되었습니다. [missing:1] 문서에 따라 대응 절차를 확인하세요.')]
    checks=[{'case':name,'original_passed':len(body)>=30 and '1)' in body,'improved_passed':not validate_evidence(body,facts,[])} for name,body in bodies]
    result={'model':index['model'],'chunks':index['chunks'],'initial_index_ms':index_ms,'overall':summary(rows),
            'by_condition':{key:summary([r for r in rows if r['condition']==key]) for key in ['alerts','question_only']},
            'rows':rows,'validation_cases':checks,
            'limitations':['검색 결과만 비교하며 답변 정확도나 전체 상담 시간은 측정하지 않는다.','6종 매뉴얼에 질문 6개를 두 조건으로 반복한 소규모 자료다.','임베딩 최초 생성 시간은 검색 지연과 따로 기록한다.','검증 비교는 작성한 오류 사례에 대한 탐지 결과다.']}
    output=ROOT/'doc/results';output.mkdir(parents=True,exist_ok=True)
    (output/'comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ['model','chunks','initial_index_ms','overall','by_condition','validation_cases']},ensure_ascii=False),flush=True)


if __name__=='__main__':asyncio.run(run())
