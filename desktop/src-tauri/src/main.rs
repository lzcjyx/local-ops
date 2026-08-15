//! 总控台桌面壳（M9）。
//!
//! 职责：启动/连接本地 ADCC daemon，用 webview 承载 daemon 的既有 UI，
//! 托盘常驻，关闭窗口只隐藏不退出。**不承载任何运行时业务逻辑**
//! （进程/端口/编排全在 daemon 侧，SPEC §19.1）。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::Command;
use std::time::{Duration, Instant};

use serde::Deserialize;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconEvent;
use tauri::Manager;
use tauri_plugin_notification::NotificationExt;

const POLL_TIMEOUT_SECS: u64 = 15;
const DAEMON_JSON: &str = "daemon.json";

#[derive(Deserialize)]
struct DaemonEndpoint {
    port: u16,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    token: Option<String>,
}

// ---------------------------------------------------------------- 数据目录

fn data_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("CONSOLE_DATA_DIR") {
        let path = PathBuf::from(dir);
        if path.is_absolute() {
            return path;
        }
    }
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".to_string());
    if cfg!(windows) {
        let appdata = std::env::var("APPDATA")
            .unwrap_or_else(|_| home);
        PathBuf::from(appdata).join("总控台")
    } else {
        PathBuf::from(home).join("Library/Application Support/总控台")
    }
}

fn endpoint_path() -> PathBuf {
    data_dir().join(DAEMON_JSON)
}

fn read_endpoint() -> Option<DaemonEndpoint> {
    let text = std::fs::read_to_string(endpoint_path()).ok()?;
    serde_json::from_str(&text).ok()
}

// ---------------------------------------------------------------- 健康检查

/// 最小 loopback HTTP GET（避免引入 reqwest）。
fn http_health(port: u16) -> bool {
    let mut stream = match TcpStream::connect(("127.0.0.1", port)) {
        Ok(s) => s,
        Err(_) => return false,
    };
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .ok();
    let request = format!(
        "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
        port
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut buffer = [0u8; 256];
    let mut read = 0;
    loop {
        match stream.read(&mut buffer[read..]) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                read += n;
                if read >= buffer.len() {
                    break;
                }
            }
        }
    }
    let head = String::from_utf8_lossy(&buffer[..read]);
    head.contains(" 200 ")
}

fn find_daemon() -> Option<u16> {
    let endpoint = read_endpoint()?;
    if http_health(endpoint.port) {
        return Some(endpoint.port);
    }
    None
}

// ---------------------------------------------------------------- daemon 启动

fn daemon_python() -> &'static str {
    if cfg!(windows) {
        "python"
    } else {
        "python3"
    }
}

/// daemon 脚本位置：开发时是仓库根，打包后在资源目录。
fn daemon_script(app: &tauri::AppHandle) -> PathBuf {
    if let Ok(resource_dir) = app.path().resource_dir() {
        let bundled = resource_dir.join("server.py");
        if bundled.is_file() {
            return bundled;
        }
    }
    // 开发模式：仓库根 = src-tauri 的上上级目录
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.parent())
        .map(|p| p.to_path_buf())
        .unwrap_or_default()
        .join("server.py")
}

/// 启动 daemon 并轮询健康；返回 (端口, 是否由本壳启动)。
fn ensure_daemon(app: &tauri::AppHandle) -> Result<(u16, bool), String> {
    if let Some(port) = find_daemon() {
        return Ok((port, false));
    }
    let script = daemon_script(app);
    if !script.is_file() {
        return Err(format!("找不到 daemon 脚本: {}", script.display()));
    }
    let data = data_dir();
    if let Err(err) = std::fs::create_dir_all(&data) {
        return Err(format!("无法创建数据目录: {}", err));
    }
    let mut command = Command::new(daemon_python());
    command
        .arg(&script)
        .arg("--no-browser")
        .env("CONSOLE_DATA_DIR", &data);
    match command.spawn() {
        Ok(_) => {}
        Err(err) => {
            return Err(format!(
                "无法启动 daemon（{} {}）: {}",
                daemon_python(),
                script.display(),
                err
            ));
        }
    }
    let deadline = Instant::now() + Duration::from_secs(POLL_TIMEOUT_SECS);
    while Instant::now() < deadline {
        if let Some(port) = find_daemon() {
            return Ok((port, true));
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    Err("daemon 启动超时（15s），请检查日志".to_string())
}

// ---------------------------------------------------------------- UI

fn navigate(window: &tauri::WebviewWindow, port: u16) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{}/", port);
    let parsed = url::Url::parse(&url).map_err(|e| e.to_string())?;
    window
        .navigate(parsed)
        .map_err(|e| format!("导航失败: {}", e))
}

fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "打开控制台", true, None::<&str>)?;
    let data_dir_item =
        MenuItem::with_id(app, "open-data-dir", "打开数据目录", true, None::<&str>)?;
    let restart = MenuItem::with_id(app, "restart-daemon", "重启 daemon", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &open,
            &data_dir_item,
            &restart,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;
    if let Some(tray) = app.tray_by_id("main") {
        tray.set_menu(Some(menu))?;
        tray.on_menu_event(|app, event| match event.id().as_ref() {
            "open" => {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.unminimize();
                    let _ = window.set_focus();
                }
            }
            "open-data-dir" => {
                let _ = open_path(&data_dir());
            }
            "restart-daemon" => {
                let _ = restart_daemon(app);
            }
            "quit" => {
                app.exit(0);
            }
            _ => {}
        });
        tray.on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: tauri::tray::MouseButton::Left,
                button_state: tauri::tray::MouseButtonState::Up,
                ..
            } = event
            {
                let app = tray.app_handle();
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
        });
    }
    Ok(())
}

#[cfg(target_os = "windows")]
fn open_path(path: &PathBuf) -> Result<(), String> {
    Command::new("explorer")
        .arg(path)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

#[cfg(not(target_os = "windows"))]
fn open_path(path: &PathBuf) -> Result<(), String> {
    Command::new("open")
        .arg(path)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

#[allow(dead_code)]
fn restart_daemon(app: &tauri::AppHandle) -> Result<(), String> {
    // 仅重启 daemon：独立进程组中的受管服务不受影响。
    let port = ensure_daemon(app).map(|(port, _)| port)?;
    let _ = app
        .notification()
        .builder()
        .title("总控台")
        .body(format!("daemon 已就绪（:{}）", port))
        .show();
    if let Some(window) = app.get_webview_window("main") {
        let _ = navigate(&window, port);
    }
    Ok(())
}

// ---------------------------------------------------------------- 入口

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .setup(|app| {
            let (port, started) = match ensure_daemon(app.handle()) {
                Ok(result) => result,
                Err(error) => {
                    let _ = app
                        .notification()
                        .builder()
                        .title("总控台")
                        .body(error.clone())
                        .show();
                    eprintln!("daemon 不可用: {}", error);
                    return Ok(());
                }
            };
            build_tray(app)?;
            if let Some(window) = app.get_webview_window("main") {
                if let Err(error) = navigate(&window, port) {
                    eprintln!("导航失败: {}", error);
                }
            }
            let _ = app
                .notification()
                .builder()
                .title("总控台")
                .body(if started {
                    format!("daemon 已启动（:{}）", port)
                } else {
                    format!("已连接 daemon（:{}）", port)
                })
                .show();
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                // 关闭窗口只隐藏：daemon 与受管服务继续运行
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("总控台桌面壳运行失败");
}
