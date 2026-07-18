//! Steam Remote Play `streaming_client` GPU engine utilisation collector.
//!
//! DRM fdinfo counters are cumulative nanoseconds, so this collector retains a
//! baseline per selected process and fd identity set and derives utilisation
//! from a monotonic [`Instant`] interval.
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

const DISCOVERY_TTL: Duration = Duration::from_secs(5);
const DISCOVERY_BUDGET: Duration = Duration::from_millis(100);
const MAX_PIDS: usize = 4096;
const MAX_CMDLINE_BYTES: u64 = 4096;
const MAX_FDINFO_FILES: usize = 64;
const MAX_FDINFO_BYTES: u64 = 16 * 1024;

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum FdIdentity {
    Client {
        pdev: String,
        client_id: String,
    },
    Engines {
        pdev: String,
        dec: Option<u64>,
        enc: Option<u64>,
        gfx: Option<u64>,
    },
}

/// A retained descriptor's stable baseline identity.  Fallback grouping uses
/// counter values only to identify duplicate fdinfo records; those counters
/// naturally change between samples and therefore cannot be baseline keys.
#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum BaselineIdentity {
    Client { pdev: String, client_id: String },
    Fallback { pdev: String, fd: u32 },
}

#[derive(Clone, Debug)]
struct FdCounters {
    fd: u32,
    identity: FdIdentity,
    dec: Option<u64>,
    enc: Option<u64>,
    gfx: Option<u64>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct SelectedProcess {
    pid: u32,
    starttime: u64,
}

#[derive(Clone, Debug)]
struct Baseline {
    process: SelectedProcess,
    identities: Vec<BaselineIdentity>,
    total: u64,
    sampled_at: Instant,
}

/// Linux-only collector.  `proc_root` is injectable for fixture-driven tests.
pub struct StreamClientCollector {
    proc_root: PathBuf,
    last_scan: Option<Instant>,
    selected: Option<SelectedProcess>,
    video_baseline: Option<Baseline>,
    gfx_baseline: Option<Baseline>,
}

impl StreamClientCollector {
    pub fn new() -> Self {
        Self::with_proc_root(PathBuf::from("/proc"))
    }

    fn with_proc_root(proc_root: PathBuf) -> Self {
        Self {
            proc_root,
            last_scan: None,
            selected: None,
            video_baseline: None,
            gfx_baseline: None,
        }
    }

    fn clear_baselines(&mut self) {
        self.video_baseline = None;
        self.gfx_baseline = None;
    }

    fn starttime(&self, pid: u32) -> Option<u64> {
        let stat = fs::read_to_string(self.proc_root.join(pid.to_string()).join("stat")).ok()?;
        // `comm` can contain spaces and parentheses; fields after its final ')'
        // start with field 3.  starttime is field 22, index 19 in that tail.
        stat.rsplit_once(')')?
            .1
            .split_whitespace()
            .nth(19)?
            .parse()
            .ok()
    }

    fn candidate_name(&self, pid: u32) -> bool {
        let proc_dir = self.proc_root.join(pid.to_string());
        if fs::read_to_string(proc_dir.join("comm"))
            .ok()
            .map(|v| v.trim_end_matches(['\n', '\r']) == "streaming_client")
            .unwrap_or(false)
        {
            return true;
        }

        let mut bytes = Vec::new();
        let Ok(mut file) = fs::File::open(proc_dir.join("cmdline")) else {
            return false;
        };
        if file
            .by_ref()
            .take(MAX_CMDLINE_BYTES)
            .read_to_end(&mut bytes)
            .is_err()
        {
            return false;
        }
        let first_arg = bytes.split(|byte| *byte == 0).next().unwrap_or_default();
        Path::new(std::ffi::OsStr::from_bytes(first_arg))
            .file_name()
            .and_then(|name| name.to_str())
            == Some("streaming_client")
    }

