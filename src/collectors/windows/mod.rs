mod pdh;
mod wmi;
pub mod audio;
pub mod cpu;
pub mod frame;
pub mod gpu;
pub mod memory;
pub mod network;
pub mod power;
pub mod storage;

pub use audio::AudioCollector;
pub use cpu::CpuCollector;
pub use frame::FrameCollector as MangoHudCollector;
pub use gpu::GpuCollector;
pub use memory::MemoryCollector;
pub use network::NetworkCollector;
pub use power::PowerCollector;
pub use storage::StorageCollector;
