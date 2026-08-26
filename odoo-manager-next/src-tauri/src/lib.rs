use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

struct BackendProcess {
    child: Mutex<Option<CommandChild>>,
    log_path: PathBuf,
}

#[derive(serde::Serialize)]
struct BackendDiagnostics {
    log_path: String,
    details: String,
}

fn prepare_backend_log(log_path: &Path) -> std::io::Result<()> {
    if log_path
        .metadata()
        .map(|metadata| metadata.len() > 2_000_000)
        .unwrap_or(false)
    {
        let previous = log_path.with_file_name("backend.previous.log");
        let _ = fs::remove_file(&previous);
        fs::rename(log_path, previous)?;
    }
    let mut file = OpenOptions::new().create(true).append(true).open(log_path)?;
    writeln!(file, "\n=== Démarrage de l'application Odoo Manager ===")
}

fn append_backend_log(log_path: &Path, message: &str) {
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(log_path) {
        let _ = writeln!(file, "{message}");
    }
}

fn read_backend_log(log_path: &Path) -> String {
    let Ok(mut file) = File::open(log_path) else {
        return "Le journal du backend n'a pas encore été créé.".into();
    };
    let mut content = String::new();
    if file.read_to_string(&mut content).is_err() {
        return "Le journal du backend est illisible.".into();
    }
    let target = content.len().saturating_sub(16_000);
    let start = content
        .char_indices()
        .find_map(|(index, _)| (index >= target).then_some(index))
        .unwrap_or(0);
    content[start..].to_string()
}

fn request_backend_shutdown() {
    let address: SocketAddr = match "127.0.0.1:8765".parse() {
        Ok(address) => address,
        Err(_) => return,
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(250)) else {
        return;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
    let request = b"POST /api/system/shutdown HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
    if stream.write_all(request).is_ok() {
        let mut response = [0_u8; 64];
        let _ = stream.read(&mut response);
        std::thread::sleep(Duration::from_millis(1200));
    }
}

fn stop_backend(app: &tauri::AppHandle) {
    request_backend_shutdown();
    let state = app.state::<BackendProcess>();
    if let Ok(mut process) = state.child.lock() {
        if let Some(child) = process.take() {
            let _ = child.kill();
        }
    };
}

#[tauri::command]
fn backend_diagnostics(app: tauri::AppHandle) -> BackendDiagnostics {
    let state = app.state::<BackendProcess>();
    BackendDiagnostics {
        log_path: state.log_path.display().to_string(),
        details: read_backend_log(&state.log_path),
    }
}

#[tauri::command]
fn open_external_url(url: String) -> Result<(), String> {
    if !url.starts_with("http://") && !url.starts_with("https://") {
        return Err("URL non autorisée.".into());
    }
    open_with_system(&url)
}

#[tauri::command]
fn open_docker_desktop() -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        return run_command("open", &["-a", "Docker"]);
    }

    #[cfg(target_os = "windows")]
    {
        let candidates = [
            std::env::var("ProgramFiles")
                .ok()
                .map(|root| format!("{root}\\Docker\\Docker\\Docker Desktop.exe")),
            std::env::var("LOCALAPPDATA")
                .ok()
                .map(|root| format!("{root}\\Programs\\Docker\\Docker\\Docker Desktop.exe")),
        ];
        for candidate in candidates.into_iter().flatten() {
            if std::path::Path::new(&candidate).exists() {
                return run_command(&candidate, &[]);
            }
        }
        return Err("Docker Desktop est introuvable sur cette machine.".into());
    }

    #[cfg(target_os = "linux")]
    {
        if run_command("systemctl", &["--user", "start", "docker-desktop"]).is_ok() {
            return Ok(());
        }
        Err("Démarre Docker Desktop ou le service Docker depuis le système.".into())
    }
}

fn open_with_system(url: &str) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        return run_command("open", &[url]);
    }

    #[cfg(target_os = "windows")]
    {
        return run_command("cmd", &["/C", "start", "", url]);
    }

    #[cfg(target_os = "linux")]
    {
        return run_command("xdg-open", &[url]);
    }
}

fn run_command(command: &str, args: &[&str]) -> Result<(), String> {
    Command::new(command)
        .args(args)
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("Impossible d'exécuter {command}: {error}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![backend_diagnostics, open_external_url, open_docker_desktop])
        .setup(|app| {
            let log_dir = app.path().app_log_dir()?;
            fs::create_dir_all(&log_dir)?;
            let log_path = log_dir.join("backend.log");
            let _ = prepare_backend_log(&log_path);

            let child = match app.shell().sidecar("odoo-manager-backend") {
                Ok(command) => match command.env("ODOO_MANAGER_LOG_DIR", &log_dir).spawn() {
                    Ok((mut events, child)) => {
                        let event_log_path = log_path.clone();
                        tauri::async_runtime::spawn(async move {
                            let mut log_file = OpenOptions::new()
                                .create(true)
                                .append(true)
                                .open(&event_log_path)
                                .ok();
                            while let Some(event) = events.recv().await {
                                let message = match event {
                                    CommandEvent::Stdout(bytes) => {
                                        String::from_utf8_lossy(&bytes).into_owned()
                                    }
                                    CommandEvent::Stderr(bytes) => {
                                        format!("[stderr] {}", String::from_utf8_lossy(&bytes))
                                    }
                                    CommandEvent::Error(error) => format!("[sidecar error] {error}"),
                                    CommandEvent::Terminated(payload) => format!("[sidecar terminé] {payload:?}"),
                                    _ => continue,
                                };
                                if let Some(file) = log_file.as_mut() {
                                    let _ = writeln!(file, "{message}");
                                }
                            }
                        });
                        Some(child)
                    }
                    Err(error) => {
                        append_backend_log(
                            &log_path,
                            &format!("Impossible de démarrer le sidecar: {error}"),
                        );
                        None
                    }
                },
                Err(error) => {
                    append_backend_log(&log_path, &format!("Sidecar introuvable: {error}"));
                    None
                }
            };
            app.manage(BackendProcess {
                child: Mutex::new(child),
                log_path,
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                window.app_handle().exit(0);
            }
        })
        .build(tauri::generate_context!())
        .expect("impossible de lancer Odoo Manager");
    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }) {
            stop_backend(app_handle);
        }
    });
}
