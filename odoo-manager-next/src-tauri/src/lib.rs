use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::process::Command;
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;
use tauri_plugin_shell::{process::CommandChild, ShellExt};

struct BackendProcess(Mutex<Option<CommandChild>>);

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
    if let Ok(mut process) = state.0.lock() {
        if let Some(child) = process.take() {
            let _ = child.kill();
        }
    };
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
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![open_external_url, open_docker_desktop])
        .setup(|app| {
            let command = app.shell().sidecar("odoo-manager-backend")?;
            let (mut events, child) = command.spawn()?;
            tauri::async_runtime::spawn(async move {
                while events.recv().await.is_some() {}
            });
            app.manage(BackendProcess(Mutex::new(Some(child))));
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
