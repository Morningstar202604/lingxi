#!/usr/bin/env python3
"""把 Python 后端打包为单文件 sidecar，供 Tauri 调用。"""
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
DIST = ROOT / "src-tauri" / "dist"
DIST.mkdir(parents=True, exist_ok=True)

# 1. 清理旧产物
for p in DIST.glob("*"):
    if p.is_file():
        p.unlink()
    else:
        shutil.rmtree(p)

# 2. 收集隐藏导入
hidden_imports = [
    "lingxi", "lingxi.agent", "lingxi.flow", "lingxi.memory",
    "lingxi.persona", "lingxi.seat", "lingxi.workflow", "lingxi.mcp",
    "lingxi.config", "lingxi.core", "lingxi.im", "lingxi.knowledge",
    "lingxi.security", "lingxi.experiment",
    "uvicorn", "fastapi", "sse_starlette", "starlette",
    "pydantic", "pydantic_settings", "pydantic_core",
    "asyncpg", "aiosqlite", "sqlalchemy",
    "litellm", "openai", "anthropic",
    "orjson", "structlog", "python-json-logger",
    "python-multipart", "python-dotenv",
    "apscheduler", "croniter",
    "prometheus_client",
    "httpx", "aiohttp",
    "watchfiles",
]

# 3. 运行 pyinstaller
cmd = [
    "pyinstaller",
    "--clean",
    "--noconfirm",
    "--onefile",
    "--name", "lingxi-server",
    "--distpath", str(DIST),
    "--workpath", str(ROOT / "build" / "pyi"),
    "--specpath", str(ROOT / "build" / "pyi"),
    "--add-data", f"{ROOT}/src/lingxi:lingxi",
    "--add-data", f"{ROOT}/pyproject.toml:.",
]

for hi in hidden_imports:
    cmd.extend(["--hidden-import", hi])

cmd.append(str(ROOT / "src" / "lingxi" / "__main__.py"))

print(">>> Building Python sidecar...")
print(f"Command: {' '.join(cmd[:5])} ...")

result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
if result.returncode != 0:
    print("STDOUT:", result.stdout[-2000:])
    print("STDERR:", result.stderr[-2000:])
    sys.exit(result.returncode)

# 4. 验证产物
exe_name = "lingxi-server.exe" if sys.platform == "win32" else "lingxi-server"
exe = DIST / exe_name
if not exe.exists():
    print(f">>> ERROR: Sidecar not found: {exe}")
    sys.exit(1)

# 5. 简单测试
print(f">>> Sidecar ready: {exe} ({exe.stat().st_size / 1024 / 1024:.1f} MB)")

# 测试导入
test_cmd = [str(exe), "--help"]
test_result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=30)
if test_result.returncode != 0 and "unrecognized arguments" not in test_result.stderr:
    print(f">>> WARNING: Sidecar test failed: {test_result.stderr}")
else:
    print(">>> Sidecar basic test passed")

# 6. 同步到 Tauri externalBin 命名（cargo tauri build 打包用的就是这份）
# externalBin: ["lingxi-server"] 会按 `lingxi-server-<target-triple>` 查找，
# 只更新 dist/ 而不更新这份，cargo tauri build 打出来的安装包里
# 还是上一次的旧 sidecar（曾实测：动态端口契约在打包产物里失效）。
_machine = platform.machine().lower()
if sys.platform == "win32":
    _triple = "x86_64-pc-windows-msvc" if _machine in ("amd64", "x86_64") else "aarch64-pc-windows-msvc"
elif sys.platform == "darwin":
    _triple = "aarch64-apple-darwin" if _machine in ("arm64", "aarch64") else "x86_64-apple-darwin"
else:
    _triple = "aarch64-unknown-linux-gnu" if _machine in ("aarch64", "arm64") else "x86_64-unknown-linux-gnu"

tauri_bin = ROOT / "src-tauri" / f"lingxi-server-{_triple}{'.exe' if sys.platform == 'win32' else ''}"
shutil.copy2(exe, tauri_bin)
print(f">>> Synced to Tauri externalBin: {tauri_bin}")

print(">>> Build complete!")
