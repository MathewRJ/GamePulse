/// Loaded-module scanner — Tier 2 settings auto-detection (D.8 + Windows).
///
/// Reads the loaded-module list of a running process and infers:
///   - Graphics API  → written to `Target.graphics_api` as an env-based fallback
///   - Upscaler tech → written to `rigsignal.settings.upscaler.tech` in the overlay
///   - Frame-gen tech → written to `rigsignal.settings.frame_gen.tech` in the overlay
///
/// Detection works because games (and Proton) load DLLs/SOs whose names encode
/// the technology: `libdxvk.so.2`, `nvngx_dlss.dll`, `libxess.so`,
/// `ffx_framegeneration_x64.dll`, etc.
///
/// Source of the module list:
///   - Linux:   `/proc/<pid>/maps` (file-backed memory mappings)
///   - Windows: `EnumProcessModules` + `GetModuleFileNameExW` (psapi)
///
/// Env-var detection (session.rs `detect_graphics_api`) is preferred when available
/// on Linux — maps detection is used as a fallback for native games that don't
/// set Wine env vars. On Windows, this scanner is the primary detection path
/// (Wine env vars don't exist).
///
/// All functions are cross-platform: on platforms without an implementation,
/// `read_mapped_paths` returns an empty Vec and every caller returns `None`.
use serde_json::{json, Value};

// ── Public API ────────────────────────────────────────────────────────────────

/// Infer the graphics API from mapped libraries. Returns the same string set
/// as `detect_graphics_api` in `session.rs`.
pub fn graphics_api_from_maps(pid: u32) -> Option<String> {
    let paths = read_mapped_paths(pid);
    detect_graphics_api_from_paths(&paths)
}

/// Build a `{ "rigsignal": { "settings": { … } } }` overlay from maps-detected
/// upscaler and frame-generation technology. Returns `Value::Null` if nothing
/// was detected. Sets `source = "auto_detected"`, `confidence = "medium"`.
pub fn settings_overlay_from_maps(pid: u32) -> Value {
    let paths = read_mapped_paths(pid);
    settings_overlay_from_paths(&paths)
}

fn settings_overlay_from_paths(paths: &[String]) -> Value {
    let upscaler = detect_upscaler_from_paths(paths);
    let frame_gen = detect_frame_gen_from_paths(paths);

    let mut settings = serde_json::Map::new();

    if let Some(tech) = upscaler {
        settings.insert("upscaler".into(), json!({ "tech": tech }));
    }
    if let Some(fg) = frame_gen {
        settings.insert("frame_gen".into(), json!({ "tech": fg }));
    }

    if settings.is_empty() {
        return Value::Null;
    }

    settings.insert("source".into(), json!("auto_detected"));
    settings.insert("confidence".into(), json!("medium"));

    json!({ "rigsignal": { "settings": Value::Object(settings) } })
}

// ── Detection logic ───────────────────────────────────────────────────────────

/// Detect graphics API from a list of mapped file paths.
/// Detection order is most-specific → least-specific to handle games that
/// use Vulkan as a backend (e.g. DXVK and VKD3D both map libvulkan.so).
pub(crate) fn detect_graphics_api_from_paths(paths: &[String]) -> Option<String> {
    let any = |fragment: &str| paths.iter().any(|p| p.contains(fragment));

    // DX12 via VKD3D — checked before raw Vulkan.
    if any("vkd3d") {
        return Some("dx12_via_vkd3d".into());
    }
    // DX9 via DXVK — check specific D3D9 SO before generic DXVK match.
    if any("libdxvk_d3d9") || any("dxvk_d3d9.dll") {
        return Some("dx9_via_dxvk".into());
    }
    // DX11 via DXVK.
    if any("dxvk") {
        return Some("dx11_via_dxvk".into());
    }
    // Native Windows Direct3D. These stay below DXVK/Wine matchers so Proton
    // translated games keep the more specific existing values.
    if any("d3d12.dll") {
        return Some("dx12".into());
    }
    if any("d3d11.dll") {
        return Some("dx11".into());
    }
    // Native Vulkan (or Vulkan-based runtime that didn't match above).
    if any("libvulkan") || any("vulkan-1.dll") {
        return Some("vulkan".into());
    }
    // OpenGL.
    if any("libgl.so") || any("libglx.so") || any("opengl32.dll") {
        return Some("opengl".into());
    }

    None
}

