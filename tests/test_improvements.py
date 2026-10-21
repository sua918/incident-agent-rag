import asyncio
import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

os.environ['LLM_BACKEND']='fake'
os.environ['SUPERVISOR_VERSION']='v2'
os.environ['LANGCHAIN_TRACING_V2']='false'
os.environ['LANGSMITH_TRACING']='false'

from fastapi.testclient import TestClient
import server
from agent.graph import validate_node, _after_validate
from agent.session_store import SqliteSessionStore

class BadWriter:
    def __init__(self): self.calls=0
    async def write(self,*args,**kwargs):
        self.calls+=1
        return '확인 불가', {'usd':0}

class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_third_attempt_stays_failed(self):
        state={'draft':'짧음','validation_attempts':2,'collected':[{'source':'monitoring','data':{}}],'events':[]}
        result=await validate_node(state,None)
        self.assertFalse(result['validation_passed'])
        self.assertEqual(result['validation_attempts'],3)
        self.assertEqual(result['events'][-1]['verdict'],'확인 필요')
        self.assertEqual(_after_validate({**state,**result}),'finalize')

    async def test_good_answer_passes(self):
        state={'draft':'1) 모니터링 지표와 알림을 확인하고 원인에 맞는 대응 매뉴얼을 검토하세요.','collected':[{'source':'monitoring','data':{}}],'events':[]}
        result=await validate_node(state,None)
        self.assertTrue(result['validation_passed'])

    async def test_numbering_failure_has_specific_feedback(self):
        state={'draft':'모니터링 지표와 알림을 먼저 확인하고 해당 시스템의 대응 매뉴얼에 따라 조치하세요.','collected':[{'source':'monitoring','data':{}}],'events':[]}
        result=await validate_node(state,None)
        self.assertFalse(result['validation_passed'])
        self.assertIn('번호',result['validation_feedback'])

    async def test_missing_data_ends_without_rewriting(self):
        state={'draft':'','collected':[],'events':[]}
        result=await validate_node(state,None)
        self.assertFalse(result['validation_passed'])
        self.assertEqual(_after_validate({**state,**result}),'finalize')

