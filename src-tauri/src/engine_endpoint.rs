//! 引擎地址发现与配置（**全平台编译**）。
//!
//! 🔗 为什么需要这个模块
//! 「智犀」是「客户端 + 引擎服务」架构。引擎本体是 Python 进程：
//! - **桌面**：Tauri 把引擎作为 sidecar 自动拉起，监听 127.0.0.1:<动态端口>，
//!   令牌随机生成 —— 客户端运行时就能拿到真实地址（见 python_sidecar.rs）。
//! - **移动**（Android/iOS）：OS 不允许 fork 任意二进制、也没有 Python
//!   运行时，**引擎不可能跑在手机上**。客户端改为连接用户自己部署的
//!   引擎地址（局域网/公网/VPN 均可），或经由云端 API 网关。
//!
//! 本模块把这两种来源统一成一份「引擎端点」：
//! `{ base_url, token, mode }`，前端只需一条 invoke 就能拿到地址，
//! 不需要知道背后是 sidecar 还是远程服务。
//!
//! 🤖 AI 避坑
//! - 端点是**可变状态**：桌面在 sidecar 重启换端口后会更新；移动端由
//!   用户随时修改。所以用全局 Mutex 而非 OnceLock。
//! - 移动端把配置持久化到 App 数据目录（`engine.json`），重启不丢；
//!   桌面端不落盘（地址由 sidecar 每次启动决定，落盘反而会读到过期值）。
//! - 令牌与地址都属于**敏感配置**：不做任何日志输出。
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::AppHandle;

#[cfg(mobile)]
use tauri::Manager;

#[cfg(mobile)]
use std::path::PathBuf;

/// 全局端点状态。`None` = 尚未配置（首次启动的移动端）。
static ENDPOINT: Mutex<Option<EngineEndpoint>> = Mutex::new(None);

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EngineEndpoint {
    /// 形如 `http://192.168.1.10:8080`（不带尾斜杠）
    pub base_url: String,
    /// 管理令牌（引擎侧 `LINGXI_SECURITY__ADMIN_TOKEN`）
    pub token: String,
    /// "local" = 桌面 sidecar；"remote" = 用户配置的远程引擎
    pub mode: String,
}

impl EngineEndpoint {
    /// 桌面端 sidecar 启动后构造本机端点。
    ///
    /// 移动端不用这个（引擎不在手机上），故 cfg 限定避免 dead_code 警告。
    #[cfg(desktop)]
    pub fn local(port: u16, token: String) -> Self {
        Self {
            base_url: format!("http://127.0.0.1:{}", port),
            token,
            mode: "local".to_string(),
        }
    }

    fn normalize(&mut self) {
        self.base_url = self.base_url.trim().trim_end_matches('/').to_string();
        self.token = self.token.trim().to_string();
    }

    /// 地址基本合法性（只做形状校验，不发起网络请求）。
    pub fn is_valid(&self) -> bool {
        (self.base_url.starts_with("http://") || self.base_url.starts_with("https://"))
            && !self.base_url.ends_with("//")
    }
}

/// 端点文件路径（仅移动端落盘；桌面端内存态即可）。
#[cfg(mobile)]
fn config_path_from(app: &AppHandle) -> Option<PathBuf> {
    let dir = app.path().app_data_dir().ok()?;
    std::fs::create_dir_all(&dir).ok()?;
    Some(dir.join("engine.json"))
}

/// 桌面端由 sidecar 启动流程调用，写入真实端口与令牌。
#[cfg(desktop)]
pub fn set_local(port: u16, token: &str) {
    if let Ok(mut guard) = ENDPOINT.lock() {
        *guard = Some(EngineEndpoint::local(port, token.to_string()));
    }
}

/// 读取当前端点（前端 `invoke("get_engine_endpoint")`）。
///
/// 返回 `None` 表示"尚未配置"——移动端首启时前端应引导用户填写地址。
#[tauri::command]
pub fn get_engine_endpoint(app: AppHandle) -> Option<EngineEndpoint> {
    #[cfg(not(mobile))]
    let _ = app;
    // 内存态优先；移动端冷启动时从磁盘恢复
    if let Ok(guard) = ENDPOINT.lock() {
        if let Some(ep) = guard.as_ref() {
            return Some(ep.clone());
        }
    }
    #[cfg(mobile)]
    {
        if let Some(path) = config_path_from(&app) {
            if let Ok(raw) = std::fs::read_to_string(&path) {
                if let Ok(ep) = serde_json::from_str::<EngineEndpoint>(&raw) {
                    if ep.is_valid() {
                        if let Ok(mut guard) = ENDPOINT.lock() {
                            *guard = Some(ep.clone());
                        }
                        return Some(ep);
                    }
                }
            }
        }
    }
    None
}