    fn scan(&mut self, now: Instant) {
        self.last_scan = Some(now);
        let scan_started = Instant::now();
        let mut pids: Vec<u32> = match fs::read_dir(&self.proc_root) {
            Ok(entries) => entries
                .filter_map(|entry| entry.ok())
                .filter_map(|entry| entry.file_name().to_str()?.parse().ok())
                .take(MAX_PIDS)
                .collect(),
            Err(_) => Vec::new(),
        };
        pids.sort_unstable();

        let mut newest: Option<SelectedProcess> = None;
        for pid in pids {
            if scan_started.elapsed() >= DISCOVERY_BUDGET {
                break;
            }
            if !self.candidate_name(pid) {
                continue;
            }
            let Some(starttime) = self.starttime(pid) else {
                continue;
            };
            let candidate = SelectedProcess { pid, starttime };
            if newest
                .as_ref()
                .map(|old| (candidate.starttime, candidate.pid) > (old.starttime, old.pid))
                .unwrap_or(true)
            {
                newest = Some(candidate);
            }
        }
        if newest != self.selected {
            self.clear_baselines();
        }
        self.selected = newest;
    }

    fn selected_process(&mut self, now: Instant) -> Option<SelectedProcess> {
        if let Some(selected) = self.selected.clone() {
            if self.starttime(selected.pid) != Some(selected.starttime) {
                // Do not retain the discovery timestamp: PID reuse must trigger a
                // fresh scan immediately rather than using a stale 5s result.
                self.selected = None;
                self.last_scan = None;
                self.clear_baselines();
            }
        }
        if self
            .last_scan
            .map(|last| now.duration_since(last) >= DISCOVERY_TTL)
            .unwrap_or(true)
        {
            self.scan(now);
        }
        self.selected.clone()
    }

    fn read_fdinfo(&self, pid: u32) -> Vec<FdCounters> {
        let dir = self.proc_root.join(pid.to_string()).join("fdinfo");
        let Ok(entries) = fs::read_dir(dir) else {
            return Vec::new();
        };
        let mut fds: Vec<(u32, PathBuf)> = entries
            .filter_map(|entry| entry.ok())
            .filter_map(|entry| {
                let fd = entry.file_name().to_str()?.parse().ok()?;
                Some((fd, entry.path()))
            })
            .collect();
        fds.sort_unstable_by_key(|(fd, _)| *fd);

        fds.into_iter()
            .take(MAX_FDINFO_FILES)
            .filter_map(|(fd, path)| parse_fdinfo(fd, &path))
            .collect()
    }

    fn sample_at(&mut self, now: Instant) -> Option<Value> {
        self.sample_at_post(now, Instant::now())
    }

    fn sample_at_post(&mut self, now: Instant, post_sample: Instant) -> Option<Value> {
        let selected = self.selected_process(now)?;
        let entries = self.read_fdinfo(selected.pid);
        let retained = deduplicate(entries);
        let mut identities: Vec<BaselineIdentity> =
            retained.iter().map(baseline_identity).collect();
        identities.sort();

        let mut video_total = 0_u64;
        let mut has_dec = false;
        let mut has_enc = false;
        let mut gfx_total = 0_u64;
        let mut has_gfx = false;
        for entry in &retained {
            if let Some(value) = entry.dec {
                video_total = video_total.saturating_add(value);
                has_dec = true;
            }
            if let Some(value) = entry.enc {
                video_total = video_total.saturating_add(value);
                has_enc = true;
            }
            if let Some(value) = entry.gfx {
                gfx_total = gfx_total.saturating_add(value);
                has_gfx = true;
            }
        }

        let mut client = Map::new();
        if has_dec || has_enc {
            if let Some(value) = delta_percent(
                &mut self.video_baseline,
                selected.clone(),
                identities.clone(),
                video_total,
                post_sample,
            ) {
                // (false, false) cannot occur inside the has_dec || has_enc guard, but an
                // always-on agent must not carry a panic-class branch in its tick path.
                let engine = match (has_dec, has_enc) {
                    (true, true) => Some("dec+enc"),
                    (true, false) => Some("dec"),
                    (false, true) => Some("enc"),
                    (false, false) => None,
                };
                if let Some(engine) = engine {
                    client.insert("video_busy_pct".to_string(), json!(value));
                    client.insert("video_engine".to_string(), json!(engine));
                }
            }
        }
        if has_gfx {
            if let Some(value) = delta_percent(
                &mut self.gfx_baseline,
                selected,
                identities,
                gfx_total,
                post_sample,
            ) {
                client.insert("gfx_busy_pct".to_string(), json!(value));
            }
        }

        Some(json!({ "rigsignal": { "stream": { "client": client } } }))
    }
}

impl Collector for StreamClientCollector {
    fn dataset(&self) -> &'static str {
        "rigsignal.stream_client"
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        Ok(self.sample_at(Instant::now()))
    }
}

