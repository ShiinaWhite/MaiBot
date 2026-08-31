"""第三层证据：真实 Host/Kernel/persist/SQLite close，只使用临时合成数据。"""

from contextlib import closing

import asyncio
import sqlite3
import sys

import numpy as np
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "persist", "metadata_close"])
async def test_shutdown_persists_and_closes_real_stores(tmp_path, monkeypatch, failure):
    from pytests.startup_test.storage_isolation import install

    install(monkeypatch)

    from src.A_memorix.host_service import AMemorixHostService
    from src.A_memorix.core.runtime.sdk_memory_kernel import SDKMemoryKernel
    from src.A_memorix.core.storage.graph_store import GraphStore
    from src.A_memorix.core.storage.metadata_store import MetadataStore
    from src.A_memorix.core.storage.vector_store import VectorStore

    app_db = sys.modules["src.common.database.database"]
    assert app_db.SHUTDOWN_TEST_STUB and not hasattr(app_db, "__file__"), "Real application DB must not be imported"

    root = tmp_path / "memory"
    kernel = SDKMemoryKernel(plugin_root=tmp_path, config={"storage": {"data_dir": str(root)}})
    kernel._runtime_writer_lock.acquire()
    metadata = MetadataStore(data_dir=root / "metadata")
    reopened = None
    events = []
    try:
        metadata.connect()
        connection_before = metadata._conn
        paragraph = metadata.add_paragraph("Synthetic shutdown fixture paragraph", source="shutdown-test")
        relation = metadata.add_relation("fixture-a", "linked-to", "fixture-b", source_paragraph=paragraph)
        vectors = VectorStore(dimension=3, data_dir=root / "vectors", use_mmap=False)
        expected_vector = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        vectors.add(expected_vector, [paragraph])
        graph = GraphStore(data_dir=root / "graph")
        graph.add_edges([("fixture-a", "fixture-b")], relation_hashes=[relation])
        kernel.metadata_store, kernel.vector_store, kernel.graph_store = metadata, vectors, graph
        kernel._initialized = True
        original_persist, original_close = kernel._persist, metadata.close

        def persist():
            events.append("persist_started")
            if failure == "persist":
                raise RuntimeError("injected persist failure")
            original_persist()  # 真实向量/图存储 save；不 mock _save_vector_store 或 store.save。
            events.append("persist_completed")

        def close():
            events.append("metadata_close_started")
            original_close()
            events.append("metadata_closed")
            if failure == "metadata_close":
                raise RuntimeError("injected metadata close failure")

        monkeypatch.setattr(kernel, "_persist", persist)
        monkeypatch.setattr(metadata, "close", close)
        host = object.__new__(AMemorixHostService)
        host._lock, host._startup_task, host._kernel, host._runtime_state = asyncio.Lock(), None, kernel, "running"
        events.append("host_stop_started")
        if failure:
            with pytest.raises(RuntimeError, match="injected"):
                await host.stop()
        else:
            await host.stop()
            events.append("host_stop_completed")
            assert host._kernel is None and host._runtime_state == "stopped"
        assert not kernel._runtime_writer_lock.held
        assert kernel.metadata_store is None and not kernel._initialized
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection_before.execute("SELECT 1")
        expected = ["host_stop_started", "persist_started"]
        if failure != "persist":
            expected.append("persist_completed")
        expected += ["metadata_close_started", "metadata_closed"]
        if not failure:
            expected.append("host_stop_completed")
        assert events == expected

        db_path = root / "metadata" / "metadata.db"
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert db.execute("SELECT content FROM paragraphs WHERE hash=?", (paragraph,)).fetchone() == (
                "Synthetic shutdown fixture paragraph",
            )
        if failure != "persist":
            for name in ("vectors.bin", "vectors_ids.bin", "vectors_metadata.json"):
                assert (root / "vectors" / name).stat().st_size > 0
            reopened_vectors = VectorStore(dimension=3, data_dir=root / "vectors", use_mmap=False)
            reopened_vectors.load()
            restored = reopened_vectors.get_vectors([paragraph])
            assert set(restored) == {paragraph}
            np.testing.assert_allclose(restored[paragraph], expected_vector[0], atol=1e-6)
            reopened_graph = GraphStore(data_dir=root / "graph")
            reopened_graph.load()
            assert reopened_graph.num_nodes == 2 and reopened_graph.num_edges == 1
            assert set(reopened_graph.get_nodes()) == {"fixture-a", "fixture-b"}
            reopened = MetadataStore(data_dir=root / "metadata")
            reopened.connect()
            assert reopened.get_relation(relation) is not None
        # 相同数据目录可再次拿锁，证明不是仅清空了一个内存标志。
        kernel._runtime_writer_lock.acquire()
        kernel._runtime_writer_lock.release()
    finally:
        MetadataStore.close(metadata)
        if reopened is not None:
            reopened.close()
        kernel._runtime_writer_lock.release()
