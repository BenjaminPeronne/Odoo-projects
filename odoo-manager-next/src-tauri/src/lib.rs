use std::sync::Mutex;
use std::process::Command;
use tauri::Manager;
use tauri_plugin_shell::{process::CommandChild, ShellExt};

struct BackendProcess(Mutex<Option<CommandChild>>);

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
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![open_external_url, open_docker_desktop])
        .setup(|app| {
            let command = app.shell().sidecar("odoo-manager-backend")?;
            let (_events, child) = command.spawn()?;
            app.manage(BackendProcess(Mutex::new(Some(child))));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                let state = window.state::<BackendProcess>();
                if let Ok(mut process) = state.0.lock() {
                    if let Some(child) = process.take() {
                        let _ = child.kill();
                    }
                };
            }
        })
        .run(tauri::generate_context!())
        .expect("impossible de lancer Odoo Manager");
}
