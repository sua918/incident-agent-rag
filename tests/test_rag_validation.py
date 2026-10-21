import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.rag import RunbookSearch,cosine
from agent.validation import validate_evidence


class FixedEmbeddings:
    model='test-fixed'
    def __init__(self): self.calls=0
    async def embed(self,texts):
        self.calls+=1
        return [[float('CPU' in text),float('메모리' in text),float('디스크' in text),1.] for text in texts]


class UnavailableEmbeddings:
    model='test-unavailable'
    async def embed(self,texts): raise ValueError('연결 실패')


class RagTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.books=self.root/'runbooks'
        shutil.copytree(Path(__file__).parents[1]/'data/runbooks',self.books)
        self.embedding=FixedEmbeddings()
        self.search=RunbookSearch(self.root/'test.db',self.books,self.embedding)
        self.environment=patch.dict(os.environ,{'RUNBOOK_SEARCH':'rag'})
        self.environment.start()

    async def asyncTearDown(self):
        self.environment.stop();self.temp.cleanup()

    async def test_index_is_reused_and_rebuilt_when_changed(self):
        first=await self.search.build_index()
        self.assertEqual(first['chunks'],28)
        self.assertEqual((await self.search.build_index())['updated'],0)
        p=self.books/'cpu-spike.md';p.write_text(p.read_text(encoding='utf-8')+'\n6. CPU 기록을 다시 확인한다.\n',encoding='utf-8')
        self.assertEqual((await self.search.build_index())['updated'],1)
        self.embedding.model='test-new-model'
        self.assertEqual((await self.search.build_index())['updated'],29)

    async def test_search_returns_three_real_chunks(self):
        result=await self.search.search('CPU 급증',[])
        self.assertEqual(result['mode'],'rag')
        self.assertEqual(len(result['hits']),3)
        for hit in result['hits']:
            self.assertIn(hit['text'],(self.books/(hit['incident_type']+'.md')).read_text(encoding='utf-8'))
            self.assertNotIn('embedding',hit)

    async def test_outage_is_explicit_and_uses_rules(self):
        self.search.embedder=UnavailableEmbeddings()
        result=await self.search.search('원인 확인',[{'status':'degraded','alerts':['CPU 91%']}])
        self.assertEqual(result['mode'],'rules_fallback')
        self.assertEqual(result['hits'][0]['incident_type'],'cpu-spike')
        self.assertTrue(result['fallback_reason'])

    async def test_no_matching_rule_does_not_invent_db_down(self):
        self.search.embedder=UnavailableEmbeddings()
        result=await self.search.search('안내',[{'status':'healthy','alerts':[]}])
        self.assertEqual(result['hits'],[])

    async def test_invalid_dimensions_are_rejected(self):
        self.assertAlmostEqual(cosine([1,0],[1,0]),1)
        with self.assertRaises(ValueError):cosine([1],[1,0])


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.facts=[{'system_name':'auth-service','status':'degraded','cpu_percent':78.2,'memory_percent':61.5},
                    {'system_name':'web-01','status':'healthy','cpu_percent':34.1,'memory_percent':48.0}]
        self.sources=[{'id':'cpu-spike:3'}]

    def check(self,text):return validate_evidence(text,self.facts,self.sources)

    def test_correct_per_system_values_pass(self):
        self.assertEqual(self.check('auth-service 상태=degraded, CPU 78.2%, 메모리 61.5% · web-01 상태=healthy, CPU 34.1%, 메모리 48.0%'),[])

    def test_wrong_and_swapped_numbers_fail(self):
        self.assertTrue(self.check('auth-service CPU 99%입니다.'))
        self.assertTrue(self.check('auth-service CPU 34.1%입니다.'))
        self.assertTrue(self.check('web-01 메모리 61.5%입니다.'))

    def test_wrong_state_and_missing_source_fail(self):
        self.assertTrue(self.check('auth-service 상태=healthy'))
        self.assertTrue(self.check('확인한 매뉴얼 [missing:1]을 참고합니다.'))
        self.assertFalse(self.check('참고 매뉴얼 [cpu-spike:3]'))

    def test_korean_state_particles_are_checked(self):
        self.assertTrue(self.check('auth-service 상태가 healthy로 확인되었습니다.'))
        self.assertEqual(self.check('auth-service 상태는 degraded입니다.'),[])

    def test_threshold_condition_is_not_a_measurement(self):
        self.assertEqual(self.check('auth-service CPU가 90%를 넘으면 부하 분산을 검토하세요.'),[])
