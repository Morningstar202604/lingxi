use std::path::PathBuf;
use tauri::{AppHandle, Manager};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command as TokioCommand;

pub struct PythonSidecar {
    pub child: Option<tokio::process::Child>,
    pub executable: PathBuf,
    pub admin_token: String,
    pub port: u16,
}

impl PythonSidecar {
    pub fn new(app_handle: &AppHandle) -> anyhow::Result<Self> {
        let executable = app_handle
            .path()
            .resolve("lingxi-server", tauri::path::BaseDirectory::Resource)?
            .with_extension(if cfg!(windows) { "exe" } else { "" });

        let admin_token = std::env::var("LINGXI_ADMIN_TOKEN")
            .or_else(|_| std::env::var("LINGXI_SECURITY__ADMIN_TOKEN"))
            .unwrap_or_else(|_| "change-me-in-production".to_string());

        let port = 8080;

        Ok(Self {
            child: None,
            executable,
            admin_token,
            port,
        })
    }

    pub async fn start(&mut self, app_handle: AppHandle) -> anyhow::Result<()> {
        println!(">>> Starting Python sidecar: {:?}", self.executable);

        let mut cmd = TokioCommand::new(&self.executable);
        cmd.env("LINGXI_SECURITY__ADMIN_TOKEN", &self.admin_token)
            .env("LINGXI_DASHBOARD_PORT", self.port.to_string())
            .env("LINGXI_DATA_DIR", Self::data_dir()?)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true);

        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000);
        }

        let mut child = cmd.spawn()?;

        if let Some(stdout) = child.stdout.take() {
            let app = app_handle.clone();
            tokio::spawn(async move {
                let reader = BufReader::new(stdout).lines();
                let mut lines = reader;
                while let Some(line) = lines.next_line().await.unwrap_or(None) {
                    println!("[lingxi-server] {}", line);
                }
            });
        }

        if let Some(stderr) = child.stderr.take() {
            tokio::spawn(async move {
                let reader = BufReader::new(stderr).lines();
                let mut lines = reader;
                while let Some(line) = lines.next_line().await.unwrap_or(None) {
                    eprintln!("[lingxi-server ERR] {}", line);
                }
            });
        }

        self.child = Some(child);

        Self::wait_health(self.port).await?;
        println!(">>> Python sidecar started on port {}", self.port);
        Ok(())
    }

    /// 静态方法：不持有 self 锁的独立启动（用于 Tauri setup 中的 spawn）
    pub async fn start_standalone(
        executable: PathBuf,
        admin_token: String,
        port: u16,
        app_handle: AppHandle,
    ) -> anyhow::Result<()> {
        println!(">>> Starting Python sidecar: {:?}", executable);

        let mut cmd = TokioCommand::new(&executable);
        cmd.env("LINGXI_SECURITY__ADMIN_TOKEN", &admin_token)
            .env("LINGXI_DASHBOARD_PORT", port.to_string())
            .env("LINGXI_DATA_DIR", Self::data_dir()?)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true);

        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000);
        }

        let mut child = cmd.spawn()?;

        if let Some(stdout) = child.stdout.take() {
            let app = app_handle.clone();
            tokio::spawn(async move {
                let reader = BufReader::new(stdout).lines();
                let mut lines = reader;
                while let Some(line) = lines.next_line().await.unwrap_or(None) {
                    println!("[lingxi-server] {}", line);
                }
            });
        }

        if let Some(stderr) = child.stderr.take() {
            tokio::spawn(async move {
                let reader = BufReader::new(stderr).lines();
                let mut lines = reader;
                while let Some(line) = lines.next_line().await.unwrap_or(None) {
                    eprintln!("[lingxi-server ERR] {}", line);
                }
            });
        }

        Self::wait_health(port).await?;
        println!(">>> Python sidecar started on port {}", port);

        // child 设置了 kill_on_drop，析构时自动清理
        // 这里不存入 self.child，由外部管理
        drop(child);
        Ok(())
    }

    async fn wait_health(port: u16) -> anyhow::Result<()> {
        let client = reqwest::Client::new();
        let url = format!("http://127.0.0.1:{}/api/health", port);

        for _ in 0..30 {
            if let Ok(resp) = client.get(&url).send().await {
                if resp.status().is_success() {
                    return Ok(());
                }
            }
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        }
        anyhow::bail!("Python sidecar health check timeout on port {}", port)
    }

    fn data_dir() -> anyhow::Result<String> {
        let dir = if cfg!(windows) {
            std::env::var("APPDATA")?
        } else if cfg!(target_os = "macos") {
            let home = std::env::var("HOME")?;
            format!("{}/Library/Application Support", home)
        } else {
            std::env::var("XDG_DATA_HOME")
                .unwrap_or_else(|_| {
                    std::env::var("HOME")
                        .map(|h| format!("{}/.local/share", h))
                        .unwrap_or_else(|_| "/tmp/.local/share".to_string())
                })
        };
        let path = PathBuf::from(dir).join("lingxi");
        std::fs::create_dir_all(&path)?;
        Ok(path.to_string_lossy().to_string())
    }

    pub async fn stop(&mut self) -> anyhow::Result<()> {
        if let Some(mut child) = self.child.take() {
            println!(">>> Stopping Python sidecar...");
            child.kill().await?;
            println!(">>> Python sidecar stopped");
        }
        Ok(())
    }
}