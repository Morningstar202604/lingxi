//! Python sidecar（lingxi-server）生命周期管理。
//!
//! 设计要点（修复审计发现的桌面端缺陷）：
//! 1. **child 句柄长期持有**：此前 `start_standalone` 设了 `kill_on_drop(true)`
//!    又在健康检查后 `drop(child)` —— 按 tokio 语义（ChildDropGuard::drop →
//!    kill）这等于启动成功后立刻把后端 TerminateProcess 掉，桌面 App 的
//!    Dashboard 活不过几秒。现在句柄存在 `PythonSidecar.child` 里，只有
//!    显式 `stop_shared`（App 退出）才杀。
//! 2. **动态端口**：不硬编码 8080。首选端口经环境变量传给 Python 侧，
//!    Python 发现被占用会自动顺延并打印 `ZHIXI_DASHBOARD_PORT=<n>`，
//!    本模块解析该行得到真实端口（唯一事实来源在 Python 侧）。
//! 3. **死亡可诊断**：stderr 尾部 30 行留在环形缓冲里，进程退出时连同
//!    退出码一起打印，能直接看到"为什么被杀"（端口冲突/依赖缺失/崩溃栈）。
//! 4. **自动重启**：进程退出或连续健康探测失败 → 退避重启（最多 5 次），
//!    重启也走同一条"重新选端口"的路径，端口被别的进程抢走也能自愈。
//! 5. **每次安装独立令牌**：随机生成并持久化到数据目录，同时注入 sidecar
//!    环境变量与前端（get_admin_token 命令），不再用公开的默认口令。
use std::collections::VecDeque;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::{AppHandle, Manager};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command as TokioCommand;
use tokio::sync::mpsc;

/// stderr 环形缓冲保留行数（诊断"为什么被杀"时打印）
const STDERR_TAIL_LINES: usize = 30;
/// 守护循环间隔
const SUPERVISE_INTERVAL: Duration = Duration::from_secs(5);
/// 连续健康探测失败多少次触发重启（5s × 12 = 60s，容忍慢启动）
const HEALTH_FAILS_BEFORE_RESTART: u32 = 12;
/// 自动重启次数上限（超过后停止，避免无限重启风暴）
const MAX_RESTARTS: u32 = 5;
/// 默认首选端口（Python 侧被占会自动顺延并上报真实端口）
const DEFAULT_PORT: u16 = 8080;

/// 前端通过 `invoke("get_backend_port")` / `invoke("get_admin_token")` 读取的共享状态。
pub struct SidecarState(pub Arc<Mutex<PythonSidecar>>);

/// 从 stderr 尾部里挑出值得展示给用户的"死因"线索。
fn describe_exit(stderr_tail: &Arc<Mutex<VecDeque<String>>>) -> String {
    let Ok(tail) = stderr_tail.lock() else {
        return String::new();
    };
    if tail.is_empty() {
        return String::new();
    }
    // 优先找异常栈/错误行；找不到就贴最后 5 行
    let interesting: Vec<&String> = tail
        .iter()
        .filter(|l| {
            let l = l.to_ascii_lowercase();
            l.contains("error") || l.contains("traceback") || l.contains("exception")
        })
        .collect();
    let picked: Vec<String> = if interesting.is_empty() {
        tail.iter().rev().take(5).rev().cloned().collect()
    } else {
        interesting.iter().rev().take(8).rev().map(|s| s.to_string()).collect()
    };
    picked.join("\n    ")
}

pub struct PythonSidecar {
    pub child: Option<tokio::process::Child>,
    pub executable: PathBuf,
    pub admin_token: String,
    /// 当前实际端口（从 Python 上报行解析；重启后会更新）
    pub port: u16,
    /// 首选端口（环境变量或默认值；Python 被占时在这个值基础上顺延）
    pub preferred_port: u16,
    pub stopping: AtomicBool,
    pub stderr_tail: Arc<Mutex<VecDeque<String>>>,
    pub restarts: u32,
}

