pub mod audio;
pub mod cpu;
pub mod gpu_amd;
pub mod mangohud;
pub mod memory;
pub mod network;
pub mod power;
pub mod storage;

pub use audio::AudioCollector;
pub use cpu::CpuCollector;
pub use gpu_amd::GpuAmdCollector as GpuCollector;
pub use mangohud::MangoHudCollector;
pub use memory::MemoryCollector;
pub use network::NetworkCollector;
pub use power::PowerCollector;
pub use storage::StorageCollector;
