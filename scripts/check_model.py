"""실제 Ollama 연결을 임시 DB에서 한 건 확인한다."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['LLM_BACKEND']='ollama'
os.environ['LANGSMITH_TRACING']='false'
os.environ['LANGCHAIN_TRACING_V2']='false'
os.environ.pop('RUNBOOK_SEARCH',None)
from fastapi.testclient import TestClient

with tempfile.TemporaryDirectory(prefix='incident-model-') as folder:
    os.environ['SESSION_DATABASE']=str(Path(folder)/'model.db')
    import server
    started=time.perf_counter()
    with TestClient(server.app) as client:
        response=client.post('/chat',json={'message':'auth-service 로그인 응답 지연 장애를 진단해줘','thread_id':'model-check'})
        response.raise_for_status()
        value=response.json()
        metadata=value['metadata']
        saved=client.get('/diagnoses/'+metadata['diagnosis_id']).json()
        result={'backend':'ollama','search_mode':metadata['search_mode'],'elapsed_seconds':round(time.perf_counter()-started,2),
                'status':metadata['status'],'validation_passed':metadata['validation_passed'],
                'validation_attempts':metadata['validation_attempts'],'issues':metadata['validation_issues'],
                'source_count':len(metadata['sources']),'history_keeps_sources':saved.get('sources')==metadata['sources'],
                'reply':value['reply'],'openai':'키 미설정으로 실제 호출 미검증'}
    out=ROOT/'doc/results';out.mkdir(parents=True,exist_ok=True)
    (out/'model-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)
