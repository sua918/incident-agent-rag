"""작은 매뉴얼 집합을 SQLite에 저장하고 의미가 가까운 문단을 찾는다."""
import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def cosine(a, b):
    if not a or len(a) != len(b):
        raise ValueError("임베딩 차원이 다릅니다.")
    scale = math.sqrt(sum(x*x for x in a)) * math.sqrt(sum(x*x for x in b))
    return sum(x*y for x,y in zip(a,b)) / scale if scale else 0.0


def keyword_type(observations):
    text = " ".join(str(d.get('status','')) + ' ' + ' '.join(d.get('alerts',[])) for d in observations).lower()
    rules = [('latency','high-latency'),('pool','high-latency'),('cpu','cpu-spike'),('memory','memory-leak'),('heap','memory-leak'),('timeout','network-timeout'),('network','network-timeout'),('disk','disk-full'),('down','db-down')]
    return next((kind for word,kind in rules if word in text), None)


class OllamaEmbeddings:
    def __init__(self):
        self.model = os.getenv('EMBEDDING_MODEL','qwen3-embedding:0.6b')
        self.url = os.getenv('OLLAMA_BASE_URL','http://127.0.0.1:11434').rstrip('/') + '/api/embed'

    async def embed(self, texts):
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(self.url,json={'model':self.model,'input':texts,'truncate':False})
            response.raise_for_status()
            vectors = response.json()['embeddings']
        if len(vectors) != len(texts) or any(not v or any(not math.isfinite(x) for x in v) for v in vectors):
            raise ValueError('올바른 임베딩을 받지 못했습니다.')
        return vectors


class RunbookSearch:
    def __init__(self, database=None, directory=None, embedder=None):
        self.database = Path(database or os.getenv('SESSION_DATABASE',ROOT/'session.db'))
        self.directory = Path(directory or ROOT/'data/runbooks')
        self.database.parent.mkdir(parents=True,exist_ok=True)
        self.embedder = embedder or OllamaEmbeddings()
        self.lock = asyncio.Lock()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS runbook_chunks(id TEXT PRIMARY KEY,incident_type TEXT,title TEXT,text TEXT,content_hash TEXT,embedding TEXT,model TEXT)')

    def chunks(self):
        chunks = []
        for path in sorted(self.directory.glob('*.md')):
            lines = path.read_text(encoding='utf-8').strip().splitlines()
            title = lines[0].lstrip('# ')
            # 번호별 절차를 한 문단으로 둬서 검색된 근거를 그대로 보여준다.
            paragraphs = [line.strip() for line in lines[1:] if line.strip()]
            for i,text in enumerate(paragraphs,1):
                chunks.append({'id':f'{path.stem}:{i}','incident_type':path.stem,'title':title,'text':text,'content_hash':hashlib.sha256((title+'\n'+text).encode()).hexdigest()})
        return chunks

    async def build_index(self):
        async with self.lock:
            chunks = self.chunks()
            with closing(sqlite3.connect(self.database)) as db:
                saved = {row[0]:(row[1],row[2]) for row in db.execute('SELECT id,content_hash,model FROM runbook_chunks')}
            pending = [c for c in chunks if saved.get(c['id']) != (c['content_hash'],self.embedder.model)]
            generated = []
            for start in range(0,len(pending),8):
                batch = pending[start:start+8]
                vectors = await self.embedder.embed([c['title']+'\n'+c['text'] for c in batch])
                generated.extend(zip(batch,vectors))
            # 임베딩 생성이 끝난 뒤 한 번에 반영해 불완전한 인덱스를 남기지 않는다.
            with closing(sqlite3.connect(self.database,timeout=15)) as db, db:
                for c,vector in generated:
                    db.execute('INSERT OR REPLACE INTO runbook_chunks VALUES(?,?,?,?,?,?,?)',(c['id'],c['incident_type'],c['title'],c['text'],c['content_hash'],json.dumps(vector),self.embedder.model))
                valid = {c['id'] for c in chunks}
                for (chunk_id,) in db.execute('SELECT id FROM runbook_chunks').fetchall():
                    if chunk_id not in valid: db.execute('DELETE FROM runbook_chunks WHERE id=?',(chunk_id,))
            return {'chunks':len(chunks),'updated':len(pending),'model':self.embedder.model}

    def rule_search(self, observations):
        kind = keyword_type(observations)
        return [dict(c,score=None) for c in self.chunks() if c['incident_type']==kind][:3]

    async def search(self, query, observations):
        if os.getenv('RUNBOOK_SEARCH') == 'rules':
            return {'mode':'rules','hits':self.rule_search(observations),'fallback_reason':None}
        try:
            await self.build_index()
            symptom = query + '\n' + '\n'.join(' '.join(d.get('alerts',[])) for d in observations)
            vector = (await self.embedder.embed([symptom]))[0]
            with closing(sqlite3.connect(self.database)) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute('SELECT * FROM runbook_chunks WHERE model=?',(self.embedder.model,)).fetchall()
            hits = [{k:row[k] for k in ['id','incident_type','title','text','content_hash']} | {'score':round(cosine(vector,json.loads(row['embedding'])),5)} for row in rows]
            hits.sort(key=lambda hit:hit['score'],reverse=True)
            return {'mode':'rag','hits':hits[:3],'fallback_reason':None}
        except (httpx.HTTPError,ValueError,KeyError,TypeError) as exc:
            return {'mode':'rules_fallback','hits':self.rule_search(observations),'fallback_reason':type(exc).__name__}


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    print(json.dumps(asyncio.run(RunbookSearch().build_index()),ensure_ascii=False))