impl PythonSidecar {
    pub fn new(app_handle: &AppHandle) -> anyhow::Result<Self> {
        let executable = app_handle
            .path()
            .resolve("lingxi-server", tauri::path::BaseDirectory::Resource)?
            .with_extension(if cfg!(windows) { "exe" } else { "" });

        // 令牌优先级：显式环境变量 > 持久化文件 > 新生成并落盘。
        // 生成 32 位十六进制随机串（uuid v4，底层 getrandom）。
        let data_dir = Self::data_dir()?;
        let admin_token = std::env::var("LINGXI_ADMIN_TOKEN")
            .or_else(|_| std::env::var("LINGXI_SECURITY__ADMIN_TOKEN"))
            .unwrap_or_else(|_| Self::load_or_create_token(&data_dir));

        let preferred_port = std::env::var("LINGXI_DASHBOARD_PORT")
            .ok()
            .and_then(|v| v.parse::<u16>().ok())
            .unwrap_or(DEFAULT_PORT);

        Ok(Self {
            child: None,
            executable,
            admin_token,
            port: preferred_port,
            preferred_port,
            stopping: AtomicBool::new(false),
            stderr_tail: Arc::new(Mutex::new(VecDeque::with_capacity(STDERR_TAIL_LINES))),
            restarts: 0,
        })
    }

    /// 从数据目录读取既有令牌；没有则生成一个随机令牌并持久化。
    fn load_or_create_token(data_dir: &str) -> String {
        let path = PathBuf::from(data_dir).join("auth_token");
        if let Ok(existing) = std::fs::read_to_string(&path) {
            let trimmed = existing.trim();
            if trimmed.len() >= 16 {
                return trimmed.to_string();
            }
        }
        let token = uuid::Uuid::new_v4().simple().to_string()
            + &uuid::Uuid::new_v4().simple().to_string();
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        match std::fs::write(&path, &token) {
            Ok(()) => println!(">>> 已生成新的本地管理令牌: {:?}", path),
            Err(e) => eprintln!(">>> 令牌持久化失败（本次仍可用，但下次会换新令牌）: {}", e),
        }
        token
    }

    /// 启动 sidecar 并等待它上报真实端口 + 通过健康检查。
    ///
    /// 成功后把 child 存回 `self.child`（长期持有），返回实际端口。
    pub async fn start(this: &Arc<Mutex<Self>>, _app_handle: &AppHandle) -> anyhow::Result<u16> {
        let (executable, token, preferred_port, stderr_tail) = {
            let guard = this.lock().unwrap();
            (
                guard.executable.clone(),
                guard.admin_token.clone(),
                guard.preferred_port,
                guard.stderr_tail.clone(),
            )
        };

        println!(
            ">>> 启动 Python 后端: {:?}（首选端口 {}，被占会自动换）",
            executable, preferred_port
        );

        let mut cmd = TokioCommand::new(&executable);
        cmd.env("LINGXI_SECURITY__ADMIN_TOKEN", &token)
            .env("LINGXI_DASHBOARD_PORT", preferred_port.to_string())
            .env("LINGXI_DATA_DIR", Self::data_dir()?)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        // 刻意不设 kill_on_drop：句柄由 PythonSidecar.child 持有，
        // 只有 App 退出（stop_shared）才允许杀进程。

        #[cfg(windows)]
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW（tokio 原生支持，无需 CommandExt）

        let mut child = cmd.spawn()?;

        // ── stdout：转发日志 + 解析机器可读端口行 ──
        let (port_tx, mut port_rx) = mpsc::channel::<u16>(4);
        if let Some(stdout) = child.stdout.take() {
            tokio::spawn(async move {
                let mut lines = BufReader::new(stdout).lines();
                while let Ok(Some(line)) = lines.next_line().await {
                    println!("[lingxi-server] {}", line);
                    if let Some(rest) = line.trim().strip_prefix("ZHIXI_DASHBOARD_PORT=") {
                        if let Ok(p) = rest.trim().parse::<u16>() {
                            let _ = port_tx.send(p).await;
                        }
                    }
                }
            });
        }

        // ── stderr：转发 + 环形缓冲留尾部（诊断死因）──
        if let Some(stderr) = child.stderr.take() {
            let tail = stderr_tail.clone();
            tokio::spawn(async move {
                let mut lines = BufReader::new(stderr).lines();
                while let Ok(Some(line)) = lines.next_line().await {
                    eprintln!("[lingxi-server ERR] {}", line);
                    if let Ok(mut t) = tail.lock() {
                        if t.len() >= STDERR_TAIL_LINES {
                            t.pop_front();
                        }
                        t.push_back(line);
                    }
                }
            });
        }

        // ── 等端口上报（启动慢的机器给足 60s；引擎构建约 1-3s，留大量余量）──
        let port = match tokio::time::timeout(Duration::from_secs(60), port_rx.recv()).await {
            Ok(Some(p)) => p,
            Ok(None) => anyhow::bail!(
                "后端进程提前退出（stdout 已关闭），未见端口上报。stderr 尾部:\n    {}",
                describe_exit(&stderr_tail)
            ),
            Err(_) => {
                let _ = child.kill().await;
                anyhow::bail!("等待后端上报端口超时（60s）。stderr 尾部:\n    {}", describe_exit(&stderr_tail))
            }
        };

        Self::wait_health(port).await?;
        println!(">>> Python 后端就绪，端口 {}", port);

        let mut guard = this.lock().unwrap();
        guard.child = Some(child);
        guard.port = port;
        Ok(port)
    }