/// 设置引擎端点（移动端用户在界面里填写；桌面端可覆盖 sidecar 默认）。
///
/// 返回 `Ok(true)` 表示已保存并可立即生效。
#[tauri::command]
pub fn set_engine_endpoint(
    app: AppHandle,
    base_url: String,
    token: String,
) -> Result<bool, String> {
    #[cfg(not(mobile))]
    let _ = app;
    let mut ep = EngineEndpoint {
        base_url,
        token,
        mode: "remote".to_string(),
    };
    ep.normalize();
    if !ep.is_valid() {
        return Err("地址需以 http:// 或 https:// 开头，例如 http://192.168.1.10:8080".into());
    }

    #[cfg(mobile)]
    if let Some(path) = config_path_from(&app) {
        let raw = serde_json::to_string_pretty(&ep).map_err(|e| e.to_string())?;
        std::fs::write(&path, raw).map_err(|e| format!("保存失败: {}", e))?;
    }

    if let Ok(mut guard) = ENDPOINT.lock() {
        *guard = Some(ep);
    }
    Ok(true)
}

/// 测试两个端点是否可达（用户在设置页点"测试连接"）。
///
/// 走 `/api/health`：该端点不需要凭据，所以即使令牌填错也能区分
/// "地址不通"与"令牌不对"——后者用带令牌的 /api/setup/status 再验一次。
#[tauri::command]
pub async fn probe_engine_endpoint(base_url: String, token: String) -> Result<String, String> {
    let base = base_url.trim().trim_end_matches('/').to_string();
    if !(base.starts_with("http://") || base.starts_with("https://")) {
        return Err("地址需以 http:// 或 https:// 开头".into());
    }
    let client = reqwest::Client::new();

    // 1) 健康检查（无凭据）
    match client
        .get(format!("{}/api/health", base))
        .timeout(std::time::Duration::from_secs(4))
        .send()
        .await
    {
        Ok(resp) if resp.status().is_success() => {}
        Ok(resp) => return Err(format!("地址可达但返回 {}", resp.status())),
        Err(e) => return Err(format!("连不上：{}", e)),
    }

    // 2) 凭据检查（需要令牌）
    if token.trim().is_empty() {
        return Ok("地址连通；未填令牌（管理接口将不可用）".to_string());
    }
    match client
        .get(format!("{}/api/setup/status", base))
        .header("x-admin-token", token.trim())
        .timeout(std::time::Duration::from_secs(4))
        .send()
        .await
    {
        Ok(resp) if resp.status().is_success() => Ok("连接成功，凭据有效".to_string()),
        Ok(resp) if resp.status().as_u16() == 401 || resp.status().as_u16() == 403 => {
            Err("地址连通，但令牌不正确".to_string())
        }
        Ok(resp) => Err(format!("凭据检查返回 {}", resp.status())),
        Err(e) => Err(format!("凭据检查失败：{}", e)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_strips_trailing_slash() {
        let mut ep = EngineEndpoint {
            base_url: "http://10.0.0.5:8080/".into(),
            token: " t ".into(),
            mode: "remote".into(),
        };
        ep.normalize();
        assert_eq!(ep.base_url, "http://10.0.0.5:8080");
        assert_eq!(ep.token, "t");
    }

    #[test]
    fn rejects_non_http_scheme() {
        let ep = EngineEndpoint {
            base_url: "10.0.0.5:8080".into(),
            token: String::new(),
            mode: "remote".into(),
        };
        assert!(!ep.is_valid());
    }

    #[test]
    fn local_endpoint_is_valid() {
        let ep = EngineEndpoint::local(8081, "tok".into());
        assert!(ep.is_valid());
        assert_eq!(ep.base_url, "http://127.0.0.1:8081");
        assert_eq!(ep.mode, "local");
    }
}
