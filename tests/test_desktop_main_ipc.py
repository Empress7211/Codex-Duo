import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))
from desktop_main_patches import PatchError, _patch_ipc


class DesktopMainIPCTest(unittest.TestCase):
    def test_directory_override_for_both_native_shapes(self):
        for native in (
            'let e=o.join((0,l.D)(),"ipc");s.mkdirSync(e,{mode:448,recursive:!0});o.join(e,"ipc.sock")',
            'let e=o.join((0,l.D)(),"ipc"),t=o.join(e,"ipc.sock"),r=p(e);',
        ):
            with self.subTest(native=native):
                texts = {"main.js": native}
                patched = dict(texts)
                self.assertEqual(_patch_ipc(texts, patched), "main.js")
                self.assertEqual(patched["main.js"], native.replace(
                    'o.join((0,l.D)(),"ipc")',
                    '/*codex-duo:ipc*/process.env.ORBIT_IPC_DIR||o.join((0,l.D)(),"ipc")',
                    1))

    def test_rejects_ambiguous_or_wrong_directory(self):
        native = 'let e=o.join(d,"ipc"),t=o.join(e,"ipc.sock"),r=p(e);'
        for text in (native + native,
                     'let e=o.join(d,"ipc"),t=o.join(other,"ipc.sock"),r=p(e);'):
            with self.subTest(text=text):
                with self.assertRaises(PatchError):
                    _patch_ipc({"main.js": text}, {"main.js": text})


if __name__ == "__main__":
    unittest.main()