    /// 守护循环：进程退出或健康探测连续失败 → 记录死因并退避重启。
    ///
    /// 返回值只是"守护结束"（stopping 或达到重启上限），不是错误。
    pub async fn supervise(this: Arc<Mutex<Self>>, app_handle: AppHandle) {
        let mut health_fails: u32 = 0;
        loop {
            tokio::time::sleep(SUPERVISE_INTERVAL).await;

            let (stopping, port, restarts) = {
                let guard = this.lock().unwrap();
                (
                    guard.stopping.load(Ordering::SeqCst),
                    guard.port,
                    guard.restarts,
                )
            };
            if stopping {
                return;
            }

            // 1) 进程级检测：try_wait 不会消费句柄
            let exited: Option<String> = {
                let mut guard = this.lock().unwrap();
                match guard.child.as_mut() {
                    Some(c) => match c.try_wait() {
                        Ok(Some(status)) => Some(format!(
                            "退出码 {:?}",
                            status.code()
                        )),
                        Ok(None) => None,
                        Err(e) => Some(format!("try_wait 失败: {}", e)),
                    },
                    None => Some("句柄缺失".to_string()),
                }
            };

            // 2) 健康探测（进程活着但卡死也能被发现）
            let healthy = if exited.is_none() {
                Self::probe(port).await
            } else {
                false
            };
            if healthy {
                health_fails = 0;
                continue;
            }
            health_fails += 1;

            let process_dead = exited.is_some();
            if !process_dead && health_fails < HEALTH_FAILS_BEFORE_RESTART {
                eprintln!(
                    ">>> 后端健康探测失败 {}/{}（端口 {}）",
                    health_fails, HEALTH_FAILS_BEFORE_RESTART, port
                );
                continue;
            }

            // ── 触发重启 ──
            let reason = match &exited {
                Some(s) => format!("后端进程已退出（{}）", s),
                None => format!(
                    "后端健康探测连续失败 {} 次（进程在但无响应）",
                    health_fails
                ),
            };
            let tail = {
                let guard = this.lock().unwrap();
                guard.stderr_tail.clone()
            };
            let stderr_hint = describe_exit(&tail);
            if restarts >= MAX_RESTARTS {
                eprintln!(
                    ">>> {}。已自动重启 {} 次，达到上限，不再重启。\n    stderr 尾部:\n    {}",
                    reason, restarts, stderr_hint
                );
                return;
            }

            eprintln!(
                ">>> {}。准备第 {} 次自动重启（会重新选端口）。\n    stderr 尾部:\n    {}",
                reason,
                restarts + 1,
                stderr_hint
            );
            // 清掉死掉的句柄与上一轮的 stderr 尾部
            let tail = {
                let mut guard = this.lock().unwrap();
                guard.child = None;
                guard.stderr_tail.clone()
            };
            if let Ok(mut t) = tail.lock() {
                t.clear();
            }
            // 退避：2s、5s、10s、20s、30s
            let backoff = [2u64, 5, 10, 20, 30]
                .get(restarts as usize)
                .copied()
                .unwrap_or(30);
            tokio::time::sleep(Duration::from_secs(backoff)).await;

            match Self::start(&this, &app_handle).await {
                Ok(new_port) => {
                    let mut guard = this.lock().unwrap();
                    guard.restarts += 1;
                    drop(guard);
                    health_fails = 0;
                    println!(">>> 自动重启成功，新端口 {}", new_port);
                }
                Err(e) => {
                    let mut guard = this.lock().unwrap();
                    guard.restarts += 1;
                    drop(guard);
                    eprintln!(">>> 自动重启失败：{:#}", e);
                    // 失败也会回到循环，下次再试（受 MAX_RESTARTS 约束）
                }
            }
        }
    }

