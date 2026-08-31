"""真实 Host → Kernel 生命周期；存储替身仅记录调用或抛出故障。"""

import asyncio

import pytest

from src.A_memorix.host_service import AMemorixHostService
from src.A_memorix.core.runtime.sdk_memory_kernel import SDKMemoryKernel


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "persist", "metadata_close"])
async def test_host_awaits_kernel_and_propagates_storage_failure(tmp_path, monkeypatch, failure):
    events = []
    kernel = SDKMemoryKernel(plugin_root=tmp_path, config={"storage": {"data_dir": str(tmp_path / "memory")}})
    kernel._runtime_writer_lock.acquire()
    kernel._initialized = True

    class Metadata:
        def close(self):
            events.append("metadata_close")
            if failure == "metadata_close":
                raise RuntimeError("fixture metadata close failed")

    async def stop_background():
        events.append("stop_background")

    def persist():
        events.append("persist")
        if failure == "persist":
            raise RuntimeError("fixture persist failed")

    kernel.metadata_store = Metadata()
    monkeypatch.setattr(kernel, "_stop_background_tasks", stop_background)
    monkeypatch.setattr(kernel, "_persist", persist)
    # 不读取实际 config 或启动 Host；仅构造 stop() 所需的状态。
    host = object.__new__(AMemorixHostService)
    host._lock = asyncio.Lock()
    host._startup_task = None
    host._kernel = kernel
    host._runtime_state = "running"
    try:
        if failure:
            with pytest.raises(RuntimeError, match="fixture"):
                await host.stop()
        else:
            await host.stop()
            assert host._kernel is None
            assert host._runtime_state == "stopped"
        assert events == ["stop_background", "persist", "metadata_close"]
        assert not kernel._runtime_writer_lock.held
        assert not kernel._initialized
    finally:
        kernel._runtime_writer_lock.release()
