#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // SAFETY: CoInitializeEx must run before any COM/UIA call, before Tauri builder
    unsafe {
        windows::Win32::System::Com::CoInitializeEx(
            None,
            windows::Win32::System::Com::COINIT_APARTMENTTHREADED,
        )
        .ok()
        .expect("CoInitializeEx STA init failed");
    }

    vox_lib::run();
}
