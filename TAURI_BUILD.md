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
- 端口固定 8080，如冲突修改 `src-tauri/src/python_sidecar.rs` 的 `port` 字段

### 5. 前端无法连接后端
检查：
1. Sidecar 是否启动成功 (控制台日志 `[lingxi-server]`)
2. `http://127.0.0.1:8080/api/health` 返回 200
3. `NEXT_PUBLIC_ADMIN_TOKEN` 环境变量已设置

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
│  │  - fetch → http://127.0.0.1:8080  │
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
- 后端作为 sidecar 随主进程启动/停止，监听 `127.0.0.1:8080`
- 无需用户安装 Python/Node/NapCat/PostgreSQL —— 全部内置