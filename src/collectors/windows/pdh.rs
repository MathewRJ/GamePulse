/// PDH (Performance Data Helper) query wrapper for Windows collectors.
///
/// # Why counters must be long-lived
///
/// PDH rate counters (e.g. `% Processor Time`) compute a delta between two samples.
/// The first `PdhCollectQueryData` after opening a query establishes the baseline —
/// rate counters return zero until a second collect has occurred. Opening a new
/// `PdhQuery` per tick would therefore always return 0 for rate counters. Keep one
/// `PdhQuery` per collector and call `collect()` on each agent tick.
///
/// # Upgrade note
///
/// If a future collector needs ETW-based counters instead of PDH, add a second
/// module (`etw.rs`) alongside this one rather than changing the `PdhQuery` API.
use anyhow::{anyhow, Result};
use windows::core::PCWSTR;
use windows::Win32::System::Performance::{
    PdhAddCounterW, PdhCloseQuery, PdhCollectQueryData, PdhGetFormattedCounterArrayW,
    PdhGetFormattedCounterValue, PDH_FMT_COUNTERVALUE, PDH_FMT_DOUBLE,
};

// PdhOpenQueryW is in the same feature but sometimes lives under a slightly
// different import path in newer windows-rs versions.
use windows::Win32::System::Performance::PdhOpenQueryW;

const PDH_MORE_DATA: u32 = 0x800007D2;

fn pdh_ok(status: u32) -> Result<()> {
    if status == 0 {
        Ok(())
    } else {
        Err(anyhow!("PDH error code: 0x{:08X}", status))
    }
}

/// Newtype over the raw PDH query handle (`isize`).
///
/// PDH query handles are safe to use from any thread after `PdhOpenQuery`; the
/// windows crate's raw `isize` is `Send` by construction.
struct QueryHandle(isize);

/// Newtype over the raw PDH counter handle (`isize`).
pub struct PdhCounter(isize);

/// Long-lived PDH query. Open once in the collector's `new()` and reuse across ticks.
pub struct PdhQuery {
    handle: QueryHandle,
}

impl PdhQuery {
    /// Open a new PDH query. Add counters immediately after, then call `collect()` once
    /// to establish the baseline before the first real tick.
    pub fn new() -> Result<Self> {
        let mut handle: isize = 0;
        let status = unsafe { PdhOpenQueryW(PCWSTR::null(), 0, &mut handle) };
        pdh_ok(status)?;
        Ok(PdhQuery {
            handle: QueryHandle(handle),
        })
    }

    /// Add a counter by its PDH path (e.g. `\\Processor(_Total)\\% Processor Time`).
    pub fn add_counter(&mut self, path: &str) -> Result<PdhCounter> {
        let wide: Vec<u16> = path.encode_utf16().chain(std::iter::once(0)).collect();
        let mut counter: isize = 0;
        let status =
            unsafe { PdhAddCounterW(self.handle.0, PCWSTR(wide.as_ptr()), 0, &mut counter) };
        pdh_ok(status)?;
        Ok(PdhCounter(counter))
    }

    /// Snapshot all counters. Must be called at least once before reading values.
    /// The first call establishes the rate baseline.
    pub fn collect(&mut self) -> Result<()> {
        let status = unsafe { PdhCollectQueryData(self.handle.0) };
        pdh_ok(status)
    }

    /// Return the formatted `f64` value of a scalar counter.
    pub fn counter_value_f64(&self, counter: &PdhCounter) -> Result<f64> {
        let mut value = PDH_FMT_COUNTERVALUE::default();
        let mut counter_type: u32 = 0;
        let status = unsafe {
            PdhGetFormattedCounterValue(
                counter.0,
                PDH_FMT_DOUBLE,
                Some(&mut counter_type),
                &mut value,
            )
        };
        pdh_ok(status)?;
        Ok(unsafe { value.Anonymous.doubleValue })
    }

    /// Return all instances of a wildcard counter sorted by instance name.
    pub fn counter_values_array(&self, counter: &PdhCounter) -> Result<Vec<(String, f64)>> {
        let mut buf_size: u32 = 0;
        let mut item_count: u32 = 0;

        // First call: determine required buffer size.
        let status = unsafe {
            PdhGetFormattedCounterArrayW(
                counter.0,
                PDH_FMT_DOUBLE,
                &mut buf_size,
                &mut item_count,
                None,
            )
        };
        if status != PDH_MORE_DATA && status != 0 {
            return Err(anyhow!(
                "PdhGetFormattedCounterArrayW size query error: 0x{:08X}",
                status as u32
            ));
        }
        if item_count == 0 {
            return Ok(Vec::new());
        }

        // Second call: fill the buffer.
        let mut buf: Vec<u8> = vec![0u8; buf_size as usize];
        let status = unsafe {
            PdhGetFormattedCounterArrayW(
                counter.0,
                PDH_FMT_DOUBLE,
                &mut buf_size,
                &mut item_count,
                Some(buf.as_mut_ptr() as *mut _),
            )
        };
        pdh_ok(status)?;

        // PDH_FMT_COUNTERVALUE_ITEM_W layout (x64):
        //   szName: *const u16  (8 bytes — pointer into the same buffer)
        //   FmtValue: PDH_FMT_COUNTERVALUE {
        //       CStatus: u32  (4 bytes)
        //       _pad:    u32  (4 bytes, alignment)
        //       union:   f64  (8 bytes)
        //   }
        // Total: 24 bytes per item.
        const ITEM_SIZE: usize = 24;
        let mut results: Vec<(String, f64)> = Vec::with_capacity(item_count as usize);

        for i in 0..item_count as usize {
            let offset = i * ITEM_SIZE;
            if offset + ITEM_SIZE > buf.len() {
                break;
            }
            let name_ptr =
                usize::from_ne_bytes(buf[offset..offset + 8].try_into().unwrap()) as *const u16;
            // doubleValue starts at offset+8 (szName ptr) + 4 (CStatus) + 4 (pad) = offset+16
            let value_offset = offset + 16;
            if value_offset + 8 > buf.len() {
                break;
            }
            let double_bytes: [u8; 8] = buf[value_offset..value_offset + 8].try_into().unwrap();
            let value = f64::from_ne_bytes(double_bytes);

            let name = if name_ptr.is_null() {
                String::new()
            } else {
                unsafe {
                    let mut len = 0usize;
                    while *name_ptr.add(len) != 0 {
                        len += 1;
                    }
                    String::from_utf16_lossy(std::slice::from_raw_parts(name_ptr, len))
                }
            };

            results.push((name, value));
        }

        results.sort_by(|a, b| a.0.cmp(&b.0));
        Ok(results)
    }
}

impl Drop for PdhQuery {
    fn drop(&mut self) {
        unsafe {
            let _ = PdhCloseQuery(self.handle.0);
        }
    }
}