    /// App 退出时调用：置停止标志并杀掉子进程（进程可能已被守护杀过，幂等）。
    pub async fn stop_shared(this: &Arc<Mutex<Self>>) {
        let taken_child = {
            let mut guard = this.lock().unwrap();
            guard.stopping.store(true, Ordering::SeqCst);
            guard.child.take()
        };
        if let Some(mut child) = taken_child {
            println!(">>> 正在停止 Python 后端...");
            let _ = child.kill().await;
            println!(">>> Python 后端已停止");
        }
    }

    /// 单次健康探测（2s 超时）。
    async fn probe(port: u16) -> bool {
        let client = reqwest::Client::new();
        match client
            .get(format!("http://127.0.0.1:{}/api/health", port))
            .timeout(Duration::from_secs(2))
            .send()
            .await
        {
            Ok(resp) => resp.status().is_success(),
            Err(_) => false,
        }
    }

    async fn wait_health(port: u16) -> anyhow::Result<()> {
        for _ in 0..60 {
            if Self::probe(port).await {
                return Ok(());
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
        anyhow::bail!("后端健康检查超时（端口 {}，30s 内 /api/health 未就绪）", port)
    }

    fn data_dir() -> anyhow::Result<String> {
        let dir = if cfg!(windows) {
            std::env::var("APPDATA")?
        } else if cfg!(target_os = "macos") {
            let home = std::env::var("HOME")?;
            format!("{}/Library/Application Support", home)
        } else {
            std::env::var("XDG_DATA_HOME").unwrap_or_else(|_| {
                std::env::var("HOME")
                    .map(|h| format!("{}/.local/share", h))
                    .unwrap_or_else(|_| "/tmp/.local/share".to_string())
            })
        };
        let path = PathBuf::from(dir).join("lingxi");
        std::fs::create_dir_all(&path)?;
        Ok(path.to_string_lossy().to_string())
    }
}

// ── Tauri 命令：前端运行时发现后端地址与令牌 ──

#[tauri::command]
pub fn get_backend_port(state: tauri::State<'_, SidecarState>) -> u16 {
    state.0.lock().map(|g| g.port).unwrap_or(DEFAULT_PORT)
}

#[tauri::command]
pub fn get_admin_token(state: tauri::State<'_, SidecarState>) -> String {
    state
        .0
        .lock()
        .map(|g| g.admin_token.clone())
        .unwrap_or_default()
}
