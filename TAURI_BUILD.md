# Tauri 桌面端构建指南

## 前置依赖

### Windows
- Rust (stable): `winget install Rustlang.Rust`
- Python 3.13+: 已有
- Node.js 20+: 已有
- PyInstaller: `pip install pyinstaller`
- Visual Studio Build Tools (C++ 桌面开发工作负载)

### macOS
- Rust: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
- Xcode Command Line Tools: `xcode-select --install`
- Python/Node/PyInstaller 同上

### Linux (Ubuntu/Debian)
```bash
sudo apt update && sudo apt install -y build-essential libssl-dev libwebkit2gtk-4.1-dev \
  libayatana-appindicator3-dev librsvg2-dev curl
```

---

## 一键构建流程

```bash
cd lingxi

# 1. 打包 Python sidecar (必须先跑)
python scripts/build_sidecar.py

# 2. 安装前端依赖
cd web && npm install

# 3a. 开发模式 (热重载前端 + sidecar)
npm run tauri:dev
# 或分离运行:
# 终端1: npm run dev
# 终端2: cargo tauri dev --manifest-path ../src-tauri/Cargo.toml

# 3b. 生产构建 (输出安装包)
npm run tauri:build
```

---

## 产物位置

| 平台 | 产物 |
|------|------|
| Windows | `src-tauri/target/release/bundle/msi/lingxi_0.2.0_x64_en-US.msi`<br>`src-tauri/target/release/bundle/nsis/lingxi_0.2.0_x64_set.exe` |
| macOS | `src-tauri/target/release/bundle/dmg/ZhiXi_0.2.0_aarch64.dmg` |
| Linux | `src-tauri/target/release/bundle/appimage/lingxi_0.2.0_amd64.AppImage`<br>`src-tauri/target/release/bundle/deb/lingxi_0.2.0_amd64.deb` |

---

## 配置文件位置

| 平台 | 数据目录 |
|------|----------|
| Windows | `%APPDATA%\lingxi\` |
| macOS | `~/Library/Application Support/lingxi/` |
| Linux | `~/.local/share/lingxi/` (或 `$XDG_DATA_HOME/lingxi/`) |

配置文件：`config.yaml` 或环境变量 `LINGXI_SECURITY__ADMIN_TOKEN`

---

## 常见问题

### 1. `pyinstaller` 报错 "module not found"
```bash
pip install --upgrade pyinstaller
# 确保所有依赖已安装
pip install -e .
```

### 2. Windows 编译报错 `LINK : fatal error LNK1181`
安装 Visual Studio Build Tools，勾选 "C++ 桌面开发" 工作负载。

### 3. macOS 签名/公证失败
本地构建不需要签名。发布时配置 GitHub Actions secrets:
- `APPLE_CERTIFICATE` (base64 encoded .p12)
- `APPLE_CERTIFICATE_PASSWORD`
- `APPLE_ID`
- `APPLE_TEAM_ID`

### 4. Sidecar 启动慢 / 端口冲突
- 首次启动需解压 sidecar (~3-5 秒)，后续秒开
- **端口已动态化**：默认首选 8080，被占用时 Python 侧自动顺延（8081…）
  并通过 stdout 的 `ZHIXI_DASHBOARD_PORT=<n>` 行上报真实端口；
  壳进程解析该行、前端经 `invoke("get_backend_port")` 运行时取真实地址。
  无需再改代码——想固定端口就设环境变量 `LINGXI_DASHBOARD_PORT`
- 后端进程意外退出/无响应：守护循环会打印**退出码 + stderr 尾部**（诊断死因）
  并自动退避重启（最多 5 次），重启也重新选端口

### 5. 前端无法连接后端
检查：
1. Sidecar 是否启动成功 (控制台日志 `[lingxi-server]`)
2. 启动日志里的 `ZHIXI_DASHBOARD_PORT=<n>`，然后访问
   `http://127.0.0.1:<n>/api/health` 应返回 200
3. 桌面版令牌由壳自动注入（每次安装随机生成，存
   `%APPDATA%\lingxi\auth_token`），**不需要**手动设置
   `NEXT_PUBLIC_ADMIN_TOKEN`；浏览器 dev 模式才需要

---

## 调试技巧

### 查看 sidecar 日志
```bash
# 开发模式直接在终端可见
# 生产模式查看系统日志或重定向到文件
```

### 重新打包 sidecar
```bash
cd lingxi
python scripts/build_sidecar.py
# 然后重新 cargo tauri build
```

### 只重新构建前端
```bash
cd web && TAURI=1 npm run build
# 然后 cargo tauri build --manifest-path ../src-tauri/Cargo.toml
```

---

## CI/CD (GitHub Actions)

见 `.github/workflows/tauri.yml` (需配置 secrets):
- `APPLE_CERTIFICATE` / `APPLE_CERTIFICATE_PASSWORD` / `APPLE_ID` / `APPLE_TEAM_ID` (macOS)
- `TAURI_PRIVATE_KEY` / `TAURI_KEY_PASSWORD` (自动更新签名)

---

## 架构说明

```
┌─────────────────────────────────────┐
│           Tauri WebView             │
│  ┌─────────────────────────────┐   │
│  │      Next.js 前端 (React)    │   │
│  │  - Dashboard / Queue / etc   │   │
│  │  - fetch → 127.0.0.1:<动态端口>  │
│  └──────────────┬───────────────┘   │
└─────────────────┼───────────────────┘
                  │ HTTP/JSON (localhost)
         ┌────────▼────────┐
         │  Python Sidecar │  (单文件 .exe, ~60-80MB)
         │  - FastAPI      │
         │  - Agent L4/L5  │
         │  - SQLite/Postgres│
         │  - MCP Server   │
         └─────────────────┘
```

- 前端打包为静态文件 (`next export`)，运行时由 Tauri WebView 加载
- 后端作为 sidecar 随主进程启动/停止，监听动态端口（默认 8080 起步，
  被占自动顺延；前端运行时经 `get_backend_port` 发现真实地址）
- 无需用户安装 Python/Node/NapCat/PostgreSQL —— 全部内置