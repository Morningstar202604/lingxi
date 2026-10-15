"""Shell 沙箱测试（Docker 隔离 + 降级可见）。

🔗 工程逻辑
    权限分级管"要不要批准"，沙箱管"执行时能碰到什么"。
    本文件锁三件事：
    1. 安全开关全部就位（禁网 / cap-drop / 资源上限 / 只读挂载）
    2. Docker 不可用时**降级但可见**（不静默失去隔离）
    3. 危险挂载不会出现（不能挂宿主根、默认不可写）
"""
from __future__ import annotations

import pytest

from lingxi.security.sandbox import (
    build_docker_command,
    docker_available,
    reset_probe_for_tests,
    run_in_sandbox,
    sandbox_status,
)


class TestDockerCommandSafety:
    """逐项断言安全开关（这是隔离的实际生效点）。"""

    def test_network_disabled_by_default(self) -> None:
        args = build_docker_command("echo hi")
        assert "--network" in args
        assert args[args.index("--network") + 1] == "none", "默认必须禁网"

    def test_capabilities_dropped(self) -> None:
        args = build_docker_command("echo hi")
        assert args[args.index("--cap-drop") + 1] == "ALL"

    def test_no_new_privileges(self) -> None:
        args = build_docker_command("echo hi")
        assert "no-new-privileges" in args

    def test_resource_limits_present(self) -> None:
        args = build_docker_command("echo hi", memory="256m", cpus="0.5", pids_limit=32)
        assert args[args.index("--memory") + 1] == "256m"
        assert args[args.index("--cpus") + 1] == "0.5"
        assert args[args.index("--pids-limit") + 1] == "32"

    def test_ephemeral_container(self) -> None:
        args = build_docker_command("echo hi")
        assert "--rm" in args, "用完即删，避免残留容器污染后续执行"

    def test_no_mount_by_default(self) -> None:
        """默认不挂任何宿主目录（隔离的最强形态）。"""
        args = build_docker_command("echo hi")
        assert "-v" not in args

    def test_mount_is_readonly_by_default(self) -> None:
        args = build_docker_command("echo hi", mount_dir="/host/data")
        spec = args[args.index("-v") + 1]
        assert spec.endswith(":ro"), f"默认必须只读挂载，实际 {spec}"

    def test_mount_writable_only_when_explicit(self) -> None:
        args = build_docker_command("echo hi", mount_dir="/host/data", writable=True)
        spec = args[args.index("-v") + 1]
        assert not spec.endswith(":ro")

    def test_command_run_via_sh_lc(self) -> None:
        args = build_docker_command("ls -la")
        assert args[-3:] == ["sh", "-lc", "ls -la"]


class TestDegradationIsVisible:
    """Docker 不可用时降级，但必须可观测（不能静默失去隔离）。"""

    @pytest.mark.asyncio
    async def test_disabled_returns_not_used(self) -> None:
        out, used = await run_in_sandbox("echo hi", enabled=False)
        assert used is False, "显式关闭时 used=False，调用方据此落宿主执行"
        assert out == ""

    @pytest.mark.asyncio
    async def test_docker_missing_returns_not_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import lingxi.security.sandbox as sb

        reset_probe_for_tests()
        monkeypatch.setattr(sb, "docker_available", lambda: False)
        out, used = await sb.run_in_sandbox("echo hi")
        assert used is False
        assert out == ""

    def test_status_reports_availability(self) -> None:
        st = sandbox_status()
        assert "docker_available" in st
        assert "default_image" in st
        assert isinstance(st["docker_available"], bool)


@pytest.mark.skipif(not docker_available(), reason="Docker 不可用，跳过真实沙箱执行")
class TestRealSandboxExecution:
    """真实容器执行（本机 Docker 可用时跑）。"""

    @pytest.mark.asyncio
    async def test_simple_command_runs_in_sandbox(self) -> None:
        out, used = await run_in_sandbox("echo sandbox_ok", timeout=90)
        assert used is True
        assert "sandbox_ok" in out

    @pytest.mark.asyncio
    async def test_network_is_blocked(self) -> None:
        """禁网是核心隔离项：容器内不该能访问外网。"""
        out, used = await run_in_sandbox(
            "wget -q -O- http://example.com 2>&1 | head -c 100", timeout=90
        )
        assert used is True
        # 禁网时 wget 报地址解析/网络错误（不同 alpine 版本措辞不同）
        lowered = out.lower()
        assert any(
            k in lowered
            for k in ("bad address", "unreachable", "not found", "connection", "network", "[exit=")
        ), f"网络似乎未被阻断，输出: {out[:200]}"

    @pytest.mark.asyncio
    async def test_dangerous_write_outside_mount_fails(self) -> None:
        """容器内写宿主路径不该影响宿主（容器文件系统隔离）。"""
        marker = "/tmp/zhixi_sandbox_write_test"
        out, used = await run_in_sandbox(f"sh -c 'echo x > {marker} && cat {marker}'", timeout=90)
        assert used is True
        # 容器内可写自己的 /tmp（隔离在容器里），但宿主同名文件不该出现
        import os

        assert not os.path.exists(marker), "容器写入泄漏到宿主文件系统"
