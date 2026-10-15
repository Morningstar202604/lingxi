"""Shell 执行沙箱 — **Docker 容器隔离**（官方方案，可配置降级）。

🔗 工程逻辑
    权限分级（guardrails.classify_shell_command）解决的是"要不要批准执行"，
    **不解决"执行时能碰到什么"** —— 一旦批准（或权限足够自动放行），
    命令就在宿主机上以引擎进程的身份运行：能读项目文件、能访问网络、
    能写任意路径。

    本模块给 shell 工具加一层**执行隔离**（用 Docker，工业标准）：

    | 隔离维度 | 做法 |
    |---|---|
    | 文件系统 | 只读挂载工作目录（`-v ...:ro`），不挂宿主根 |
    | 网络  | `--network none`（默认禁网，可按需开启） |
    | 权限  | `--cap-drop ALL` + `--security-opt no-new-privileges` |
    | 资源  | `--memory/--cpus/--pids-limit` 防 DoS |
    | 生命周期 | `--rm` 用完即删，不残留容器 |

    🤖 AI 避坑
        - Docker 不可用（未装/未启动/无权限）时**降级为宿主执行**，
          但必须打 warning 并可被 `sandbox_status()` 观测 ——
          静默降级等于"以为有隔离，其实没有"。
        - 挂载**只读**：agent 要写文件必须显式用可写挂载点（writable_dir），
          绝不给宿主根可写权限。
        - 不要用 `docker exec` 进长驻容器做隔离：容器会被上一条命令的
          副作用污染（残留文件/进程），失去"每次执行都是干净环境"的保证。
        - 超时用 `docker run` 的 `timeout` 参数（容器内小工具）不可靠，
          由外层 asyncio.wait_for + `docker kill` 兜底。
"""
from __future__ import annotations

import asyncio
import shutil
from typing import Any

from lingxi.core import get_logger

log = get_logger(__name__)

# 默认镜像：官方 alpine（体积小、无多余工具），首次使用会自动拉取
DEFAULT_IMAGE = "alpine:3.20"

_docker_available: bool | None = None


def docker_available() -> bool:
    """Docker CLI 是否可用（结果缓存；启动后不变）。

    只检查 CLI 存在 + daemon 响应，不做实际拉镜像（那会有网络依赖）。
    """
    global _docker_available
    if _docker_available is not None:
        return _docker_available
    if shutil.which("docker") is None:
        _docker_available = False
        log.warning(
            "shell_sandbox_unavailable_no_docker_cli",
            hint="装 Docker Desktop 或设 LINGXI_SANDBOX__ENABLED=false 显式关闭沙箱",
        )
        return False
    try:
        import subprocess

        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        _docker_available = proc.returncode == 0
        if not _docker_available:
            log.warning(
                "shell_sandbox_unavailable_daemon_down",
                stderr=proc.stderr.strip()[:200],
            )
    except Exception as e:  # noqa: BLE001
        _docker_available = False
        log.warning("shell_sandbox_probe_failed", error=str(e))
    return _docker_available


def build_docker_command(
    command: str,
    *,
    image: str = DEFAULT_IMAGE,
    workdir: str = "/work",
    mount_dir: str = "",
    writable: bool = False,
    network: str = "none",
    memory: str = "512m",
    cpus: str = "1",
    pids_limit: int = 128,
    timeout_seconds: float = 30.0,
) -> list[str]:
    """构造隔离执行的 docker run 参数（纯函数，便于单测断言每个安全开关）。

    安全开关逐项对应上面的隔离维度表；`network=none` 为默认。
    """
    args = [
        "docker", "run", "--rm",
        "--network", network,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", memory,
        "--cpus", cpus,
        "--pids-limit", str(pids_limit),
        "--workdir", workdir,
    ]
    if mount_dir:
        spec = f"{mount_dir}:{workdir}" + ("" if writable else ":ro")
        args += ["-v", spec]
    # 超时不用容器内的 `timeout`（alpine 的 busybox timeout 语义不一致，
    # 且多进程/睡眠场景不可靠）—— 由外层 asyncio.wait_for + kill 兜底。
    args += [image, "sh", "-lc", command]
    return args


async def run_in_sandbox(
    command: str,
    *,
    enabled: bool = True,
    image: str = DEFAULT_IMAGE,
    mount_dir: str = "",
    writable: bool = False,
    network: str = "none",
    timeout: float = 30.0,
) -> tuple[str, bool]:
    """在 Docker 沙箱里执行命令。

    Returns:
        (输出文本, 是否实际走了沙箱)。第二项让调用方/日志能区分
        "沙箱执行"与"降级宿主执行"——降级必须可见。
    """
    if not enabled or not docker_available():
        return ("", False)

    args = build_docker_command(
        command,
        image=image,
        mount_dir=mount_dir,
        writable=writable,
        network=network,
        timeout_seconds=timeout,
    )
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 15)
    except TimeoutError:
        # 容器内超时不可靠（多进程/睡眠），外层兜底 kill
        with_suppress = __import__("contextlib").suppress
        with with_suppress(Exception):
            proc.kill()
        return (f"[sandbox-timeout] 命令超过 {timeout}s 未完成，已终止", True)

    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return (f"[exit={proc.returncode}] {err or out}", True)
    return (out, True)


def sandbox_status() -> dict[str, Any]:
    """沙箱状态（健康检查/排障）。"""
    return {
        "docker_available": docker_available(),
        "default_image": DEFAULT_IMAGE,
    }


def reset_probe_for_tests() -> None:
    """清掉探测缓存（测试用）。"""
    global _docker_available
    _docker_available = None


__all__ = [
    "DEFAULT_IMAGE",
    "build_docker_command",
    "docker_available",
    "run_in_sandbox",
    "sandbox_status",
]