/// Detect upscaler technology from mapped file paths.
/// Returns a string matching the `rigsignal.settings.upscaler.tech` field values.
pub(crate) fn detect_upscaler_from_paths(paths: &[String]) -> Option<String> {
    let any = |fragment: &str| paths.iter().any(|p| p.contains(fragment));

    // DLSS (NVIDIA) — check specific DLL names; avoid false-positives from nvngx.dll
    // which is the generic NGX loader and present even without DLSS active.
    if any("nvngx_dlss") {
        return Some("dlss".into());
    }
    // XeSS (Intel).
    if any("xess") {
        return Some("xess".into());
    }
    // FSR (AMD) — AMD FidelityFX Super Resolution.
    if any("ffx_fsr") || any("libffx_fsr") || any("amd_fidelityfx_vk") || any("openfsr") {
        return Some("fsr".into());
    }

    None
}

/// Detect frame-generation technology from mapped file paths.
/// Returns a string matching the `rigsignal.settings.frame_gen.tech` field values.
pub(crate) fn detect_frame_gen_from_paths(paths: &[String]) -> Option<String> {
    let any = |fragment: &str| paths.iter().any(|p| p.contains(fragment));

    // DLSS 3 Frame Generation (NVIDIA) — dedicated G-buffer SO/DLL.
    if any("nvngx_dlssg") || any("dlss_fg") || any("dlssg.dll") {
        return Some("dlss3".into());
    }
    // FSR 3 Frame Generation (AMD).
    if any("ffx_framegeneration") || any("ffx_fsr3framegen") {
        return Some("fsr3".into());
    }
    // AFMF — AMD Fluid Motion Frames (driver-level, rarely appears in maps).
    if any("afmf") {
        return Some("afmf".into());
    }

    None
}

// ── Loaded-module readers (per-platform) ──────────────────────────────────────

/// Read the loaded modules of `pid` and return all file paths (lowercase).
/// Returns an empty Vec on any error (process gone, access denied, unsupported
/// platform). All callers tolerate empty results by returning `None`.
#[cfg(target_os = "linux")]
pub(crate) fn read_mapped_paths(pid: u32) -> Vec<String> {
    let content = match std::fs::read_to_string(format!("/proc/{}/maps", pid)) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };

    content
        .lines()
        .filter_map(|line| {
            // Format: address perms offset dev inode [pathname]
            // Split on whitespace; the pathname is the 6th token (index 5) when present.
            let mut fields = line.splitn(6, char::is_whitespace);
            for _ in 0..5 {
                fields.next();
            }
            let path = fields.next()?.trim();
            // Only file-backed mappings have paths starting with '/'.
            if path.starts_with('/') {
                Some(path.to_lowercase())
            } else {
                None
            }
        })
        .collect()
}

#[cfg(target_os = "windows")]
pub(crate) fn read_mapped_paths(pid: u32) -> Vec<String> {
    use windows::Win32::Foundation::{CloseHandle, HMODULE};
    use windows::Win32::System::ProcessStatus::{EnumProcessModules, GetModuleFileNameExW};
    use windows::Win32::System::Threading::{
        OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,
    };

    // PROCESS_VM_READ is required by GetModuleFileNameExW even though we're
    // not reading process memory directly. PROCESS_QUERY_LIMITED_INFORMATION
    // is not enough on its own — psapi needs the VM_READ right.
    let handle =
        match unsafe { OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, false, pid) } {
            Ok(h) => h,
            Err(_) => return Vec::new(),
        };

    // 1024 HMODULEs is enough for any sane process (a heavy game loads ~300).
    let mut modules: Vec<HMODULE> = vec![HMODULE::default(); 1024];
    let cb_in = (modules.len() * std::mem::size_of::<HMODULE>()) as u32;
    let mut needed: u32 = 0;

    let enum_ok =
        unsafe { EnumProcessModules(handle, modules.as_mut_ptr(), cb_in, &mut needed).is_ok() };

    let mut paths: Vec<String> = Vec::new();

    if enum_ok {
        let count = (needed as usize / std::mem::size_of::<HMODULE>()).min(modules.len());
        // Use a long buffer (4096 wide chars) — long-path Windows installs can
        // exceed MAX_PATH (260). GetModuleFileNameExW truncates silently if too
        // small, which would cause matcher misses.
        let mut buf = [0u16; 4096];
        for h_module in modules.iter().take(count).copied() {
            let len = unsafe { GetModuleFileNameExW(handle, h_module, &mut buf) } as usize;
            if len == 0 {
                continue;
            }
            paths.push(String::from_utf16_lossy(&buf[..len]).to_lowercase());
        }
    }

    // SAFETY: CloseHandle is always called, even on EnumProcessModules failure.
    unsafe {
        let _ = CloseHandle(handle);
    }

    paths
}

