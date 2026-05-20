/// audio.rs — Windows mic volume auto-boost via IAudioEndpointVolume.
use windows::core::HRESULT;
use windows::Win32::Media::Audio::{
    eCapture, eConsole, IMMDeviceEnumerator, MMDeviceEnumerator,
};
use windows::Win32::Media::Audio::Endpoints::IAudioEndpointVolume;
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL,
    COINIT_APARTMENTTHREADED,
};
use windows::Win32::System::Com::StructuredStorage::PROPVARIANT;

fn read_and_boost_mic() -> Result<(u32, bool), String> {
    unsafe {
        let com_init = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
        let already_init = matches!(&com_init, Err(e) if e.code() == HRESULT(1));
        if let Err(ref e) = com_init {
            if e.code() != HRESULT(1) {
                return Err(format!("CoInitializeEx: {e}"));
            }
        }

        let result = (|| -> Result<(u32, bool), String> {
            let enumerator: IMMDeviceEnumerator =
                CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
                    .map_err(|e| format!("IMMDeviceEnumerator: {e}"))?;

            let device = enumerator
                .GetDefaultAudioEndpoint(eCapture, eConsole)
                .map_err(|e| format!("GetDefaultAudioEndpoint: {e}"))?;

            let no_params: Option<*const PROPVARIANT> = None;
            let vol: IAudioEndpointVolume = device
                .Activate(CLSCTX_ALL, no_params)
                .map_err(|e| format!("Activate: {e}"))?;

            let current = vol
                .GetMasterVolumeLevelScalar()
                .map_err(|e| format!("GetMasterVolumeLevelScalar: {e}"))?;

            let pct = (current * 100.0).round() as u32;
            let mut boosted = false;

            if current < 0.80 {
                vol.SetMasterVolumeLevelScalar(1.0, std::ptr::null())
                    .map_err(|e| format!("SetMasterVolumeLevelScalar: {e}"))?;
                boosted = true;
            }
            Ok((pct, boosted))
        })();

        if !already_init {
            CoUninitialize();
        }
        result
    }
}

#[tauri::command]
pub fn ensure_mic_volume() -> String {
    match read_and_boost_mic() {
        Ok((pct, true)) => format!(
            "[audio] mic was at {pct}% — boosted to 100%"
        ),
        Ok((pct, false)) => format!("[audio] mic volume OK ({pct}%)"),
        Err(e) => format!("[audio] mic volume check failed: {e}"),
    }
}