fn parse_fdinfo(fd: u32, path: &Path) -> Option<FdCounters> {
    let mut contents = Vec::new();
    fs::File::open(path)
        .ok()?
        .take(MAX_FDINFO_BYTES)
        .read_to_end(&mut contents)
        .ok()?;
    let contents = String::from_utf8_lossy(&contents);
    let mut values: BTreeMap<&str, String> = BTreeMap::new();
    for line in contents.lines() {
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        let key = key.trim();
        if matches!(
            key,
            "drm-pdev" | "drm-client-id" | "drm-engine-dec" | "drm-engine-enc" | "drm-engine-gfx"
        ) {
            values.insert(key, value.trim().to_string());
        }
    }
    let pdev = values.remove("drm-pdev")?;
    let dec = values
        .get("drm-engine-dec")
        .and_then(|value| value.parse().ok());
    let enc = values
        .get("drm-engine-enc")
        .and_then(|value| value.parse().ok());
    let gfx = values
        .get("drm-engine-gfx")
        .and_then(|value| value.parse().ok());
    if dec.is_none() && enc.is_none() && gfx.is_none() {
        return None;
    }
    let identity = match values.get("drm-client-id") {
        Some(client_id) => FdIdentity::Client {
            pdev,
            client_id: client_id.clone(),
        },
        None => FdIdentity::Engines {
            pdev,
            dec,
            enc,
            gfx,
        },
    };
    Some(FdCounters {
        fd,
        identity,
        dec,
        enc,
        gfx,
    })
}

fn deduplicate(entries: Vec<FdCounters>) -> Vec<FdCounters> {
    let mut retained: BTreeMap<FdIdentity, FdCounters> = BTreeMap::new();
    for entry in entries {
        match retained.get(&entry.identity) {
            Some(old) if old.fd <= entry.fd => {}
            _ => {
                retained.insert(entry.identity.clone(), entry);
            }
        }
    }
    retained.into_values().collect()
}

fn baseline_identity(entry: &FdCounters) -> BaselineIdentity {
    match &entry.identity {
        FdIdentity::Client { pdev, client_id } => BaselineIdentity::Client {
            pdev: pdev.clone(),
            client_id: client_id.clone(),
        },
        FdIdentity::Engines { pdev, .. } => BaselineIdentity::Fallback {
            pdev: pdev.clone(),
            fd: entry.fd,
        },
    }
}

