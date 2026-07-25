import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "fleet-owner-cluster"


class FleetOwnerFixtureTests(unittest.TestCase):
    """Keep gate seed inputs pinned to the committed owner-cluster captures."""

    CAPTURE_SHA256 = {
        "logs-rigsignal.events": "f9b7e836185c0cc9b27785a5c8dca9b0d6b294935fc32a81c32c9de6065ca46f",
        "metrics-rigsignal.audio": "4fef5c207ae022769edce13e92cb5cf336e9354de798fd1d941fdafac56d6892",
        "metrics-rigsignal.cpu": "006468b5197608739d415479812b66c33474409a7fc8d5ca3b81baae3368e0b0",
        "metrics-rigsignal.ebpf": "b02e08a682c768ec3e5ef4f7f29d18ee1ec986b376cd4c8bdf47a29ac24e335c",
        "metrics-rigsignal.ebpf_thread": "b8551d8fcabb673254476ba8071cdd585f388afce06810a5d4e8771a50dbac2e",
        "metrics-rigsignal.frame": "7d55c19145f0d34783a3bfda6f17211b3d4275c3e1cb8aee1f0d94a5bf851040",
        "metrics-rigsignal.gpu": "dc366264c02b4adcdf0340e5ef232861666dc833e6eeb41d5486af696a90eaaa",
        "metrics-rigsignal.memory": "270558cf51df3bf867dcc18b7fd93e646e51bedc1fcafd9ae62aff52a429e630",
        "metrics-rigsignal.network": "d69a71b81604e1c366a6499f8f7a9b4e4bb5c49e6ac28c09b8a19270e1b04898",
        "metrics-rigsignal.power": "3ad9a218b69d40742ca21eb6c60a7bcbbaca384ad48d91df11d3a7767109aeff",
        "metrics-rigsignal.session": "74637164d0dfb526b1d6a71cc86413fe4a7cc533860a16c1b2a9653c1e76471b",
        "metrics-rigsignal.storage": "7fd851a95bc334c11dbdf3bcb05dbbbb6ec8f296f79ef462bc4a61dc16d033ab",
        "metrics-rigsignal.stream_client": "610fd9604e1b89c34f005656b2eb45e3b6c659d1e3114f5676f78ac0b4190b30",
    }

    def test_seeded_index_template_bodies_are_the_owner_captures(self):
        self.assertEqual(len(self.CAPTURE_SHA256), 13)
        for name, digest in self.CAPTURE_SHA256.items():
            with self.subTest(name=name):
                path = FIXTURES / f"live-{name}.json"
                raw = path.read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
                body = json.loads(raw)["index_templates"][0]["index_template"]
                self.assertEqual(body["template"]["mappings"]["_meta"]["managed_by"], "fleet")
