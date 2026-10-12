use std::path::PathBuf;
use tauri::{Manager, Runtime};
use tauri_plugin_shell::ShellExt;
use tokio::process::Command as TokioCommand;

pub struct PythonSidecar {
    child: Option<tokio::process::Child>,
    executable: PathBuf,
    admin_token: String,
    port: u16,
}

impl PythonSidecar {
    pub fn new(app_handle: &tauri::AppHandle) -> anyhow::Result<Self> {
        // 从资源目录解析 sidecar 路径
        let executable = app_handle
            .path()
            .resolve("lingxi-server", tauri::path::BaseDirectory::Resource)?
            .with_extension(if cfg!(windows) { "exe" } else { "" });

        // 读取 admin token（开发环境从环境变量，生产环境从配置文件）
        let admin_token = std::env::var("LINGXI_ADMIN_TOKEN")
            .or_else(|_| std::env::var("LINGXI_SECURITY__ADMIN_TOKEN"))
            .unwrap_or_else(|_| "change-me-in-production".to_string());

        // 固定端口（也可随机选取并写入配置文件）
        let port = 8080;

        Ok(Self {
            child: None,
            executable,
            admin_token,
            port,
        })
    }

    pub async fn start(&mut self, app_handle: tauri::AppHandle) -> anyhow::Result<()> {
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
            cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }

        let mut child = cmd.spawn()?;

        // 读取 stdout 到日志
        if let Some(stdout) = child.stdout.take() {
            let app = app_handle.clone();
            tokio::spawn(async move {
                use tokio::io::{AsyncBufReadExt, BufReader};
                let reader = BufReader::new(stdout).lines();
                for await line in reader {
                    if let Ok(l) = line {
                        println!("[lingxi-server] {}", l);
                    }
                }
            });
        }

        // 读取 stderr 到日志
        if let Some(stderr) = child.stderr.take() {
            tokio::spawn(async move {
                use tokio::io::{AsyncBufReadExt, BufReader};
                let reader = BufReader::new(stderr).lines();
                for await line in reader {
                    if let Ok(l) = line {
                        eprintln!("[lingxi-server ERR] {}", l);
                    }
                }
            });
        }

        self.child = Some(child);

        // 等待健康检查通过
        Self::wait_health(self.port).await?;
        println!(">>> Python sidecar started on port {}", self.port);
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
                .unwrap_or_else(|_| format!("{}/.local/share", std::env::var("HOME")?))
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