class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.old_db=os.environ.get('SESSION_DATABASE')
        os.environ['SESSION_DATABASE']=str(Path(cls.temp.name)/'test.db')
        cls.context=TestClient(server.app)
        cls.client=cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None,None,None)
        if cls.old_db is None: os.environ.pop('SESSION_DATABASE',None)
        else: os.environ['SESSION_DATABASE']=cls.old_db
        cls.temp.cleanup()

    def chat(self,message='auth-service 장애 진단해줘',thread='test-incident'):
        response=self.client.post('/chat',json={'message':message,'thread_id':thread})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_normal_diagnosis_saved_and_searchable(self):
        response=self.chat(thread='normal')
        self.assertEqual(response['metadata']['status'],'completed')
        self.assertTrue(response['metadata']['validation_passed'])
        self.assertEqual(response['metadata']['validation_attempts'],2)
        diagnosis=response['metadata']['diagnosis_id']
        detail=self.client.get('/diagnoses/'+diagnosis).json()
        self.assertEqual(detail['reply'],response['reply'])
        self.assertEqual(detail['query'],'auth-service 장애 진단해줘')
        self.assertTrue(any(row['id']==diagnosis for row in self.client.get('/diagnoses?q=auth-service&status=completed').json()))
        self.assertEqual(self.client.get('/diagnoses?q=NO-SUCH-QUERY').json(),[])

    def test_greeting_keeps_direct_route_and_no_diagnosis(self):
        result=self.chat('안녕하세요','greeting')
        self.assertEqual(result['metadata']['route'],'chat')
        self.assertIsNone(result['metadata']['diagnosis_id'])

    def test_partial_failure_keeps_success_and_marks_review(self):
        mcp=server.app.state.deps.mcp
        original=mcp.call_tool
        async def partial(name,args,server='ops'):
            if args.get('system_name')=='auth-service': raise RuntimeError('offline')
            return await original(name,args,server)
        mcp.call_tool=partial
        try: result=self.chat(thread='partial')
        finally: mcp.call_tool=original
        self.assertEqual(result['metadata']['status'],'needs_review')
        self.assertIn('일부 자료',result['reply'])
        self.assertIn('web-01',result['reply'])
        self.assertEqual(result['metadata']['failed_sources'][0]['target'],'auth-service')
        self.assertTrue(any(e['type']=='TaskCompleted' and e['target']=='web-01' for e in result['events']))
        detail=self.client.get('/diagnoses/'+result['metadata']['diagnosis_id']).json()
        self.assertEqual(detail['status'],'needs_review')

    def test_all_failed_does_not_ask_writer_to_invent_answer(self):
        deps=server.app.state.deps
        original=deps.mcp.call_tool
        writer=deps.quality_llm
        bad=BadWriter()
        async def offline(*args,**kwargs): raise RuntimeError('offline')
        deps.mcp.call_tool=offline;deps.quality_llm=bad
        try: result=self.chat(thread='all-failed')
        finally: deps.mcp.call_tool=original;deps.quality_llm=writer
        self.assertEqual(bad.calls,0)
        self.assertEqual(result['metadata']['status'],'needs_review')
        self.assertIn('진단 자료를 조회하지 못해',result['reply'])
        self.assertFalse(result['metadata']['validation_passed'])

    def test_three_invalid_drafts_never_become_final_reply(self):
        deps=server.app.state.deps;writer=deps.quality_llm;bad=BadWriter();deps.quality_llm=bad
        try: result=self.chat(thread='bad-writer')
        finally: deps.quality_llm=writer
        self.assertEqual(bad.calls,3)
        self.assertFalse(result['metadata']['validation_passed'])
        self.assertEqual(result['metadata']['status'],'needs_review')
        self.assertNotIn('확인 불가',result['reply'])
        self.assertNotIn('합격(최대',json.dumps(result,ensure_ascii=False))

    def test_failed_runbook_is_recorded_with_monitoring_preserved(self):
        mcp=server.app.state.deps.mcp;original=mcp.read_resource
        async def offline(*args,**kwargs): raise RuntimeError('unavailable')
        mcp.read_resource=offline
        try: result=self.chat(thread='runbook-failed')
        finally: mcp.read_resource=original
        self.assertEqual(result['metadata']['status'],'needs_review')
        self.assertEqual(result['metadata']['failed_sources'][0]['source'],'runbook')
        self.assertIn('auth-service',result['reply'])

    def test_stream_saves_history_and_excludes_raw_draft_and_prompt(self):
        with self.client.stream('GET','/chat/stream',params={'message':'auth-service 장애 진단해줘','thread_id':'stream'}) as response:
            body=''.join(response.iter_text())
        self.assertIn('data: [DONE]',body)
        self.assertNotIn('재시작하면 됩니다.',body)
        self.assertNotIn('"prompt"',body)
        events=[json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: {')]
        answer=next(e for e in events if e['type']=='FinalAnswer')
        self.assertEqual(answer['status'],'completed')
        detail=self.client.get('/diagnoses/'+answer['diagnosis_id']).json()
        self.assertEqual(detail['reply'],answer['reply'])

    def test_unknown_system_is_a_failed_lookup_not_a_normal_result(self):
        result=self.chat('missing-service 장애 진단해줘',thread='unknown-system')
        self.assertEqual(result['metadata']['status'],'needs_review')
        self.assertEqual(result['metadata']['failed_sources'][0]['target'],'missing-service')
        self.assertNotIn('DB Down',result['reply'])
        self.assertIn('web-01',result['reply'])

    def test_history_invalid_filter_and_missing_id(self):
        self.assertEqual(self.client.get('/diagnoses?status=unknown').status_code,422)
        self.assertEqual(self.client.get('/diagnoses/missing').status_code,404)

    def test_z_history_survives_server_restart(self):
        result=self.chat(thread='restart')
        diagnosis=result['metadata']['diagnosis_id']
        self.context.__exit__(None,None,None)
        type(self).context=TestClient(server.app)
        type(self).client=type(self).context.__enter__()
        detail=self.client.get('/diagnoses/'+diagnosis).json()
        self.assertEqual(detail['reply'],result['reply'])
        self.assertEqual(detail['status'],'completed')
        self.assertEqual(len(server.app.state.deps.session_store.load('restart')),2)

if __name__=='__main__': unittest.main()