#[cfg(not(any(target_os = "linux", target_os = "windows")))]
pub(crate) fn read_mapped_paths(_pid: u32) -> Vec<String> {
    Vec::new()
}

// ── Unit tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn paths(input: &[&str]) -> Vec<String> {
        input.iter().map(|s| s.to_lowercase()).collect()
    }

    // ── Graphics API ─────────────────────────────────────────────────────────

    #[test]
    fn test_gfx_vkd3d_beats_vulkan() {
        // A DX12 game via VKD3D also maps libvulkan.so (its backend).
        // VKD3D must win.
        let p = paths(&[
            "/usr/lib/x86_64-linux-gnu/libvulkan.so.1",
            "/home/user/.steam/root/steamapps/common/Game/x64/vkd3d-proton.dll",
        ]);
        assert_eq!(
            detect_graphics_api_from_paths(&p).as_deref(),
            Some("dx12_via_vkd3d")
        );
    }

    #[test]
    fn test_gfx_dxvk_d3d9() {
        let p = paths(&[
            "/usr/lib/libdxvk_d3d9.so.0",
            "/usr/lib/x86_64-linux-gnu/libvulkan.so.1",
        ]);
        assert_eq!(
            detect_graphics_api_from_paths(&p).as_deref(),
            Some("dx9_via_dxvk")
        );
    }

    #[test]
    fn test_gfx_dxvk_d3d11() {
        let p = paths(&[
            "/usr/lib/libdxvk.so.2",
            "/usr/lib/x86_64-linux-gnu/libvulkan.so.1",
        ]);
        assert_eq!(
            detect_graphics_api_from_paths(&p).as_deref(),
            Some("dx11_via_dxvk")
        );
    }

    #[test]
    fn test_gfx_native_d3d12_only() {
        let p = paths(&[r"c:\windows\system32\d3d12.dll"]);
        assert_eq!(detect_graphics_api_from_paths(&p).as_deref(), Some("dx12"));
    }

    #[test]
    fn test_gfx_native_d3d11_only() {
        let p = paths(&[r"c:\windows\system32\d3d11.dll"]);
        assert_eq!(detect_graphics_api_from_paths(&p).as_deref(), Some("dx11"));
    }

    #[test]
    fn test_gfx_dxvk_beats_native_d3d11() {
        let p = paths(&[
            r"z:\home\user\.steam\steamapps\common\game\dxvk.dll",
            r"c:\windows\system32\d3d11.dll",
        ]);
        assert_eq!(
            detect_graphics_api_from_paths(&p).as_deref(),
            Some("dx11_via_dxvk")
        );
    }

    #[test]
    fn test_gfx_native_vulkan() {
        let p = paths(&["/usr/lib/x86_64-linux-gnu/libvulkan.so.1"]);
        assert_eq!(
            detect_graphics_api_from_paths(&p).as_deref(),
            Some("vulkan")
        );
    }

    #[test]
    fn test_gfx_opengl() {
        let p = paths(&["/usr/lib/x86_64-linux-gnu/libGL.so.1"]);
        assert_eq!(
            detect_graphics_api_from_paths(&p).as_deref(),
            Some("opengl")
        );
    }

    #[test]
    fn test_gfx_none() {
        let p = paths(&["/usr/lib/libm.so.6", "/usr/lib/libc.so.6"]);
        assert!(detect_graphics_api_from_paths(&p).is_none());
    }

    // ── Upscaler ─────────────────────────────────────────────────────────────

    #[test]
    fn test_upscaler_dlss() {
        let p = paths(&["/pfx/drive_c/windows/system32/nvngx_dlss.dll"]);
        assert_eq!(detect_upscaler_from_paths(&p).as_deref(), Some("dlss"));
    }

    #[test]
    fn test_upscaler_xess() {
        let p = paths(&["/game/x64/libxess.so.1"]);
        assert_eq!(detect_upscaler_from_paths(&p).as_deref(), Some("xess"));
    }

    #[test]
    fn test_upscaler_fsr() {
        let p = paths(&["/game/x64/libffx_fsr2_api_vk_x64.so"]);
        assert_eq!(detect_upscaler_from_paths(&p).as_deref(), Some("fsr"));
    }

    #[test]
    fn test_upscaler_none() {
        let p = paths(&["/usr/lib/x86_64-linux-gnu/libvulkan.so.1"]);
        assert!(detect_upscaler_from_paths(&p).is_none());
    }

    // ── Windows native-style paths (no Wine prefix) ──────────────────────────

    #[test]
    fn test_windows_path_dlss() {
        // Native Windows path: drive letter + backslashes, lowercased.
        let p = paths(&[
            r"c:\program files (x86)\steam\steamapps\common\game\nvngx_dlss.dll",
            r"c:\windows\system32\d3d12.dll",
        ]);
        assert_eq!(detect_upscaler_from_paths(&p).as_deref(), Some("dlss"));
        assert_eq!(detect_graphics_api_from_paths(&p).as_deref(), Some("dx12"));
    }

    #[test]
    fn test_windows_path_dlss3_frame_gen() {
        let p = paths(&[
            r"c:\program files (x86)\steam\steamapps\common\game\nvngx_dlssg.dll",
            r"c:\program files (x86)\steam\steamapps\common\game\nvngx_dlss.dll",
        ]);
        assert_eq!(detect_upscaler_from_paths(&p).as_deref(), Some("dlss"));
        assert_eq!(detect_frame_gen_from_paths(&p).as_deref(), Some("dlss3"));
    }

    // ── Frame generation ─────────────────────────────────────────────────────

    #[test]
    fn test_frame_gen_dlss3() {
        let p = paths(&["/pfx/drive_c/windows/system32/nvngx_dlssg.dll"]);
        assert_eq!(detect_frame_gen_from_paths(&p).as_deref(), Some("dlss3"));
    }

    #[test]
    fn test_frame_gen_fsr3() {
        let p = paths(&["/game/x64/ffx_framegeneration_x64.dll"]);
        assert_eq!(detect_frame_gen_from_paths(&p).as_deref(), Some("fsr3"));
    }

    #[test]
    fn test_frame_gen_none() {
        let p = paths(&["/usr/lib/libdxvk.so.2"]);
        assert!(detect_frame_gen_from_paths(&p).is_none());
    }

    // ── Settings overlay ──────────────────────────────────────────────────────

    #[test]
    fn test_overlay_null_when_no_hints() {
        // No upscaler or frame-gen in paths → Null overlay (nothing to merge)
        let p = paths(&["/usr/lib/x86_64-linux-gnu/libvulkan.so.1"]);
        assert!(settings_overlay_from_paths(&p).is_null());
    }

    #[test]
    fn test_overlay_emits_frame_gen_tech_object() {
        let p = paths(&["/pfx/drive_c/windows/system32/nvngx_dlssg.dll"]);
        let overlay = settings_overlay_from_paths(&p);
        let settings = &overlay["rigsignal"]["settings"];
        assert_eq!(settings["frame_gen"]["tech"], "dlss3");
        assert!(settings["frame_gen"].is_object());
    }
}
