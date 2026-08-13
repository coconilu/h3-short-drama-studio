#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::process::Command;
use tauri::{WebviewUrl, WebviewWindowBuilder};

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn project_root() -> Result<PathBuf, String> {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|frontend| frontend.parent())
        .map(PathBuf::from)
        .ok_or_else(|| "无法定位镜场项目目录".to_string())
}

fn ensure_services() -> Result<(), String> {
    let root = project_root()?;
    let start_script = root.join("scripts").join("start.ps1");
    if !start_script.is_file() {
        return Err(format!("启动脚本不存在：{}", start_script.display()));
    }
    let mut command = Command::new("powershell.exe");
    command
        .arg("-NoProfile")
        .arg("-ExecutionPolicy")
        .arg("Bypass")
        .arg("-File")
        .arg(&start_script)
        .arg("-NoBrowser")
        .current_dir(&root);
    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);
    let output = command
        .output()
        .map_err(|error| format!("无法启动本机服务：{error}"))?;
    if output.status.success() {
        Ok(())
    } else {
        let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
        Err(if detail.is_empty() {
            "本机服务未能在 30 秒内就绪，请检查 runtime 日志".to_string()
        } else {
            detail
        })
    }
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            ensure_services()
                .map_err(|message| -> Box<dyn std::error::Error> { message.into() })?;
            let url = "http://127.0.0.1:4173/"
                .parse()
                .map_err(|error| format!("工作台地址无效：{error}"))?;
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
                .title("镜场 · H3 短剧工作台")
                .inner_size(1440.0, 960.0)
                .min_inner_size(1080.0, 720.0)
                .center()
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("镜场桌面客户端运行失败");
}
