"""Orchestrator startup and fallback never fetch implicit embedding models."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from djcode.orchestrator import vector_context
from djcode.orchestrator.context_bus import ContextBus
from djcode.orchestrator.router import SemanticRouter


@pytest.fixture(autouse=True)
def fake_chroma_settings(monkeypatch):
    def settings(**kwargs):
        assert kwargs == {"anonymized_telemetry": False}
        return kwargs
    monkeypatch.setitem(sys.modules, "chromadb.config", SimpleNamespace(Settings=settings))


class NoEmbeddings:
    async def embed(self, text):
        pytest.fail('implicit embedding request')


def test_default_router_stays_offline():
    router = SemanticRouter(NoEmbeddings())
    assert not asyncio.run(router.initialize())
    assert asyncio.run(router.route('write tests for this function'))
    assert not router.is_semantic


def test_router_semantics_are_explicit():
    seen = []
    class Provider:
        async def embed(self, text):
            seen.append(text)
            return [1., 0.]
    router = SemanticRouter(Provider(), semantic=True)
    assert asyncio.run(router.initialize())
    assert asyncio.run(router.route('write code'))
    assert seen


def test_default_context_persists_and_retrieves_without_chroma(tmp_path, monkeypatch):
    monkeypatch.setattr(vector_context, 'VECTOR_DIR', tmp_path)
    monkeypatch.setitem(sys.modules, 'chromadb', SimpleNamespace(
        PersistentClient=lambda **kw: pytest.fail('implicit Chroma model backend initialized')))
    store = vector_context.VectorContextStore(NoEmbeddings())
    assert store.initialize()
    store.store('Rust compiler uses stable channel', category='fact')
    store.store('Python tests use pytest', category='fact')
    reopened = vector_context.VectorContextStore(NoEmbeddings())
    assert reopened.initialize()
    assert reopened.count() == 2
    result = reopened.retrieve('Rust compiler', category='fact')
    assert len(result) == 1
    assert result[0]['backend'] == 'lexical'
    assert result[0]['distance'] == 0
    assert not reopened.retrieve('Rust', category='exchange')
    assert reopened.backend == 'lexical (SQLite)'


def test_multiple_retrieved_entries_are_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(vector_context, 'VECTOR_DIR', tmp_path)
    store = vector_context.VectorContextStore()
    store.initialize()
    store.store('Rust first note')
    store.store('Rust second note')
    bus = ContextBus()
    assert store.inject_context(bus, 'Rust') == 2
    assert len(bus.read_all()) == 2
    assert all(entry.metadata['source'] == 'lexical' for entry in bus.read_all())


def test_explicit_semantic_path_always_supplies_vectors(tmp_path, monkeypatch):
    monkeypatch.setattr(vector_context, 'VECTOR_DIR', tmp_path)
    collection_options = {}
    class Collection:
        def upsert(self, **kwargs):
            assert kwargs['embeddings'] == [[1., 0.]]
        def query(self, **kwargs):
            assert kwargs['query_embeddings'] == [[1., 0.]]
            assert 'query_texts' not in kwargs
            return {'documents': [['Rust compiler']], 'metadatas': [[{}]], 'distances': [[0.1]]}
    class Client:
        def get_or_create_collection(self, **kwargs):
            collection_options.update(kwargs)
            return Collection()
    monkeypatch.setitem(sys.modules, 'chromadb', SimpleNamespace(PersistentClient=lambda **kw: Client()))
    store = vector_context.VectorContextStore(embedding_function=lambda _: [1., 0.])
    assert store.initialize()
    assert collection_options['embedding_function'] is None
    store.store('Rust compiler')
    assert store.retrieve('Rust')[0]['backend'] == 'semantic'


def test_embedding_failure_retains_lexical_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(vector_context, 'VECTOR_DIR', tmp_path)
    class Client:
        def get_or_create_collection(self, **kwargs):
            return object()
    monkeypatch.setitem(sys.modules, 'chromadb', SimpleNamespace(PersistentClient=lambda **kw: Client()))
    def failing(text):
        raise RuntimeError('embedding server unavailable')
    store = vector_context.VectorContextStore(embedding_function=failing)
    store.initialize()
    store.store('Durable Rust context')
    assert store.retrieve('Rust')[0]['backend'] == 'lexical'
