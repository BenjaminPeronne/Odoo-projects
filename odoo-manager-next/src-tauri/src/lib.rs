use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;

struct BackendProcess {
    child: Mutex<Option<Child>>,
    log_path: PathBuf,
}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        if let Ok(process) = self.child.get_mut() {
            if let Some(mut child) = process.take() {
                terminate_child(&mut child);
            }
        }
    }
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

fn backend_executable_path() -> Result<PathBuf, String> {
    let current = std::env::current_exe()
        .map_err(|error| format!("Chemin de l'application introuvable: {error}"))?;
    let directory = current
        .parent()
        .ok_or_else(|| "Dossier de l'application introuvable.".to_string())?;
    let executable = if cfg!(windows) {
        "odoo-manager-backend.exe"
    } else {
        "odoo-manager-backend"
    };
    Ok(directory.join(executable))
}

fn launch_backend(log_dir: &Path, log_path: &Path) -> Option<Child> {
    let executable = match backend_executable_path() {
        Ok(executable) => executable,
        Err(error) => {
            append_backend_log(log_path, &error);
            return None;
        }
    };
    append_backend_log(
        log_path,
        &format!("Sidecar attendu: {}", executable.display()),
    );
    if !executable.is_file() {
        append_backend_log(log_path, "Le fichier du sidecar est absent.");
        return None;
    }

    let stdout = match OpenOptions::new().create(true).append(true).open(log_path) {
        Ok(file) => file,
        Err(error) => {
            append_backend_log(log_path, &format!("Journal stdout indisponible: {error}"));
            return None;
        }
    };
    let stderr = match stdout.try_clone() {
        Ok(file) => file,
        Err(error) => {
            append_backend_log(log_path, &format!("Journal stderr indisponible: {error}"));
            return None;
        }
    };

    let mut command = Command::new(&executable);
    command
        .env("ODOO_MANAGER_LOG_DIR", log_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }

    match command.spawn() {
        Ok(child) => {
            append_backend_log(log_path, &format!("Sidecar démarré, PID {}.", child.id()));
            Some(child)
        }
        Err(error) => {
            append_backend_log(
                log_path,
                &format!("Impossible de démarrer le sidecar: {error}"),
            );
            None
        }
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

fn backend_health_is_ready() -> bool {
    let Ok(address) = "127.0.0.1:8765".parse::<SocketAddr>() else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(500)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    let request = b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n\r\n";
    if stream.write_all(request).is_err() {
        return false;
    }
    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return false;
    }
    (response.starts_with("HTTP/1.0 200") || response.starts_with("HTTP/1.1 200"))
        && response.contains("\"ok\": true")
}

fn terminate_child(child: &mut Child) {
    for _ in 0..20 {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => std::thread::sleep(Duration::from_millis(100)),
            Err(_) => break,
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn stop_backend(app: &tauri::AppHandle) {
    request_backend_shutdown();
    let state = app.state::<BackendProcess>();
    if let Ok(mut process) = state.child.lock() {
        if let Some(mut child) = process.take() {
            terminate_child(&mut child);
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
    let mut process = Command::new(command);
    process.args(args);
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        process.creation_flags(0x0800_0000);
    }
    process
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("Impossible d'exécuter {command}: {error}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![backend_diagnostics, open_external_url, open_docker_desktop])
        .setup(|app| {
            let log_dir = app.path().app_log_dir()?;
            fs::create_dir_all(&log_dir)?;
            let log_path = log_dir.join("backend.log");
            let _ = prepare_backend_log(&log_path);

            let child = launch_backend(&log_dir, &log_path);
            let probe_log_path = log_path.clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_secs(8));
                let status = if backend_health_is_ready() {
                    "API /api/health opérationnelle après le lancement."
                } else {
                    "API /api/health toujours indisponible 8 secondes après le lancement du sidecar."
                };
                append_backend_log(&probe_log_path, status);
            });
            app.manage(BackendProcess {
                child: Mutex::new(child),
                log_path,
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                stop_backend(window.app_handle());
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
