//! The Gesture Control window. This shell does four things and no more:
//! opens the window, keeps one copy of it, finds or starts the engine, and
//! hands the engine's port and token to the web frontend. All app logic is in
//! the Python engine (files 02 to 04) and the frontend in `../src`.

use std::fs;
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::Manager;

const START_WAIT: Duration = Duration::from_secs(15);

#[derive(Deserialize)]
struct EngineFile {
    port: u16,
    token: String,
    #[serde(default)]
    version: u32,
}

#[derive(Serialize)]
struct EngineInfo {
    port: u16,
    token: String,
    version: u32,
}

/// Where the engine and this app leave notes for each other: `engine.json`,
/// `ui.json` and `engine.log`. A per-user temporary folder, the same one
/// `store.runtime_dir` works out in the Python code. It is not the config
/// folder, because some Python installs redirect files written under AppData.
fn runtime_dir() -> PathBuf {
    let var = |name: &str| std::env::var(name).ok().filter(|value| !value.is_empty());
    if let Some(home) = var("GESTURE_CONTROL_HOME") {
        return PathBuf::from(home);
    }
    if cfg!(target_os = "windows") {
        return var("TEMP")
            .map(PathBuf::from)
            .unwrap_or_else(std::env::temp_dir)
            .join("gesture_control");
    }
    if let Some(shared) = var("XDG_RUNTIME_DIR").or_else(|| var("TMPDIR")) {
        return PathBuf::from(shared).join("gesture_control");
    }
    PathBuf::from("/tmp").join(format!(
        "gesture_control-{}",
        var("USER").unwrap_or_else(|| "user".into())
    ))
}

/// What `engine.json` says, if an engine is really listening there. A file
/// left behind by an engine that died does not count.
fn running_engine() -> Option<EngineInfo> {
    let text = fs::read_to_string(runtime_dir().join("engine.json")).ok()?;
    let file: EngineFile = serde_json::from_str(&text).ok()?;
    let address = SocketAddr::from(([127, 0, 0, 1], file.port));
    TcpStream::connect_timeout(&address, Duration::from_millis(400)).ok()?;
    Some(EngineInfo { port: file.port, token: file.token, version: file.version })
}

/// The command that starts the engine, and the folder to start it in.
fn engine_command(app: &tauri::AppHandle) -> Result<(Command, Option<PathBuf>), String> {
    // 1. An explicit path, for unusual setups.
    if let Ok(path) = std::env::var("GESTURE_CONTROL_ENGINE") {
        if !path.is_empty() {
            return Ok((Command::new(path), None));
        }
    }
    // 2. The frozen engine shipped inside the bundle (file 01, Packaging).
    let name = if cfg!(target_os = "windows") {
        "gesture-control-engine.exe"
    } else {
        "gesture-control-engine"
    };
    if let Ok(resources) = app.path().resource_dir() {
        let bundled = resources.join("engine").join(name);
        if bundled.exists() {
            return Ok((Command::new(bundled), None));
        }
    }
    // 3. Development: the Python package next to this project.
    if cfg!(debug_assertions) {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
        let venv = if cfg!(target_os = "windows") {
            root.join(".venv").join("Scripts").join("python.exe")
        } else {
            root.join(".venv").join("bin").join("python")
        };
        let mut command = Command::new(if venv.exists() { venv } else { PathBuf::from("python") });
        command.args(["-m", "gesture_control"]);
        return Ok((command, Some(root)));
    }
    Err("The engine is not installed with this app.".into())
}

/// Start the engine so that it outlives this window: its own process group,
/// no console, and output in `engine.log` instead of a pipe we would hold.
fn start_engine(app: &tauri::AppHandle) -> Result<(), String> {
    let (mut command, folder) = engine_command(app)?;
    let dir = runtime_dir();
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("engine.log"))
        .map_err(|e| e.to_string())?;
    let errors = log.try_clone().map_err(|e| e.to_string())?;
    if let Some(folder) = folder {
        command.current_dir(folder);
    }
    command.stdin(Stdio::null()).stdout(log).stderr(errors);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    command.spawn().map(|_| ()).map_err(|e| format!("Could not start the engine: {e}"))
}

/// Port and token of the engine. With `start`, an engine that is not running
/// is started first; without it we only look, so an engine the user quit from
/// the tray stays quit until they ask for it again.
/// Runs off the main thread, because it can wait several seconds.
#[tauri::command(async)]
fn engine_info(app: tauri::AppHandle, start: bool) -> Result<EngineInfo, String> {
    if let Some(info) = running_engine() {
        return Ok(info);
    }
    if !start {
        return Err("The engine stopped or was quit.".into());
    }
    start_engine(&app)?;
    let end = Instant::now() + START_WAIT;
    while Instant::now() < end {
        sleep(Duration::from_millis(150));
        if let Some(info) = running_engine() {
            return Ok(info);
        }
    }
    Err(format!(
        "The engine did not start. Its output is in {}",
        runtime_dir().join("engine.log").display()
    ))
}

/// Tell the engine where this app is, so its tray menu can open the window.
fn remember_ui_path() {
    let Ok(exe) = std::env::current_exe() else { return };
    let dir = runtime_dir();
    if fs::create_dir_all(&dir).is_ok() {
        let body = serde_json::json!({ "exe": exe.to_string_lossy() });
        let _ = fs::write(dir.join("ui.json"), body.to_string());
    }
}

fn show_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // A second launch (for example "Open window" in the tray) shows this one.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .setup(|_app| {
            remember_ui_path();
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![engine_info])
        .run(tauri::generate_context!())
        .expect("error while running Gesture Control");
}
