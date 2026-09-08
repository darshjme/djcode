"""Regression checks through real provider serialization and actual local tools."""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from click.testing import CliRunner

from djcode.cli import main
from djcode.provider import Provider, ProviderConfig, Message
from djcode.agents.operator import Operator, ThinkingStreamProcessor
from djcode.streaming import stream_turn
from djcode.tools.bash import execute_bash
from djcode.tools.git import execute_git


def event(delta=None, finish=None):
    return {"choices": [{"delta": delta or {}, "finish_reason": finish}]}


def sse(events):
    return '\n\n'.join('data: '+json.dumps(e) for e in events)+'\n\ndata: [DONE]\n\n'


def test_actual_http_tool_loop_writes_edits_and_runs_test(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    requests = []
    def handler(request):
        body = json.loads(request.content); requests.append(body)
        turn = len(requests)
        if turn == 1:
            fn = {'name':'file_write','arguments':json.dumps({'path':str(tmp_path/'sum.py'),'content':'assert 2 + 2 == 5\n'})}
        elif turn == 2:
            assert body['messages'][-1]['tool_call_id'] == 'call_1'
            fn = {'name':'file_edit','arguments':json.dumps({'path':str(tmp_path/'sum.py'),'old_string':'== 5','new_string':'== 4'})}
        elif turn == 3:
            fn = {'name':'bash','arguments':json.dumps({'command':f'{os.sys.executable} sum.py'})}
        else:
            assert body['messages'][-1]['content'] == '(no output)'
            return httpx.Response(200,text=sse([event({'content':'Verified arithmetic.'}),event(finish='stop')]))
        return httpx.Response(200,text=sse([event({'tool_calls':[{'index':0,'id':f'call_{turn}','function':fn}]}),event(finish='tool_calls')]))
    async def run():
        provider = Provider(ProviderConfig('custom','https://fixture.test/v1','fixture'))
        await provider._client.aclose()
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            operator=Operator(provider,auto_accept=True,raw=True)
            result=''.join([t async for t in operator.send('Fix and test sum.py')])
            assert result=='Verified arithmetic.'
            assert len(requests)==4
            assert (tmp_path/'sum.py').read_text()=='assert 2 + 2 == 4\n'
        finally: await provider.close()
    asyncio.run(run())


def test_cli_custom_url_and_auto_accept_forwarded():
    with patch('djcode.repl.run_oneshot', new_callable=AsyncMock) as run:
        result=CliRunner().invoke(main,['-u','https://fixture.test/api/v1','-m','fixture','--auto-accept','hello'])
        assert result.exit_code==0,result.output
        assert run.call_args.kwargs['provider']=='https://fixture.test/api/v1'
        assert run.call_args.kwargs['auto_accept'] is True


def test_cli_failure_is_nonzero():
    with patch.object(Provider,'validate_model',return_value=(False,'No matching model')), patch.object(Provider,'close',new_callable=AsyncMock) as close:
        result=CliRunner().invoke(main,['hello'])
        assert result.exit_code!=0
        assert 'No matching model' in result.output
        close.assert_awaited_once()


@pytest.mark.parametrize('parts', [['<think>private</think>answer'], ['<thi','nk>private</th','ink>answer'], ['before<think>x</think>after']])
def test_thinking_tags_any_chunk_boundary(parts):
    parser=ThinkingStreamProcessor(show_thinking=False)
    answer=''.join(filter(None,(parser.process_token(p) for p in parts)))+(parser.flush() or '')
    assert answer==('beforeafter' if parts[0].startswith('before') else 'answer')
    assert parser.had_thinking


@pytest.mark.parametrize('finish',[None,'length','content_filter'])
def test_incomplete_stream_rejected(finish):
    class Fake:
        async def chat(self,*a,**kw):
            yield event({'content':'partial'})
            if finish: yield event(finish=finish)
    async def run():
        with pytest.raises((ConnectionError,RuntimeError)):
            _=[x async for x in stream_turn(Fake(),[])]
    asyncio.run(run())


def test_multiple_native_tool_calls_preserved():
    from djcode.providers.base import ProviderChunk,ToolCall,FinishReason
    class Native:
        async def chat(self,*a,**kw):
            yield ProviderChunk(tool_calls=[ToolCall(id='a',name='file_read',arguments='{"path":"a"}'),ToolCall(id='b',name='file_read',arguments='{"path":"b"}')],finish_reason=FinishReason.TOOL_USE)
        async def close(self):pass
    async def run():
        provider=Provider(ProviderConfig('openai','https://fixture.test/v1','fixture'))
        provider._new_provider=Native()
        try:
            calls=[x async for x in stream_turn(provider,[])][-1][1]
            assert [c['id'] for c in calls]==['a','b']
            assert [json.loads(c['function']['arguments'])['path'] for c in calls]==['a','b']
        finally:await provider.close()
    asyncio.run(run())


def test_malformed_arguments_never_execute(tmp_path):
    class Fake:
        config=ProviderConfig('custom','https://fixture.test','fixture')
        async def chat(self,messages,**kw):
            if messages[-1].role=='tool':
                assert 'JSON object' in messages[-1].content
                yield event({'content':'Cannot run malformed call.'});yield event(finish='stop')
            else:
                yield event({'tool_calls':[{'index':0,'id':'bad','function':{'name':'bash','arguments':'touch sentinel'}}]});yield event(finish='tool_calls')
    async def run():
        with patch('djcode.agents.operator.dispatch_tool',new_callable=AsyncMock) as dispatch:
            _=[x async for x in Operator(Fake(),auto_accept=True,raw=True).send('task')]
            dispatch.assert_not_awaited()
    asyncio.run(run())


def test_git_does_not_invoke_shell(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(execute_git('status; touch sentinel'))
    assert not (tmp_path/'sentinel').exists()


def test_shell_timeout_kills_descendant(tmp_path):
    # Child would leave evidence after parent shell's timeout if the group survived.
    marker=tmp_path/'escaped'
    result=asyncio.run(execute_bash(f"sleep 0.3; touch '{marker}'",timeout=0.05))
    assert 'timed out' in result
    import time; time.sleep(.4)
    assert not marker.exists()


def test_shell_output_bounded():
    result=asyncio.run(execute_bash("yes x | head -c 90000"))
    assert len(result)<50100 and 'truncated' in result


def test_featherless_config(monkeypatch):
    monkeypatch.setenv('FEATHERLESS_API_KEY','test-placeholder')
    config=ProviderConfig.from_config('featherless','model-id')
    assert config.base_url=='https://api.featherless.ai/v1'
    assert config.api_key=='test-placeholder'


def test_transient_retry_before_emission_only():
    async def run():
        provider=Provider(ProviderConfig('custom','https://fixture.test/v1','fixture'))
        attempts=[]
        async def backend(*a,**kw):
            attempts.append(1)
            if len(attempts)<2:
                yield {'error':{'code':503,'message':'overloaded'}}
            else:
                yield event({'content':'done'});yield event(finish='stop')
        provider.chat_openai_compat=backend
        try:
            with patch('djcode.provider.asyncio.sleep',new_callable=AsyncMock):
                result=[x async for x in stream_turn(provider,[])]
            assert len(attempts)==2 and result[0][0]=='done'
        finally:await provider.close()
    asyncio.run(run())


def test_native_tool_summary_code_blocks_are_never_reexecuted(tmp_path, monkeypatch):
    """A summary after native execution is prose, never a second write/command."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / 'test_arithmetic.py'
    marker = tmp_path / 'summary-command-ran'
    summary = (
        'Verified `test_arithmetic.py`:\n```python\nARITHMETIC_TESTS_PASSED\nCONFIDENCE: 1.00\n```\n'
        f'Example command (already complete):\n```bash\ntouch {marker}\n```\n'
    )
    class Fake:
        config = ProviderConfig('custom', 'https://fixture.test', 'fixture')
        calls = 0
        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield event({'tool_calls': [{'index': 0, 'id': 'write_1', 'function': {
                    'name': 'file_write', 'arguments': json.dumps({'path': str(target), 'content': 'assert 2 + 2 == 4\n'})}}]})
                yield event(finish='tool_calls')
            elif self.calls == 2:
                yield event({'content': summary})
                yield event(finish='stop')
            else:
                raise AssertionError('Native summary incorrectly caused another tool round')
    async def run():
        provider = Fake()
        operator = Operator(provider, auto_accept=True, raw=True)
        result = ''.join([part async for part in operator.send('Write and summarize test')])
        assert result == summary
        assert provider.calls == 2
        assert target.read_text() == 'assert 2 + 2 == 4\n'
        assert not marker.exists()
        assert operator.messages[-1].role == 'assistant'
        assert not any('[Tool Execution Results]' in message.content for message in operator.messages)
    asyncio.run(run())
