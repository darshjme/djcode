"""Persistence and protocol invariants exercised without model downloads or inference."""
import asyncio
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from djcode.context.compressor import CompressionStrategy, ConversationCompressor
from djcode.context.manager import ContextWindowManager
from djcode.memory import manager as memory_module
from djcode.memory.embedder import VectorStore
from djcode.provider import Message
from djcode.sessions import SessionDB


def transcript():
    return [
        Message('system', 'Keep project rules'),
        Message('user', 'Investigate ' * 200),
        Message('assistant', '', tool_calls=[
            {'id': 'one', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}},
            {'id': 'two', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}},
        ]),
        Message('tool', 'file one ' * 100, tool_call_id='one', name='read_file'),
        Message('tool', 'file two ' * 100, tool_call_id='two', name='read_file'),
        Message('assistant', 'Both read'),
        Message('user', 'Fix the bug'),
    ]


def assert_complete_tools(messages):
    pending = set()
    for msg in messages:
        if msg.role == 'tool':
            assert msg.tool_call_id in pending
            pending.remove(msg.tool_call_id)
        else:
            assert not pending
            pending = {call['id'] for call in msg.tool_calls or []}
    assert not pending


@pytest.mark.parametrize('strategy', list(CompressionStrategy))
@pytest.mark.parametrize('recent', [0, 1, 3, 4])
def test_compaction_keeps_complete_tool_groups(strategy, recent):
    result = asyncio.run(ConversationCompressor().compress(
        transcript(), strategy=strategy, target_tokens=100, keep_recent=recent,
    ))
    assert_complete_tools(result.messages)
    assert result.messages[0].content == 'Keep project rules'


def test_pinned_tool_result_keeps_whole_group():
    messages = transcript()
    messages[3]._pinned = True
    result = ConversationCompressor().trim(messages, target_tokens=1, keep_recent=1)
    assert_complete_tools(result.messages)
    assert messages[3] in result.messages


def test_sqlite_restart_keeps_protocol_and_search(tmp_path):
    path = tmp_path / 'sessions.db'
    db = SessionDB(path)
    sid = db.create_session('test-model', 'test-provider')
    db.save_conversation(sid, transcript())
    restored = SessionDB(path).load_conversation(sid)
    messages = [Message(**{k: v for k, v in row.items() if k != 'timestamp'}) for row in restored]
    assert_complete_tools(messages)
    assert messages[3].name == 'read_file'
    assert SessionDB(path).search_sessions('Investigate')[0].id == sid
    db.save_message(sid, 'tool', 'extra', tool_call_id='extra', name='read_file')
    assert SessionDB(path).load_conversation(sid)[-1]['tool_call_id'] == 'extra'


def test_schema_upgrade_preserves_existing_conversation(tmp_path):
    path = tmp_path / 'old.db'
    db = SessionDB(path)
    sid = db.create_session('test', 'test')
    db.save_message(sid, 'user', 'retained')
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE conversations DROP COLUMN tool_call_id')
        conn.execute('ALTER TABLE conversations DROP COLUMN name')
    reopened = SessionDB(path)
    assert reopened.load_conversation(sid)[0]['content'] == 'retained'
    reopened.save_message(sid, 'tool', 'result', tool_call_id='restored')
    assert reopened.load_conversation(sid)[-1]['tool_call_id'] == 'restored'


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, 'MEMORY_DIR', tmp_path)
    monkeypatch.setattr(memory_module, 'FACTS_FILE', tmp_path / 'facts.json')
    monkeypatch.setattr(memory_module, 'CONVERSATIONS_DIR', tmp_path / 'conversations')
    monkeypatch.setattr(VectorStore, '_init_chroma', lambda self: None)
    return memory_module.MemoryManager()


def test_memory_restart_lexical_and_vector_fallback(memory):
    memory.remember('compiler', 'Use Rust stable', embedding=[1., 0.])
    reopened = memory_module.MemoryManager()
    assert reopened.recall('compiler') == 'Use Rust stable'
    assert reopened.search('Rust')[0][0] == 'compiler'
    assert reopened.search_similar([1., 0.]) == [('compiler', 1.)]
    assert 'lexical' in reopened.stats['search_backend']


def test_atomic_failure_preserves_previous_facts(memory, monkeypatch):
    memory.remember('saved', 'original')
    def fail(*args):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(memory_module.os, 'replace', fail)
    with pytest.raises(OSError):
        memory.remember('new', 'not durable')
    assert json.loads(memory_module.FACTS_FILE.read_text())['saved']['content'] == 'original'
    assert 'new' not in json.loads(memory_module.FACTS_FILE.read_text())


def test_invalid_memory_is_not_silently_overwritten(memory):
    memory_module.FACTS_FILE.write_text('{truncated')
    with pytest.raises(ValueError, match='Cannot read memory'):
        memory_module.MemoryManager()
    assert memory_module.FACTS_FILE.read_text() == '{truncated'


def test_conversation_rejects_traversal_and_invalid_payload(memory):
    with pytest.raises(ValueError):
        memory.save_conversation('../../escape')
    memory.add_session_message('user', 'retained')
    path = memory.save_conversation('safe')
    path.write_text('{}')
    assert not memory.load_conversation('safe')
    assert memory.get_session_messages()[0]['content'] == 'retained'


def test_chroma_has_no_implicit_embedding_model(monkeypatch, tmp_path):
    def settings(**options):
        assert options == {"anonymized_telemetry": False}
        return options
    monkeypatch.setitem(sys.modules, "chromadb.config", SimpleNamespace(Settings=settings))
    kwargs = {}
    class Client:
        def get_or_create_collection(self, **options):
            kwargs.update(options)
            return object()
    monkeypatch.setitem(sys.modules, 'chromadb', SimpleNamespace(PersistentClient=lambda **kw: Client()))
    monkeypatch.setattr('djcode.memory.embedder.CHROMA_DIR', tmp_path)
    assert VectorStore().is_chroma
    assert kwargs['embedding_function'] is None


def test_expired_context_updates_cached_budget(monkeypatch):
    manager = ContextWindowManager('test', max_context=1000)
    manager.inject_context('long context ' * 20, source='test', ttl=10)
    before = manager.current_tokens
    monkeypatch.setattr('djcode.context.manager.time.time', lambda: 1e20)
    assert before > 0
    assert manager.current_tokens == 0


def test_two_open_sessions_do_not_lose_each_others_facts(memory):
    second = memory_module.MemoryManager()
    memory.remember('first', 'one')
    second.remember('second', 'two')
    assert memory.list_facts() == ['first', 'second']
    memory.forget('first')
    second.remember('third', 'three')
    assert memory_module.MemoryManager().list_facts() == ['second', 'third']


def test_updating_fact_without_embedding_clears_stale_vector(memory, monkeypatch):
    deleted = []
    monkeypatch.setattr(memory._vectors, 'delete', deleted.append)
    memory.remember('fact', 'old', embedding=[1., 0.])
    memory.remember('fact', 'new')
    assert deleted == ['fact']
    assert memory.search_similar([1., 0.]) == []