fn delta_percent(
    baseline: &mut Option<Baseline>,
    process: SelectedProcess,
    identities: Vec<BaselineIdentity>,
    total: u64,
    sampled_at: Instant,
) -> Option<f64> {
    let previous = baseline.take();
    let value = previous.and_then(|previous| {
        if previous.process != process
            || previous.identities != identities
            || total < previous.total
        {
            return None;
        }
        let elapsed_ns = sampled_at
            .checked_duration_since(previous.sampled_at)?
            .as_nanos();
        if elapsed_ns == 0 {
            return None;
        }
        let raw = 100.0 * (total - previous.total) as f64 / elapsed_ns as f64;
        if raw > 100.0 {
            tracing::debug!(
                raw_busy_pct = raw,
                "stream_client utilisation clamped to 100%"
            );
        }
        Some(((raw.clamp(0.0, 100.0) * 100.0).round()) / 100.0)
    });
    *baseline = Some(Baseline {
        process,
        identities,
        total,
        sampled_at,
    });
    value
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static NEXT_TEMP: AtomicUsize = AtomicUsize::new(0);

    fn fixture_root() -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "rigsignal-stream-client-test-{}-{}",
            std::process::id(),
            NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&root).unwrap();
        root
    }

    fn stat(starttime: u64) -> String {
        // state (field 3), then fields 4..22; starttime is tail item 19.
        format!(
            "1 (streaming_client) S {} {starttime} 0\n",
            vec!["0"; 18].join(" ")
        )
    }

    fn process(root: &Path, pid: u32, starttime: u64, comm: Option<&str>) {
        let dir = root.join(pid.to_string());
        fs::create_dir_all(dir.join("fdinfo")).unwrap();
        fs::write(dir.join("stat"), stat(starttime)).unwrap();
        if let Some(comm) = comm {
            fs::write(dir.join("comm"), format!("{comm}\n")).unwrap();
        }
    }

    fn fdinfo(root: &Path, pid: u32, fd: u32, contents: &str) {
        fs::write(
            root.join(pid.to_string())
                .join("fdinfo")
                .join(fd.to_string()),
            contents,
        )
        .unwrap();
    }

    fn client(doc: &Value) -> &Value {
        &doc["rigsignal"]["stream"]["client"]
    }

    #[test]
    fn deduplicates_client_and_fallback_identities_using_lowest_fd() {
        let root = fixture_root();
        process(&root, 99, 1, Some("streaming_client"));
        fdinfo(
            &root,
            99,
            28,
            "drm-pdev: 0000:03:00.0\ndrm-client-id: 7\ndrm-engine-dec: 100000000\n",
        );
        fdinfo(
            &root,
            99,
            30,
            "drm-pdev: 0000:03:00.0\ndrm-client-id: 7\ndrm-engine-dec: 100000000\n",
        );
        fdinfo(
            &root,
            99,
            31,
            "drm-pdev: 0000:04:00.0\ndrm-engine-enc: 50000000\n",
        );
        fdinfo(
            &root,
            99,
            32,
            "drm-pdev: 0000:04:00.0\ndrm-engine-enc: 50000000\n",
        );
        let mut collector = StreamClientCollector::with_proc_root(root.clone());
        let start = Instant::now();
        collector.sample_at_post(start, start).unwrap();
        fdinfo(
            &root,
            99,
            28,
            "drm-pdev: 0000:03:00.0\ndrm-client-id: 7\ndrm-engine-dec: 200000000\n",
        );
        fdinfo(
            &root,
            99,
            30,
            "drm-pdev: 0000:03:00.0\ndrm-client-id: 7\ndrm-engine-dec: 999000000\n",
        );
        fdinfo(
            &root,
            99,
            31,
            "drm-pdev: 0000:04:00.0\ndrm-engine-enc: 100000000\n",
        );
        fdinfo(
            &root,
            99,
            32,
            "drm-pdev: 0000:04:00.0\ndrm-engine-enc: 100000000\n",
        );
        let doc = collector
            .sample_at_post(
                start + Duration::from_secs(1),
                start + Duration::from_secs(1),
            )
            .unwrap();
        assert_eq!(client(&doc)["video_busy_pct"], json!(15.0));
        assert_eq!(client(&doc)["video_engine"], json!("dec+enc"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn newest_candidate_and_pid_tie_break_are_selected() {
        let root = fixture_root();
        process(&root, 10, 20, Some("streaming_client"));
        process(&root, 11, 20, Some("streaming_client"));
        process(&root, 12, 19, Some("streaming_client"));
        let mut collector = StreamClientCollector::with_proc_root(root.clone());
        collector.selected_process(Instant::now());
        assert_eq!(
            collector.selected,
            Some(SelectedProcess {
                pid: 11,
                starttime: 20
            })
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn pid_reuse_and_backwards_counter_reset_baselines() {
        let root = fixture_root();
        process(&root, 77, 1, Some("streaming_client"));
        fdinfo(&root, 77, 3, "drm-pdev: gpu\ndrm-engine-gfx: 100\n");
        let mut collector = StreamClientCollector::with_proc_root(root.clone());
        let start = Instant::now();
        collector.sample_at_post(start, start).unwrap();
        fdinfo(&root, 77, 3, "drm-pdev: gpu\ndrm-engine-gfx: 200\n");
        assert!(client(
            &collector
                .sample_at_post(
                    start + Duration::from_secs(1),
                    start + Duration::from_secs(1)
                )
                .unwrap()
        )["gfx_busy_pct"]
            .is_number());
        fs::write(root.join("77/stat"), stat(2)).unwrap();
        fdinfo(&root, 77, 3, "drm-pdev: gpu\ndrm-engine-gfx: 300\n");
        let reused = collector
            .sample_at_post(
                start + Duration::from_secs(6),
                start + Duration::from_secs(6),
            )
            .unwrap();
        assert!(client(&reused).get("gfx_busy_pct").is_none());
        fdinfo(&root, 77, 3, "drm-pdev: gpu\ndrm-engine-gfx: 250\n");
        let backwards = collector
            .sample_at_post(
                start + Duration::from_secs(7),
                start + Duration::from_secs(7),
            )
            .unwrap();
        assert!(client(&backwards).get("gfx_busy_pct").is_none());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn monotonic_delta_is_rounded_and_clamped() {
        let process = SelectedProcess {
            pid: 1,
            starttime: 1,
        };
        let identities = vec![BaselineIdentity::Fallback {
            pdev: "gpu".into(),
            fd: 1,
        }];
        let start = Instant::now();
        let mut baseline = None;
        assert_eq!(
            delta_percent(
                &mut baseline,
                process.clone(),
                identities.clone(),
                100,
                start
            ),
            None
        );
        assert_eq!(
            delta_percent(
                &mut baseline,
                process.clone(),
                identities.clone(),
                1_200_000_100,
                start + Duration::from_secs(1)
            ),
            Some(100.0)
        );
        assert_eq!(
            delta_percent(
                &mut baseline,
                process,
                identities,
                1_201_234_100,
                start + Duration::from_secs(2)
            ),
            Some(0.12)
        );
    }

    #[test]
    fn gfx_only_and_enc_only_keep_metric_shape_honest() {
        let root = fixture_root();
        process(&root, 50, 1, Some("streaming_client"));
        fdinfo(&root, 50, 4, "drm-pdev: gpu\ndrm-engine-gfx: 10\n");
        let mut collector = StreamClientCollector::with_proc_root(root.clone());
        let start = Instant::now();
        collector.sample_at_post(start, start).unwrap();
        fdinfo(&root, 50, 4, "drm-pdev: gpu\ndrm-engine-gfx: 30\n");
        let gfx = collector
            .sample_at_post(
                start + Duration::from_secs(1),
                start + Duration::from_secs(1),
            )
            .unwrap();
        assert!(client(&gfx).get("video_busy_pct").is_none());
        assert!(client(&gfx)["gfx_busy_pct"].is_number());

        fdinfo(&root, 50, 4, "drm-pdev: gpu\ndrm-engine-enc: 40\n");
        collector
            .sample_at_post(
                start + Duration::from_secs(2),
                start + Duration::from_secs(2),
            )
            .unwrap();
        fdinfo(&root, 50, 4, "drm-pdev: gpu\ndrm-engine-enc: 60\n");
        let enc = collector
            .sample_at_post(
                start + Duration::from_secs(3),
                start + Duration::from_secs(3),
            )
            .unwrap();
        assert_eq!(client(&enc)["video_engine"], json!("enc"));
        assert!(client(&enc).get("gfx_busy_pct").is_none());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn one_document_per_tick_even_with_multiple_metrics() {
        let root = fixture_root();
        process(&root, 90, 1, Some("streaming_client"));
        fdinfo(
            &root,
            90,
            4,
            "drm-pdev: gpu\ndrm-engine-dec: 1\ndrm-engine-gfx: 1\n",
        );
        let mut collector = StreamClientCollector::with_proc_root(root.clone());
        let at = Instant::now();
        let emissions: Vec<Value> = collector.sample_at_post(at, at).into_iter().collect();
        assert_eq!(
            emissions.len(),
            1,
            "a collector tick has one metric document at most"
        );
        let _ = fs::remove_dir_all(root);
    }
}
